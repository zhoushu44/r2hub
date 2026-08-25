import asyncio
import hashlib
import hmac
import mimetypes
import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.security.utils import get_authorization_scheme_param
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import db, init_db, insert_returning_id
from s3util import bucket_stats, direct_url, gen_key, get_candidates, head_bucket, make_client

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
if not ADMIN_TOKEN:
    raise SystemExit("环境变量 ADMIN_TOKEN 未设置")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_BUCKET_GB = float(os.environ.get("MAX_BUCKET_GB", "9.5"))
UNHEALTHY_FAILS = int(os.environ.get("UNHEALTHY_FAILS", "5"))
CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "60"))
LINK_MODE = os.environ.get("LINK_MODE", "redirect").lower()
THREADPOOL_TOKENS = int(os.environ.get("THREADPOOL_TOKENS", "100"))

app = FastAPI(title="R2 Hub", docs_url=None, redoc_url=None)
init_db()


def auth(request: Request):
    auth_header = request.headers.get("Authorization", "")
    scheme, param = get_authorization_scheme_param(auth_header)
    if scheme.lower() != "bearer" or not hmac.compare_digest(param, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="未授权")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _touch_api_key(key_id: int):
    from datetime import datetime

    try:
        with db() as conn:
            conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key_id))
    except Exception:
        pass


def auth_upload(request: Request):
    auth_header = request.headers.get("Authorization", "")
    scheme, param = get_authorization_scheme_param(auth_header)
    if scheme.lower() != "bearer" or not param:
        raise HTTPException(status_code=401, detail="未授权")
    if hmac.compare_digest(param, ADMIN_TOKEN):
        return
    kh = _sha(param)
    with db() as conn:
        row = conn.execute("SELECT id FROM api_keys WHERE key_hash=? AND enabled=1", (kh,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="未授权")
    _touch_api_key(row["id"])


class BucketIn(BaseModel):
    label: str
    account_id: str = ""
    access_key_id: str
    secret_access_key: str
    bucket: str
    endpoint: str = ""
    public_base_url: str = ""
    path_style: bool = True
    enabled: bool = True


class BulkIn(BaseModel):
    buckets: list[BucketIn]


class ApiKeyIn(BaseModel):
    name: str
    enabled: Optional[bool] = None


@app.get("/api/keys")
def list_keys(_=Depends(auth)):
    with db() as conn:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY id DESC").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "key_prefix": r["key_prefix"],
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"] or "-",
        }
        for r in rows
    ]


@app.post("/api/keys")
def create_key(k: ApiKeyIn, _=Depends(auth)):
    name = k.name.strip() or "default"
    raw = "r2h_" + secrets.token_hex(16)
    with db() as conn:
        conn.execute(
            "INSERT INTO api_keys(name,key_prefix,key_hash) VALUES(?,?,?)",
            (name, raw[:10] + "...", _sha(raw)),
        )
    return {"key": raw, "name": name}


@app.put("/api/keys/{kid}")
def update_key(kid: int, k: ApiKeyIn, _=Depends(auth)):
    if k.enabled is None:
        raise HTTPException(400, "需要 enabled 字段")
    with db() as conn:
        row = conn.execute("SELECT id FROM api_keys WHERE id=?", (kid,)).fetchone()
        if not row:
            raise HTTPException(404, "密钥不存在")
        conn.execute("UPDATE api_keys SET enabled=? WHERE id=?", (1 if k.enabled else 0, kid))
    return {"ok": True}


@app.delete("/api/keys/{kid}")
def delete_key(kid: int, _=Depends(auth)):
    with db() as conn:
        row = conn.execute("SELECT id FROM api_keys WHERE id=?", (kid,)).fetchone()
        if not row:
            raise HTTPException(404, "密钥不存在")
        conn.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/me")
def me(_=Depends(auth)):
    return {"ok": True}


AUTH_ERROR_MARKS = ("401", "403", "signaturedoesnotmatch", "invalidaccesskeyid", "accessdenied", "invalidtoken")


def _is_auth_error(message: str) -> bool:
    low = message.lower()
    return any(m in low for m in AUTH_ERROR_MARKS)


