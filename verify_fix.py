import concurrent.futures
import json
import time
import collections
import urllib.error
import urllib.request

B = "http://127.0.0.1:8100"


def req(method, path, token=None, json_body=None):
    h = {}
    if token:
        h["Authorization"] = "Bearer " + token
    if json_body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    else:
        data = None
    r = urllib.request.Request(B + path, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


print("健康:", req("GET", "/health")[0])

for label, n, conc in (("presign @50并发", 300, 50), ("presign @100并发", 300, 100)):
    codes = collections.Counter()
    t0 = time.time()

    def one(i):
        s, b = req("POST", "/api/presign", "zs1236547", {"filename": f"v{i}.png"})
        codes[s] += 1
        return s

    with concurrent.futures.ThreadPoolExecutor(conc) as ex:
        list(ex.map(one, range(n)))
    dur = time.time() - t0
    print(f"{label}: {dict(codes)} 耗时{dur:.1f}s 吞吐{n/dur:.0f} rps")

s, b = req("GET", "/api/buckets", "zs1236547")
lst = json.loads(b)
print("桶状态:")
for x in lst:
    print(f"  {x['label']:14s} enabled={x['enabled']} failed={x['failed_count']}")
