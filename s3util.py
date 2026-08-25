import re
import time
import secrets
import threading
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from db import db, get_meta, set_meta

_client_cache = {}
_cache_lock = threading.Lock()


def resolve_endpoint(account_id: str, endpoint: str) -> str:
    if endpoint:
        return endpoint.rstrip("/")
    if account_id:
        return f"https://{account_id}.r2.cloudflarestorage.com"
    raise ValueError("需要提供 account_id 或 endpoint")


def make_client(b) -> "boto3.client":
    cache_key = (
        b["id"],
        b["access_key_id"],
        b["secret_access_key"],
        b["endpoint"],
        b["account_id"],
        bool(b["path_style"]),
    )
    with _cache_lock:
        client = _client_cache.get(cache_key)
        if client is None:
            if len(_client_cache) > 200:
                _client_cache.clear()
            client = boto3.client(
                "s3",
                endpoint_url=resolve_endpoint(b["account_id"], b["endpoint"]),
                aws_access_key_id=b["access_key_id"],
                aws_secret_access_key=b["secret_access_key"],
                region_name="auto",
                config=BotoConfig(
                    s3={"addressing_style": "path" if b["path_style"] else "virtual"},
                    retries={"max_attempts": 2},
                    connect_timeout=10,
                    read_timeout=60,
                    max_pool_connections=50,
                ),
            )
            _client_cache[cache_key] = client
        return client


def head_bucket(b) -> dict:
    try:
        make_client(b).head_bucket(Bucket=b["bucket"])
        return {"ok": True, "message": "连接成功"}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return {"ok": False, "message": f"{status} {code}"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:300]}


def bucket_stats(b, max_pages: int = 200) -> dict:
    client = make_client(b)
    count = 0
    total = 0
    pages = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=b["bucket"]):
        pages += 1
        for obj in page.get("Contents", []):
            count += 1
            total += obj["Size"]
        if pages >= max_pages:
            break
    truncated = pages >= max_pages
    return {"objects": count, "bytes": total, "truncated": truncated}


def gen_key(filename: str, content_type: str) -> str:
    ext = ""
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", filename or "")
    if m:
        ext = "." + m.group(1).lower()
    else:
        guess = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/avif": ".avif",
            "image/svg+xml": ".svg",
            "video/mp4": ".mp4",
        }.get(content_type, "")
        ext = guess
    day = time.strftime("%Y%m%d")
    name = f"{int(time.time() * 1000):x}{secrets.token_hex(4)}{ext}"
    return f"images/{day}/{name}"


def get_candidates(conn, explicit: Optional[str] = None):
    rows = conn.execute(
        """SELECT b.*, COALESCE((SELECT SUM(size) FROM images WHERE bucket_id=b.id),0) AS used
           FROM buckets b WHERE b.enabled=1 ORDER BY b.failed_count, b.id"""
    ).fetchall()
    if not rows:
        raise RuntimeError("没有已启用的桶，请先在后台添加")
    if explicit:
        matched = [r for r in rows if r["label"] == explicit or str(r["id"]) == explicit]
        if not matched:
            raise RuntimeError(f"桶 {explicit} 不存在或未启用")
        return matched
    row = conn.execute(
        "UPDATE meta SET v=CAST(v AS INTEGER)+1 WHERE k='rr' RETURNING v"
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(k,v) VALUES('rr','1')")
        start = 0
    else:
        start = int(row["v"] if isinstance(row, dict) else row[0])
    n = len(rows)
    return [rows[(start + i) % n] for i in range(n)]


def direct_url(b, key: str, base_url: str) -> str:
    pub = (b["public_base_url"] or "").rstrip("/")
    if pub:
        return f"{pub}/{key}"
    label = b["label"]
    return f"{base_url.rstrip('/')}/f/{label}/{key}"
