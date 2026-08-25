import json
import urllib.request
import urllib.error
import concurrent.futures
import time
import io
import collections

BASE = "http://127.0.0.1:8100"
ADMIN = "zs1236547"
AH = {"Authorization": "Bearer " + ADMIN}
PNG = open("test.png", "rb").read()

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {name}")
    else:
        failed += 1
        print(f"FAIL *** {name} {extra}")


def req(method, path, token=None, json_body=None, data=None, headers=None, timeout=20):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    if json_body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        body = resp.read()
        try:
            return resp.status, json.loads(body)
        except Exception:
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


print("=" * 30, "1 健康与页面", "=" * 30)
s, b = req("GET", "/health")
check("health 200", s == 200)
s, b = req("GET", "/")
check("管理页 200 含侧边栏/统计卡", s == 200 and b"sideNav" in b and b"statCards" in b)
s, b = req("GET", "/docs")
check("文档页 200 公开", s == 200 and b"cf-setup" in b)

print("=" * 30, "2 鉴权", "=" * 30)
s, _ = req("GET", "/api/me")
check("无Token -> 401", s == 401)
s, _ = req("GET", "/api/me", token="wrong-token")
check("错误Token -> 401", s == 401)
s, b = req("GET", "/api/me", token=ADMIN)
check("管理员Token -> 200", s == 200 and b.get("ok"))
s, _ = req("POST", "/upload")
check("上传无Token -> 401", s == 401)
s, b = req("POST", "/api/presign", token=ADMIN, json_body={})
check("presign 管理员可用(空body)", s in (200, 400))

print("=" * 30, "3 桶 CRUD", "=" * 30)
s, lst = req("GET", "/api/buckets", token=ADMIN)
before = {x["id"] for x in lst}
check("桶列表可读", s == 200 and isinstance(lst, list))
real_id = next((x["id"] for x in lst if x["label"] == "r2-account01"), None)
minio_ids = [x["id"] for x in lst if x["label"].startswith("minio-test")]
check("真实R2桶存在", real_id is not None)
check("MinIO测试桶>=2个", len(minio_ids) >= 2)
for x in lst:
    if x["label"].startswith(("t-", "dbg")):
        req("DELETE", f"/api/buckets/{x['id']}", token=ADMIN)

s, b = req("POST", "/api/buckets", token=ADMIN,
           json_body={"label": "t-full", "account_id": "", "access_key_id": "minioadmin",
                      "secret_access_key": "minioadmin", "bucket": "test-bucket",
                      "endpoint": "http://127.0.0.1:9000"})
check("添加桶", s == 200 and b.get("id", 0) > 0)
tid = b["id"]
s, b = req("POST", "/api/buckets", token=ADMIN,
           json_body={"label": "t-full", "access_key_id": "x", "secret_access_key": "y", "bucket": "z"})
check("重名标签 -> 409", s == 409)
s, b = req("POST", "/api/buckets", token=ADMIN, json_body={"label": "", "access_key_id": "x", "secret_access_key": "y", "bucket": "z"})
check("缺字段 -> 400", s == 400)
s, b = req("PUT", f"/api/buckets/{tid}", token=ADMIN,
           json_body={"label": "t-full", "access_key_id": "", "secret_access_key": "",
                      "bucket": "test-bucket", "endpoint": "http://127.0.0.1:9000",
                      "path_style": True, "enabled": True})
check("编辑留空保留原密钥", s == 200)
s, b = req("POST", f"/api/buckets/{tid}/test", token=ADMIN)
check("编辑后连通测试 ok", s == 200 and b.get("ok"))
s, b = req("GET", f"/api/buckets/{tid}/stats", token=ADMIN)
check("远端统计", s == 200 and "objects" in b)
s, b = req("POST", "/api/buckets/99999/test", token=ADMIN)
check("不存在桶 -> 404", s == 404)

print("=" * 30, "4 上传链路", "=" * 30)


def multipart(field, filename, content):
    boundary = "----x9"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode() + content + \
           (f"\r\n--{boundary}--\r\n").encode()
    return body, "multipart/form-data; boundary=" + boundary


body, ct = multipart("file", "t.png", PNG)
s, b = req("POST", "/upload?bucket=t-full", token=ADMIN, data=body, headers={"Content-Type": ct})
check("显式指定MinIO桶上传", s == 200 and b.get("bucket") == "t-full")
proxy_key = b["key"]

