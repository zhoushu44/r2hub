import concurrent.futures
import json
import time
import collections
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8100"
ADMIN = "zs1236547"
PNG = open("test.png", "rb").read()


class _NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_NR())


def req(method, path, token=None, json_body=None, data=None, headers=None, timeout=120):
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
    boundary = "----uh"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode() + PNG + \
           (f"\r\n--{boundary}--\r\n").encode()
    return body, "multipart/form-data; boundary=" + boundary


report = []


def phase(name, fn, n, conc):
    lat = []
    codes = collections.Counter()
    errs = []
    t0 = time.time()

    def task(i):
        try:
            s, dt, err = fn(i)
        except Exception as e:
            s, dt, err = -1, 0.0, f"{type(e).__name__}: {str(e)[:60]}"
        lat.append(dt)
        codes[s] += 1
        if s >= 500 or s == -1:
            errs.append((i, s, str(err)[:80]))
    with concurrent.futures.ThreadPoolExecutor(conc) as ex:
        list(ex.map(task, range(n)))
    dur = time.time() - t0
    lat.sort()
    n_ = len(lat)
    p50 = lat[n_ // 2] * 1000
    p95 = lat[int(n_ * .95)] * 1000
    p99 = lat[int(n_ * .99)] * 1000
    mx = lat[-1] * 1000
    ok = codes.get(200, 0) + codes.get(302, 0)
    print(f"\n{name}")
    print(f"  并发={conc} 总数={n_} 成功={ok} 失败={n_ - ok} 耗时={dur:.1f}s 吞吐={n_ / dur:.1f} req/s")
    print(f"  延迟 p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms max={mx:.0f}ms")
    print(f"  状态码={dict(codes)}")
    if errs:
        print(f"  5xx样本: {errs[:3]}")
    report.append({"phase": name, "n": n_, "ok": ok, "fail": n_ - ok, "rps": round(n_ / dur, 1),
                   "p50": round(p50), "p95": round(p95)})


def upload_to(i):
    bucket = ("minio-test", "minio-test2")[i % 2]
    body, ct = multipart(f"u{i}.png")
    t0 = time.time()
    s, b = req("POST", f"/upload?bucket={bucket}", token=ADMIN, data=body,
               headers={"Content-Type": ct})
    return s, time.time() - t0, b[:120] if s >= 500 else ""


def read_short(i):
    t0 = time.time()
    s, _ = req("GET", f"/b/{REAL_KEY}", timeout=30)
    return s, time.time() - t0, ""


def read_proxy(i):
    t0 = time.time()
    s, raw = req("GET", f"/f/minio-test/{PROXY_KEY}", timeout=30)
    return s, time.time() - t0, ""


def presign_only(i):
    t0 = time.time()
    s, b = req("POST", "/api/presign", token=ADMIN,
               json_body={"filename": f"x{i}.png"}, timeout=120)
    return s, time.time() - t0, b[:120] if s >= 500 else ""


def mixed(i):
    m = i % 20
    if m < 14:
        return read_short(i)
    elif m < 19:
        return upload_to(i)
    else:
        t0 = time.time()
        s, _ = req("GET", "/api/buckets", token=ADMIN, timeout=30)
        return s, time.time() - t0, ""


print("=" * 64)
print("R2 Hub 超高并发压力测试")
print("=" * 64)

s, b = req("POST", "/upload?bucket=r2-account01", token=ADMIN,
           data=multipart("ultra-read.png")[0], headers={"Content-Type": multipart("x")[1]})
REAL_KEY = json.loads(b)["key"]
s, b = req("POST", "/upload?bucket=minio-test", token=ADMIN,
           data=multipart("ultra-proxy.png")[0], headers={"Content-Type": multipart("x")[1]})
PROXY_KEY = json.loads(b)["key"]
print(f"预热完成 read_key={REAL_KEY[:24]}... proxy_key={PROXY_KEY[:24]}...")

phase("R1 短链跳转 /b/", read_short, 3000, 300)
phase("R2 代理下载 /f/ 完整字节", read_proxy, 1200, 150)
phase("W1 multipart 上传", upload_to, 600, 100)
phase("W2 预签名生成(纯签名)", presign_only, 300, 50)
phase("MIX 混合负载 70读/25写/5管理", mixed, 2400, 200)

tot = sum(r["n"] for r in report)
ok = sum(r["ok"] for r in report)
fl = sum(r["fail"] for r in report)
print("\n" + "=" * 64)
print(f"总计 {tot} 请求 | 成功 {ok} | 失败 {fl} ({fl / tot * 100:.2f}%)")
avg_rps = sum(r["rps"] for r in report) / len(report)
print(json.dumps(report, ensure_ascii=False, indent=1))

n = 0
for bk in ("minio-test", "minio-test2"):
    while True:
        s, b = req("GET", f"/api/images?bucket={bk}&limit=500&offset=0", token=ADMIN)
        items = json.loads(b).get("items", [])
        if not items:
            break
        for it in items:
            req("DELETE", f"/api/images/{it['id']}", token=ADMIN)
            n += 1
print(f"清理压测文件 {n} 个")

with open("ultra_result.json", "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
