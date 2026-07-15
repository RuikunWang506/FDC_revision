import re
import sys

import requests

sys.path.insert(0, r"c:\FDC_FLASK_APP")
import importlib.util

spec = importlib.util.spec_from_file_location("fs", r"c:\FDC_FLASK_APP\py\other\4_seller.py")
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)

main = requests.get(
    "https://4seller-res.meiyunji.net/europe-erp-web-nuxt/2026-05/_erp_assets/B71jLfui.js",
    timeout=60,
).text
chunks = sorted(set(re.findall(r"\./([A-Za-z0-9_-]+\.js)", main)))
base = "https://4seller-res.meiyunji.net/europe-erp-web-nuxt/2026-05/_erp_assets/"

patterns = [
    "expiresHour",
    "get-export-schedule",
    "exportRow",
    "90",
    "order/backend",
    "history",
    "historical",
    "over90",
    "backend",
    "order/count",
    "order/total",
]
for chunk in chunks:
    js = requests.get(base + chunk, timeout=20).text
    hits = [p for p in patterns if p in js]
    if hits:
        print("===", chunk, hits)
        for p in hits:
            for m in re.finditer(re.escape(p), js):
                print(" ", js[max(0, m.start() - 80) : m.end() + 120][:220].replace("\n", " "))

s = fs._build_cookie_session(fs._load_cookie_cache())

# probe order-related endpoints
candidates = [
    ("GET", "https://www.4seller.com/api/v2/order/count", None),
    ("POST", "https://www.4seller.com/api/v2/order/count", {"orderStatus": "all"}),
    ("POST", "https://www.4seller.com/api/v2/order/backend/page", {"pageCurrent": 1, "pageSize": 1, "orderStatus": "all"}),
    ("POST", "https://www.4seller.com/api/v2/order/history/page", {"pageCurrent": 1, "pageSize": 1, "orderStatus": "all"}),
    ("POST", "https://www.4seller.com/api/v2/order/page", {"pageCurrent": 1, "pageSize": 1, "orderStatus": "all", "hours": "2160"}),
    ("POST", "https://www.4seller.com/api/v2/order/page", {"pageCurrent": 1, "pageSize": 1, "orderStatus": "all", "expiresHour": "2160"}),
]
for method, url, body in candidates:
    try:
        if method == "GET":
            r = s.get(url, timeout=30)
        else:
            r = s.post(url, json=body, timeout=30)
        print(f"\n{method} {url} {body}")
        print(r.status_code, r.text[:300])
    except Exception as e:
        print(f"\n{method} {url} ERROR {e}")