s, b = req("POST", "/upload?bucket=r2-account01", token=ADMIN, data=body, headers={"Content-Type": ct})
check("显式上传到真实R2桶", s == 200 and b.get("bucket") == "r2-account01")
real_key = b["key"]
real_short = b["short_url"]

s, raw = req("GET", f"/f/t-full/{proxy_key}")
check("代理下载字节一致", s == 200 and raw == PNG)
s, _ = req("GET", f"/f/no-such-bucket/{proxy_key}")
check("未知桶代理 -> 404", s == 404)

big = b"\x00" * (51 * 1024 * 1024)
body, ct = multipart("file", "big.bin", big)
s, _ = req("POST", "/upload", token=ADMIN, data=body, headers={"Content-Type": ct}, timeout=60)
check("51MB超限 -> 413", s == 413)

body, ct = multipart("file", "empty.png", b"")
s, _ = req("POST", "/upload", token=ADMIN, data=body, headers={"Content-Type": ct})
check("空文件 -> 400", s == 400)

s, b = req("POST", "/upload?bucket=no-exist", token=ADMIN, data=multipart("file", "x.png", PNG)[0],
           headers={"Content-Type": multipart("file", "x.png", PNG)[1]})
check("指定不存在的桶 -> 400", s == 400)

print("=" * 30, "5 预签名直传", "=" * 30)
s, p = req("POST", "/api/presign", token=ADMIN,
           json_body={"filename": "direct-t.png", "size": len(PNG), "content_type": "image/png",
                      "bucket": "t-full"})
check("presign 返回签名", s == 200 and p.get("upload_url", "").startswith("http"))
r = urllib.request.Request(p["upload_url"], data=PNG, method="PUT", headers={"Content-Type": "image/png"})
try:
    resp = urllib.request.urlopen(r, timeout=30)
    put_status = resp.status
except Exception as e:
    put_status = getattr(e, "code", 0)
check("直传 PUT -> 200", put_status == 200)
s, b = req("POST", "/api/presign/confirm", token=ADMIN, json_body={"key": p["key"], "size": len(PNG)})
check("confirm 回报大小", s == 200)
s, raw = req("GET", f"/b/{p['key']}")
check("直传对象短链代理可取回", s == 200 and raw == PNG)

print("=" * 30, "6 统一短链", "=" * 30)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


s, hdr_status, hdr_loc = None, None, None
r = urllib.request.Request(BASE + f"/b/{real_key}", method="GET")
try:
    op = urllib.request.build_opener(NoRedirect())
    resp = op.open(r, timeout=20)
    s = resp.status
except urllib.error.HTTPError as e:
    s = e.code
    hdr_loc = e.headers.get("Location")
check("真实桶短链 -> 302", s == 302)
check("302 Location指向r2.dev", (hdr_loc or "").startswith("https://pub-"))
r = urllib.request.Request(BASE + f"/b/{real_key}", method="HEAD")
try:
    op = urllib.request.build_opener(NoRedirect())
    resp = op.open(r, timeout=20)
    s = resp.status
except urllib.error.HTTPError as e:
    s = e.code
check("HEAD短链 -> 302", s == 302)
s, _ = req("GET", "/b/images/not/exist.png")
check("不存在的key -> 404", s == 404)

print("=" * 30, "7 记录管理", "=" * 30)
s, b = req("GET", "/api/images?limit=5&offset=0", token=ADMIN)
check("记录分页列表结构正确", s == 200 and isinstance(b.get("total"), int) and len(b.get("items", [])) <= 5)
bdy, cth = multipart("file", "del-selftest.png", PNG)
s, b = req("POST", "/upload?bucket=minio-test", token=ADMIN, data=bdy, headers={"Content-Type": cth})
target = {"id": b["id"], "key": b["key"]} if s == 200 else None
if target:
    s, b = req("DELETE", f"/api/images/{target['id']}", token=ADMIN)
    check("删除图片接口", s == 200)
    s, _ = req("GET", f"/f/minio-test/{target['key']}")
    check("删除后远端对象404", s == 404)
else:
    check("找到可删的测试记录", False)