def _persist_test(conn, bid: int, result: dict):
    from datetime import datetime

    conn.execute(
        "UPDATE buckets SET last_checked=?, last_status=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), result["message"], bid),
    )


def _check_all_sync() -> list:
    report = []
    with db() as conn:
        rows = conn.execute("SELECT * FROM buckets WHERE enabled=1").fetchall()
    for row in rows:
        result = head_bucket(row)
        with db() as conn:
            _persist_test(conn, row["id"], result)
            if not result["ok"]:
                if _is_auth_error(result["message"]) or row["failed_count"] + 1 >= UNHEALTHY_FAILS:
                    conn.execute("UPDATE buckets SET enabled=0 WHERE id=?", (row["id"],))
                    result["disabled"] = True
                else:
                    conn.execute("UPDATE buckets SET failed_count=failed_count+1 WHERE id=?", (row["id"],))
            else:
                conn.execute("UPDATE buckets SET failed_count=0 WHERE id=?", (row["id"],))
        report.append({"label": row["label"], "id": row["id"], **result})
    return report


async def _check_all_async():
    return await asyncio.to_thread(_check_all_sync)


@app.post("/api/buckets/check_all")
async def check_all(_=Depends(auth)):
    return await _check_all_async()


@app.on_event("startup")
async def tune_threadpool():
    try:
        from anyio.to_thread import current_default_thread_limiter

        current_default_thread_limiter().total_tokens = THREADPOOL_TOKENS
    except Exception:
        pass


@app.on_event("startup")
async def scheduled_health_check():
    if CHECK_INTERVAL_MIN <= 0:
        return

    async def runner():
        while True:
            await asyncio.sleep(CHECK_INTERVAL_MIN * 60)
            try:
                await _check_all_async()
            except Exception:
                pass

    asyncio.create_task(runner())


@app.get("/api/buckets")
def list_buckets(_=Depends(auth)):
    with db() as conn:
        rows = conn.execute(
            """SELECT b.*, COALESCE((SELECT SUM(size) FROM images WHERE bucket_id=b.id),0) AS used
               FROM buckets b ORDER BY b.id"""
        ).fetchall()
        counts = {
            r["bucket_id"]: {"count": r["n"]}
            for r in conn.execute(
                "SELECT bucket_id, COUNT(*) n FROM images GROUP BY bucket_id"
            ).fetchall()
        }
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "account_id_masked": ("*" + r["account_id"][-4:]) if len(r["account_id"]) >= 4 else "",
            "access_key_id_masked": ("*" + r["access_key_id"][-4:]) if len(r["access_key_id"]) >= 4 else "",
            "bucket": r["bucket"],
            "endpoint": r["endpoint"],
            "public_base_url": r["public_base_url"],
            "path_style": bool(r["path_style"]),
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
            "failed_count": r["failed_count"],
            "last_checked": r["last_checked"],
            "last_status": r["last_status"],
            "platform_count": counts.get(r["id"], {}).get("count", 0),
            "platform_bytes": r["used"],
        }
        for r in rows
    ]


