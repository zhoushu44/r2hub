import json
import urllib.request

B = "http://127.0.0.1:8100"


def req(m, p):
    r = urllib.request.Request(B + p, method=m, headers={"Authorization": "Bearer zs1236547"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())


n = 0
for bk in ("minio-test", "minio-test2"):
    while True:
        items = req("GET", f"/api/images?bucket={bk}&limit=500")["items"]
        if not items:
            break
        for it in items:
            req("DELETE", f"/api/images/{it['id']}")
            n += 1
print("清理残留压测文件:", n)
lst = req("GET", "/api/buckets")
for x in lst:
    print(f"  {x['label']:14s} enabled={x['enabled']} failed={x['failed_count']} files={x['platform_count']}")
