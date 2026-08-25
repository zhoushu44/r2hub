import concurrent.futures
import json
import time
import collections
import statistics
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8100"
ADMIN = "zs1236547"
PNG = open("test.png", "rb").read()
results = []


class _NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_NR())


def req(method, path, token=None, json_body=None, data=None, headers=None, timeout=90):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    if json_body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        resp = OPENER.open(r, timeout=timeout)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def multipart(filename):
    boundary = "----st"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode() + PNG + \
           (f"\r\n--{boundary}--\r\n").encode()
    return body, "multipart/form-data; boundary=" + boundary


def phase(name, fn, n, conc):
    lat = []
    codes = collections.Counter()
    t0 = time.time()

    def task(i):
        s, dt = fn(i)
        lat.append(dt)
        codes[s] += 1
    with concurrent.futures.ThreadPoolExecutor(conc) as ex:
        list(ex.map(task, range(n)))
    dur = time.time() - t0
    lat.sort()
    p50 = lat[len(lat) // 2] * 1000
    p95 = lat[int(len(lat) * .95)] * 1000
    mx = lat[-1] * 1000
    ok = codes.get(200, 0) + codes.get(302, 0)
    print(f"{name}\n  并发={conc} 总数={n} 成功={ok} 失败={n - ok} 耗时={dur:.1f}s 吞吐={n / dur:.1f} req/s")
    print(f"  延迟 p50={p50:.0f}ms p95={p95:.0f}ms max={mx:.0f}ms 状态码={dict(codes)}")
    results.append((name, n, ok, n - ok, dur))


print("=" * 62)
print("阶段0 基准单请求")
t0 = time.time()
for _ in range(5):
    req("GET", "/health")
print(f"  /health 平均 {(time.time() - t0) / 5 * 1000:.1f}ms")

print("=" * 62)
print("阶段1 读压力: 统一短链 /b/ 302跳转 x600 @100并发")
s, b = req("POST", "/upload?bucket=r2-account01", token=ADMIN,
           data=multipart("read-target.png")[0], headers={"Content-Type": multipart("x")[1]})
real_key = json.loads(b)["key"]


def do_read(i):
    t0 = time.time()
    s, _ = req("GET", f"/b/{real_key}", timeout=30)
    return s, time.time() - t0


phase("短链302跳转", do_read, 600, 100)

print("=" * 62)
print("阶段2 读压力: 代理下载 /f/ (流式过VPS) x300 @50并发")
s, b = req("POST", "/upload?bucket=minio-test", token=ADMIN,
           data=multipart("proxy-target.png")[0], headers={"Content-Type": multipart("x")[1]})
proxy_key = json.loads(b)["key"]


def do_proxy(i):
    t0 = time.time()
    s, raw = req("GET", f"/f/minio-test/{proxy_key}", timeout=30)
    assert raw == PNG or s != 200
    return s, time.time() - t0


phase("代理下载(完整字节)", do_proxy, 300, 50)

print("=" * 62)
print("阶段3 写压力: multipart上传 x200 @50并发 (MinIO两桶轮换)")
up_keys = []


def do_upload(i):
    bucket = "minio-test" if i % 2 == 0 else "minio-test2"
    body, ct = multipart(f"s{i}.png")
    t0 = time.time()
    s, b = req("POST", f"/upload?bucket={bucket}", token=ADMIN, data=body,
               headers={"Content-Type": ct}, timeout=120)
    if s == 200:
        up_keys.append((bucket, json.loads(b)["id"], b""))
    return s, time.time() - t0


phase("上传(MinIO)", do_upload, 200, 50)

print("=" * 62)
print("阶段4 写压力: 真实R2直传链路 presign+PUT x30 @15并发")


def do_presign_real(i):
    t0 = time.time()
    s, b = req("POST", "/api/presign", token=ADMIN,
               json_body={"filename": f"ps{i}.png", "size": len(PNG),
                          "content_type": "image/png"}, timeout=60)
    if s != 200:
        return s, time.time() - t0
    p = json.loads(b)
    r = urllib.request.Request(p["upload_url"], data=PNG, method="PUT",
                               headers={"Content-Type": "image/png"})
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        s2 = resp.status
        up_keys.append(("r2-account01", p["id"], b""))
    except Exception as e:
        s2 = getattr(e, "code", 0)
    return s2, time.time() - t0


phase("presign签名+R2直传(含真实网络)", do_presign_real, 30, 15)

print("=" * 62)
print("阶段5 混合负载 @60并发 x450 (读70%/写25%/管理5%)")


def do_mixed(i):
    m = i % 20
    if m < 14:
        return do_read(i)
    elif m < 19:
        return do_upload(i)
    else:
        t0 = time.time()
        s, _ = req("GET", "/api/buckets", token=ADMIN, timeout=30)
        return s, time.time() - t0


phase("混合读写", do_mixed, 450, 60)

print("=" * 62)
tot = sum(r[1] for r in results)
ok = sum(r[2] for r in results)
fl = sum(r[3] for r in results)
print(f"总请求 {tot} 成功 {ok} 失败 {fl}")
print(f"失败明细: {[r[0] for r in results if r[3] > 0] or '无'}")

print("=" * 62)
print("清理压测数据...")
cleaned = 0
for bucket in ("minio-test", "minio-test2"):
    off = 0
    while True:
        s, b = req("GET", f"/api/images?bucket={bucket}&limit=500&offset=0", token=ADMIN)
        items = json.loads(b) if isinstance(b, bytes) else b
        items = items.get("items", [])
        if not items:
            break
        for it in items:
            s2, _ = req("DELETE", f"/api/images/{it['id']}", token=ADMIN)
            cleaned += 1 if s2 == 200 else 0
print(f"已清理 {cleaned} 个测试文件")

with open("stress_result.json", "w") as f:
    json.dump({"phases": [(r[0], r[1], r[2], round(r[4], 1)) for r in results],
               "total": tot, "ok": ok, "fail": fl}, f, ensure_ascii=False, indent=1)
print("结果已存 stress_result.json")