@app.post("/api/buckets")
def add_bucket(b: BucketIn, _=Depends(auth)):
    b.label = b.label.strip()
    if not b.label or not b.access_key_id or not b.secret_access_key or not b.bucket:
        raise HTTPException(400, "label / access_key_id / secret_access_key / bucket 必填")
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM buckets WHERE label=?", (b.label,)).fetchone()
        if exists:
            raise HTTPException(409, f"名称 {b.label} 已存在")
        bid = insert_returning_id(
            conn,
            """INSERT INTO buckets(label,account_id,access_key_id,secret_access_key,bucket,
               endpoint,public_base_url,path_style,enabled)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                b.label,
                b.account_id.strip(),
                b.access_key_id.strip(),
                b.secret_access_key.strip(),
                b.bucket.strip(),
                b.endpoint.strip(),
                b.public_base_url.strip().rstrip("/"),
                1 if b.path_style else 0,
                1 if b.enabled else 0,
            ),
        )
    return {"id": bid}


@app.post("/api/buckets/bulk")
def bulk_add(payload: BulkIn, _=Depends(auth)):
    added = []
    skipped = []
    with db() as conn:
        for b in payload.buckets:
            b.label = b.label.strip()
            if not b.label or not b.bucket or not b.access_key_id or not b.secret_access_key:
                skipped.append({"label": b.label, "reason": "缺少必填字段"})
                continue
            if conn.execute("SELECT 1 FROM buckets WHERE label=?", (b.label,)).fetchone():
                skipped.append({"label": b.label, "reason": "名称已存在"})
                continue
            conn.execute(
                """INSERT INTO buckets(label,account_id,access_key_id,secret_access_key,bucket,
                   endpoint,public_base_url,path_style,enabled) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    b.label,
                    b.account_id.strip(),
                    b.access_key_id.strip(),
                    b.secret_access_key.strip(),
                    b.bucket.strip(),
                    b.endpoint.strip(),
                    b.public_base_url.strip().rstrip("/"),
                    1 if b.path_style else 0,
                    1 if b.enabled else 0,
                ),
            )
            added.append(b.label)
    return {"added": added, "skipped": skipped}


@app.put("/api/buckets/{bid}")
def update_bucket(bid: int, b: BucketIn, _=Depends(auth)):
    with db() as conn:
        row = conn.execute("SELECT * FROM buckets WHERE id=?", (bid,)).fetchone()
        if not row:
            raise HTTPException(404, "桶不存在")
        dup = conn.execute("SELECT 1 FROM buckets WHERE label=? AND id!=?", (b.label, bid)).fetchone()
        if dup:
            raise HTTPException(409, f"名称 {b.label} 已存在")
        conn.execute(
            """UPDATE buckets SET label=?, account_id=?, access_key_id=?, secret_access_key=?,
               bucket=?, endpoint=?, public_base_url=?, path_style=?, enabled=? WHERE id=?""",
            (
                b.label.strip(),
                b.account_id.strip() or row["account_id"],
                b.access_key_id.strip() or row["access_key_id"],
                b.secret_access_key.strip() or row["secret_access_key"],
                b.bucket.strip(),
                b.endpoint.strip(),
                b.public_base_url.strip().rstrip("/"),
                1 if b.path_style else 0,
                1 if b.enabled else 0,
                bid,
            ),
        )
    return {"ok": True}


@app.delete("/api/buckets/{bid}")
def delete_bucket(bid: int, purge: bool = Query(False), _=Depends(auth)):
    from botocore.exceptions import ClientError

    with db() as conn:
        row = conn.execute("SELECT * FROM buckets WHERE id=?", (bid,)).fetchone()
        if not row:
            raise HTTPException(404, "桶不存在")
        deleted_objects = 0
        if purge:
            client = make_client(row)
            paginator = client.get_paginator("list_objects_v2")
            try:
                for page in paginator.paginate(Bucket=row["bucket"]):
                    objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if objs:
                        client.delete_objects(Bucket=row["bucket"], Delete={"Objects": objs, "Quiet": True})
                        deleted_objects += len(objs)
            except ClientError as e:
                raise HTTPException(502, f"清空远端失败: {e.response.get('Error', {}).get('Code', '')}")
            conn.execute("DELETE FROM images WHERE bucket_id=?", (bid,))
        conn.execute("DELETE FROM buckets WHERE id=?", (bid,))
    return {"ok": True, "purged_objects": deleted_objects}


@app.post("/api/buckets/{bid}/test")
def test_bucket(bid: int, _=Depends(auth)):
    with db() as conn:
        row = conn.execute("SELECT * FROM buckets WHERE id=?", (bid,)).fetchone()
    if not row:
        raise HTTPException(404, "桶不存在")
    return head_bucket(row)


@app.get("/api/buckets/{bid}/stats")
def stats_bucket(bid: int, _=Depends(auth)):
    with db() as conn:
        row = conn.execute("SELECT * FROM buckets WHERE id=?", (bid,)).fetchone()
    if not row:
        raise HTTPException(404, "桶不存在")
    try:
        return bucket_stats(row)
    except Exception as e:
        raise HTTPException(502, f"统计失败: {str(e)[:300]}")


