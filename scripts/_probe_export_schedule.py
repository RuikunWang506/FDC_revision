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

for chunk in chunks:
    js = requests.get(base + chunk, timeout=20).text
    if "get-export-schedule" in js or "exportRow" in js or "expiresHour" in js:
        print("===", chunk)
        for pat in ("get-export-schedule", "exportRow", "expiresHour", "hours", "order/export"):
            for m in re.finditer(re.escape(pat), js):
                print(pat, js[max(0, m.start() - 60) : m.end() + 100][:180])

s = fs._build_cookie_session(fs._load_cookie_cache())

# try export with exportType order and poll get-export-schedule
payload = {
    "exportType": "order",
    "exportTemplateId": -1,
    "platform": "",
    "shopIdList": [],
    "warehouseId": "",
    "logisticsServiceId": "",
    "itemType": "",
    "printStatus": "",
    "shippingMethod": [],
    "searchType": "orderNo",
    "searchMethod": "exact",
    "searchValue": "",
    "noteQuery": "",
    "itemMin": "",
    "itemMax": "",
    "weightMin": "",
    "weightMax": "",
    "amountMax": "",
    "amountMin": "",
    "weightUnit": "lb",
    "lengthMin": "",
    "lengthMax": "",
    "widthMin": "",
    "widthMax": "",
    "heightMin": "",
    "heightMax": "",
    "dimensionUnit": "in",
    "expiresHour": "",
    "hours": "",
    "bundled": "",
    "platformStatus": "",
    "shipFromAddressId": "",
    "countryCode": "",
    "countryName": "",
    "country": "",
    "state": "",
    "buyerNote": "",
    "sellerNote": "",
    "internalNote": "",
    "tagQuery": "",
    "platformMarker": "",
    "dateQueryCreateStartTime": "",
    "dateQueryCreateEndTime": "",
    "dateQueryPaidStartTime": "",
    "dateQueryPaidEndTime": "",
    "dateQueryShipStartTime": "",
    "dateQueryShipEndTime": "",
    "dateQueryPrintLabelStartTime": "",
    "dateQueryPrintLabelEndTime": "",
    "dateQueryCancelStartTime": "",
    "dateQueryCancelEndTime": "",
    "logisticsServiceMap": [""],
    "tagIdList": [],
    "pageCurrent": 1,
    "allOrderStatus": "",
    "orderStatus": "all",
    "exceptionType": "",
    "orderBy": "orderPaidTime",
    "desc": "desc",
    "exportRow": 277,
}
r = s.post("https://www.4seller.com/api/v2/order/export", json=payload, timeout=180)
print("\nexport", r.text)
code = r.json().get("data")
import time

for i in range(30):
    rr = s.get(
        "https://www.4seller.com/api/v2/order/get-export-schedule",
        params={"scheduleCode": code},
        timeout=30,
    )
    print(i, rr.text[:250])
    d = rr.json().get("data") or {}
    if d.get("fileUrl"):
        print("DONE", d)
        break
    time.sleep(2)