print("=" * 30, "8 批量导入 / 巡检 / 故障自愈", "=" * 30)
s, lst = req("GET", "/api/buckets", token=ADMIN)
for x in lst:
    if x["label"] in ("t-bulk-a", "t-dead"):
        req("DELETE", f"/api/buckets/{x['id']}", token=ADMIN)
s, b = req("POST", "/api/buckets/bulk", token=ADMIN, json_body={"buckets": [
    {"label": "t-bulk-a", "account_id": "", "access_key_id": "minioadmin", "secret_access_key": "minioadmin",
     "bucket": "test-bucket2", "endpoint": "http://127.0.0.1:9000"},
    {"label": "t-bulk-a", "account_id": "", "access_key_id": "minioadmin", "secret_access_key": "minioadmin",
     "bucket": "test-bucket2", "endpoint": "http://127.0.0.1:9000"},
]})
check("批量导入 added/skipped", s == 200 and len(b.get("added")) == 1 and len(b.get("skipped")) == 1)

s, b = req("POST", "/api/buckets/bulk", token=ADMIN, json_body={"buckets": [
    {"label": "t-dead", "account_id": "", "access_key_id": "bad", "secret_access_key": "bad",
     "bucket": "test-bucket", "endpoint": "http://127.0.0.1:9000"}]})
s, lst = req("GET", "/api/buckets", token=ADMIN)
dead = next((x for x in lst if x["label"] == "t-dead"), None)
check("坏凭证桶已导入", dead is not None)

s, rep = req("POST", "/api/buckets/check_all", token=ADMIN)
dead_rep = next((x for x in rep if x["label"] == "t-dead"), None)
check("巡检检出坏桶 ok=False", dead_rep is not None and dead_rep["ok"] is False)
check("鉴权类失败自动停用", dead_rep is not None and dead_rep.get("disabled") is True)
s, lst = req("GET", "/api/buckets", token=ADMIN)
dead2 = next((x for x in lst if x["label"] == "t-dead"), None)
check("死号 enabled=False", dead2 is not None and dead2["enabled"] is False)

s, body2 = multipart("file", "fo.png", PNG)
bdy, cth = multipart("file", "fo.png", PNG)
s, b = req("POST", "/upload", token=ADMIN, data=bdy, headers={"Content-Type": cth})
check("轮询上传避开停用桶", s == 200 and b.get("bucket") not in ("t-dead",))

print("=" * 30, "9 高并发 20 张", "=" * 30)


def one(i):
    bdy, cth = multipart("file", f"c{i}.png", PNG)
    t0 = time.time()
    s, b = req("POST", "/upload", token=ADMIN, data=bdy, headers={"Content-Type": cth}, timeout=60)
    if isinstance(b, bytes):
        try:
            b = json.loads(b)
        except Exception:
            b = {}
    return s, (b or {}).get("bucket"), time.time() - t0


with concurrent.futures.ThreadPoolExecutor(24) as ex:
    rs = list(ex.map(one, range(20)))
ok_n = sum(1 for s, _, _ in rs if s == 200)
dist = collections.Counter(x[1] for x in rs)
check(f"并发20全部成功 (实际{ok_n})", ok_n == 20)
check("分布≥3个桶", len(dist) >= 3)
print(f"   分布: {dict(dist)} 平均{sum(t for _,_,t in rs)/20:.2f}s 最慢{max(t for _,_,t in rs):.2f}s")

print("=" * 30, "10 清理测试数据", "=" * 30)
s, imgs = req("GET", "/api/images?limit=500", token=ADMIN)
cleaned = 0
for i in imgs.get("items", []):
    if i["bucket_label"] != "r2-account01":
        s2, _ = req("DELETE", f"/api/images/{i['id']}", token=ADMIN)
        if s2 == 200:
            cleaned += 1
check(f"清理MinIO侧测试文件({cleaned}个)", cleaned >= 10)
s, lst = req("GET", "/api/buckets", token=ADMIN)
for x in lst:
    if x["label"] in ("t-full", "t-bulk-a", "t-dead"):
        req("DELETE", f"/api/buckets/{x['id']}", token=ADMIN)
check("清理临时桶配置", True)

print()
print("=" * 60)
print(f"结果: 通过 {passed} / 失败 {failed}")