@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    bucket: Optional[str] = Query(None),
    _=Depends(auth_upload),
):
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 限制")
    if not data:
        raise HTTPException(400, "空文件")
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    base = str(request.base_url).rstrip("/")
    return await asyncio.to_thread(
        _do_upload_sync, data, os.path.basename(file.filename or ""), content_type, base, bucket
    )


def _do_upload_sync(
    data: bytes,
    filename: str,
    content_type: str,
    base: str,
    bucket_param: Optional[str],
) -> dict:
    max_bytes = MAX_BUCKET_GB * 1024**3
    with db() as conn:
        try:
            candidates = get_candidates(conn, bucket_param)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        explicit = bool(bucket_param)
        target = None
        key = None
        last_err = ""
        for cand in candidates:
            if not explicit:
                if cand["failed_count"] >= UNHEALTHY_FAILS:
                    continue
                if cand["used"] >= max_bytes:
                    continue
            key = gen_key(filename, content_type)
            try:
                make_client(cand).put_object(Bucket=cand["bucket"], Key=key, Body=data, ContentType=content_type)
            except Exception as e:
                conn.execute("UPDATE buckets SET failed_count=failed_count+1 WHERE id=?", (cand["id"],))
                last_err = f"{cand['label']}: {str(e)[:200]}"
                continue
            conn.execute("UPDATE buckets SET failed_count=0 WHERE id=?", (cand["id"],))
            target = cand
            break
        if target is None:
            detail = (
                f"指定桶 {bucket_param} 上传失败: {last_err}" if explicit else f"所有候选桶均不可用。最后错误: {last_err}"
            )
            raise HTTPException(502, detail)
        image_id = insert_returning_id(
            conn,
            "INSERT INTO images(bucket_id,key,filename,size,content_type) VALUES(?,?,?,?,?)",
            (target["id"], key, filename or os.path.basename(key), len(data), content_type),
        )
    url = direct_url(target, key, base)
    return {
        "id": image_id,
        "url": url,
        "short_url": f"{base}/b/{key}",
        "proxied_url": f"{base}/f/{target['label']}/{key}",
        "bucket": target["label"],
        "key": key,
        "size": len(data),
        "filename": filename,
    }


class PresignIn(BaseModel):
    filename: str = ""
    size: int = 0
    content_type: str = ""
    bucket: Optional[str] = None


@app.post("/api/presign")
def presign(p: PresignIn, request: Request, _=Depends(auth_upload)):
    if p.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 限制")
    base = str(request.base_url).rstrip("/")
    content_type = p.content_type or mimetypes.guess_type(p.filename)[0] or ""
    max_bytes = MAX_BUCKET_GB * 1024**3
    with db() as conn:
        try:
            candidates = get_candidates(conn, p.bucket)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        target = None
        for cand in candidates:
            if cand["failed_count"] >= UNHEALTHY_FAILS:
                continue
            if cand["used"] + max(p.size, 0) >= max_bytes:
                continue
            target = cand
            break
        if target is None:
            raise HTTPException(502, "没有可用容量的桶，请接入新账号")
        key = gen_key(p.filename, content_type)
        params: dict = {"Bucket": target["bucket"], "Key": key}
        if content_type:
            params["ContentType"] = content_type
        try:
            upload_url = make_client(target).generate_presigned_url("put_object", Params=params, ExpiresIn=900)
        except Exception as e:
            import logging

            logging.getLogger("uvicorn.error").warning(f"presign failed bucket={target['label']}: {e!r}")
            raise HTTPException(502, f"生成签名失败: {str(e)[:300]}")
        image_id = insert_returning_id(
            conn,
            "INSERT INTO images(bucket_id,key,filename,size,content_type) VALUES(?,?,?,?,?)",
            (target["id"], key, os.path.basename(p.filename or key), max(p.size, 0), content_type),
        )
    url = direct_url(target, key, base)
    return {
        "id": image_id,
        "upload_url": upload_url,
        "expires_in": 900,
        "method": "PUT",
        "url": url,
        "short_url": f"{base}/b/{key}",
        "bucket": target["label"],
        "key": key,
    }


class PresignConfirmIn(BaseModel):
    key: str
    size: int = 0


@app.post("/api/presign/confirm")
def presign_confirm(p: PresignConfirmIn, _=Depends(auth_upload)):
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM images WHERE key=? ORDER BY id DESC LIMIT 1", (p.key,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        if p.size > 0:
            conn.execute("UPDATE images SET size=? WHERE id=?", (p.size, row["id"]))
    return {"ok": True}


@app.get("/api/images")
def list_images(
    bucket: Optional[str] = None,
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    _=Depends(auth),
):
    where = ""
    params: list = []
    if bucket:
        where = "WHERE b.label=? OR CAST(b.id AS TEXT)=?"
        params = [bucket, bucket]
    with db() as conn:
        rows = conn.execute(
            f"""SELECT i.id, i.key, i.filename, i.size, i.content_type, i.created_at,
                       b.label AS bucket_label, b.public_base_url
                FROM images i JOIN buckets b ON b.id=i.bucket_id
                {where} ORDER BY i.id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) n FROM images i JOIN buckets b ON b.id=i.bucket_id {where}", params
        ).fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


@app.delete("/api/images/{image_id}")
def delete_image(image_id: int, _=Depends(auth)):
    with db() as conn:
        row = conn.execute(
            """SELECT i.*, b.bucket AS bucket_name FROM images i
               JOIN buckets b ON b.id=i.bucket_id WHERE i.id=?""",
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        target = conn.execute("SELECT * FROM buckets WHERE id=?", (row["bucket_id"],)).fetchone()
        try:
            make_client(target).delete_object(Bucket=target["bucket"], Key=row["key"])
        except Exception as e:
            raise HTTPException(502, f"删除远端对象失败: {str(e)[:300]}")
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    return {"ok": True}


@app.api_route("/b/{key:path}", methods=["GET", "HEAD"])
def short_link(key: str):
    with db() as conn:
        row = conn.execute(
            """SELECT i.key AS ikey, b.id AS bid, b.public_base_url AS pub
               FROM images i JOIN buckets b ON b.id=i.bucket_id WHERE i.key=? ORDER BY i.id DESC LIMIT 1""",
            (key,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "文件不存在")
    pub = (row["pub"] or "").rstrip("/")
    if pub:
        return RedirectResponse(
            url=f"{pub}/{row['ikey']}",
            status_code=302,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    with db() as conn:
        b = conn.execute("SELECT * FROM buckets WHERE id=?", (row["bid"],)).fetchone()
    try:
        obj = make_client(b).get_object(Bucket=b["bucket"], Key=row["ikey"])
    except Exception:
        raise HTTPException(404, "对象不存在或不可访问")
    ct = obj.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
    return StreamingResponse(
        obj["Body"].iter_chunks(65536),
        media_type=ct,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(obj["ContentLength"]),
        },
    )


@app.api_route("/f/{label}/{key:path}", methods=["GET", "HEAD"])
def fetch(label: str, key: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM buckets WHERE label=? OR CAST(id AS TEXT)=?", (label, label)).fetchone()
    if not row:
        raise HTTPException(404, "桶不存在")
    try:
        obj = make_client(row).get_object(Bucket=row["bucket"], Key=key)
    except Exception:
        raise HTTPException(404, "对象不存在或不可访问")
    ct = obj.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
    return StreamingResponse(
        obj["Body"].iter_chunks(65536),
        media_type=ct,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(obj["ContentLength"]),
        },
    )


@app.get("/docs")
def docs_page():
    from fastapi.responses import FileResponse

    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "docs.html"), media_type="text/html")


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    workers = int(os.environ.get("WORKERS", "1"))
    port = int(os.environ.get("PORT", "8100"))
    if workers > 1:
        uvicorn.run("main:app", host="0.0.0.0", port=port, workers=workers)
    else:
        uvicorn.run(app, host="0.0.0.0", port=port)
