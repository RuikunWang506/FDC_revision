"""GOFO coverage helpers: Open API (primary) + Excel ZIP lookup (fallback)."""
from __future__ import annotations

import base64
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from py.utilities.nssm_env import load_nssm_env_file

DEFAULT_BASE_URL = "https://dmsapi.gofoexpress.com"
VERIFY_PATH = "/open-api/v2/order/verifyDelivery"
COST_PATH = "/open-api/v2/order/costEstimation"

# Parcel reachable ZIP workbook (fallback if API is unavailable)
_PARCEL_ZIP_XLSX_GLOB = "GOFO-Parcel*可达邮编*.xlsx"
_PARCEL_ACTIVE_SHEET = "GOFO_可达邮编"
_PARCEL_CLOSED_SHEET = "待关闭_可达邮编"

# Ground reachable ZIP workbook (Fashion Ground 报价 / 可达邮编)
_GROUND_ZIP_XLSX_GLOBS = ("*GOFO-Ground*.xlsx", "*GOFO*Ground*.xlsx")
_GROUND_ACTIVE_SHEET = "可达邮编"
_GROUND_DEFAULT_PORT = "LAX"

# Product codes from GOFO对接接口文档v260326.pdf
PRODUCTS = (
    {"key": "parcel", "label": "GOFO Parcel", "product_code": "GOFO Parcel Pickup"},
    {"key": "ground", "label": "GOFO Ground", "product_code": "GOFO Ground Pickup"},
)

# Warehouse shipper stubs for costEstimation (required by API; not shown in UI)
_DEFAULT_SHIPPER = {
    "shipperName": "FDC",
    "shipperPhone": "3235550100",
    "shipperCountry": "US",
    "shipperState": "CA",
    "shipperCity": "Vernon",
    "shipperStreet": "17788 Rowland Street",
    "shipperCode": "90058",
}

_zip_cache: dict[str, Any] = {
    "mtime": None,
    "path": None,
    "active": {},
    "closed": {},
    "loaded_at": 0.0,
}

_QUOTE_WORKERS = 8
_QUOTE_CACHE_TTL_SEC = 180.0
_CRED_CACHE_TTL_SEC = 60.0
_tls = threading.local()
_cred_cache: dict[str, Any] = {"loaded_at": 0.0, "base": "", "user": "", "password": ""}
_quote_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_quote_cache_lock = threading.Lock()
_ground_zip_cache: dict[str, Any] = {
    "mtime": None,
    "path": None,
    "zips": {},
    "loaded_at": 0.0,
}


def _credentials() -> tuple[str, str, str]:
    now = time.time()
    if (
        _cred_cache["user"]
        and _cred_cache["password"]
        and now - float(_cred_cache["loaded_at"] or 0) < _CRED_CACHE_TTL_SEC
    ):
        return _cred_cache["base"], _cred_cache["user"], _cred_cache["password"]
    load_nssm_env_file(reload_empty=True)
    base = (os.environ.get("GOFO_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    user = (os.environ.get("GOFO_API_ACCOUNT") or "").strip()
    password = (os.environ.get("GOFO_API_PASSWORD") or "").strip()
    _cred_cache.update({"loaded_at": now, "base": base, "user": user, "password": password})
    return base, user, password


def _http_session() -> requests.Session:
    sess = getattr(_tls, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=_QUOTE_WORKERS, pool_maxsize=_QUOTE_WORKERS)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _tls.session = sess
    return sess


def _dim_key(length: float, width: float, height: float, weight: float) -> tuple[float, float, float, float]:
    return (round(float(length), 2), round(float(width), 2), round(float(height), 2), round(float(weight), 3))


def _auth_headers(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def normalize_zip(zip_code: str | None) -> str:
    raw = (zip_code or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 5:
        return digits[:5]
    return raw


def normalize_country(country: str | None) -> str:
    c = (country or "").strip()
    if not c:
        return "US"
    upper = c.upper()
    if upper in ("US", "USA", "UNITED STATES", "UNITED STATES(USA)", "UNITED STATES OF AMERICA"):
        return "US"
    return c


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_parcel_zip_xlsx() -> Path:
    root = _project_root()
    matches = sorted(root.glob(_PARCEL_ZIP_XLSX_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise FileNotFoundError(
            f"GOFO Parcel ZIP Excel not found (expected {_PARCEL_ZIP_XLSX_GLOB} in project root)."
        )
    return matches[0]


def _cell_zip(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        value = int(value)
    if isinstance(value, int):
        return f"{value:05d}"
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 5:
        return digits[:5]
    return text


def _read_zip_sheet(ws, *, data_start_row: int = 4) -> dict[str, dict[str, str]]:
    """Parse sheets where col B is destination ZIP, C region, D state, E city."""
    out: dict[str, dict[str, str]] = {}
    for row in ws.iter_rows(min_row=data_start_row, values_only=True):
        if not row:
            continue
        zip_code = _cell_zip(row[1] if len(row) > 1 else None)
        if not zip_code:
            continue
        out[zip_code] = {
            "region": str(row[2] or "").strip() if len(row) > 2 else "",
            "state": str(row[3] or "").strip() if len(row) > 3 else "",
            "city": str(row[4] or "").strip() if len(row) > 4 else "",
        }
    return out


def _load_parcel_zip_tables() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, str]]:
    """Return (active_zips, closed_zips, meta). Cached by file mtime."""
    path = _find_parcel_zip_xlsx()
    mtime = path.stat().st_mtime
    if (
        _zip_cache["path"] == str(path)
        and _zip_cache["mtime"] == mtime
        and _zip_cache["active"]
    ):
        return _zip_cache["active"], _zip_cache["closed"], {"path_name": path.name}

    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if _PARCEL_ACTIVE_SHEET not in wb.sheetnames:
            raise FileNotFoundError(f"Sheet {_PARCEL_ACTIVE_SHEET!r} missing in {path.name}")
        active = _read_zip_sheet(wb[_PARCEL_ACTIVE_SHEET])
        closed: dict[str, dict[str, str]] = {}
        if _PARCEL_CLOSED_SHEET in wb.sheetnames:
            closed = _read_zip_sheet(wb[_PARCEL_CLOSED_SHEET])
    finally:
        wb.close()

    _zip_cache["path"] = str(path)
    _zip_cache["mtime"] = mtime
    _zip_cache["active"] = active
    _zip_cache["closed"] = closed
    _zip_cache["loaded_at"] = time.time()
    return active, closed, {"path_name": path.name}


def _find_ground_zip_xlsx() -> Path:
    root = _project_root()
    found: dict[Path, Path] = {}
    for pattern in _GROUND_ZIP_XLSX_GLOBS:
        for path in root.glob(pattern):
            found[path.resolve()] = path
    matches = sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise FileNotFoundError(
            f"GOFO Ground ZIP Excel not found (expected {_GROUND_ZIP_XLSX_GLOBS} in project root)."
        )
    return matches[0]


def _read_ground_zip_sheet(ws) -> dict[str, dict[str, Any]]:
    """Parse Ground 可达邮编: col B ZIP, C region, D state, E city, F+ injection-port zones."""
    rows = ws.iter_rows(min_row=2, values_only=True)
    next(rows, None)  # header labels
    port_row = next(rows, None)
    ports: list[tuple[int, str]] = []
    known_ports = {
        "LAX", "EWR", "SFO", "SAV", "ATL", "ORD", "IAH",
        "PHX", "DFW", "JFK", "MIA", "MCO", "SEA", "CLT", "CVG",
    }
    if port_row:
        for idx, val in enumerate(port_row):
            name = str(val or "").strip().upper()
            if name in known_ports:
                ports.append((idx, name))

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row:
            continue
        zip_code = _cell_zip(row[1] if len(row) > 1 else None)
        if not zip_code:
            continue
        zones: dict[str, str] = {}
        for idx, port in ports:
            if idx < len(row) and row[idx] not in (None, ""):
                zones[port] = str(row[idx]).strip()
        remote = str(row[12] if len(row) > 12 and row[12] is not None else "").strip()
        out[zip_code] = {
            "region": str(row[2] or "").strip() if len(row) > 2 else "",
            "state": str(row[3] or "").strip() if len(row) > 3 else "",
            "city": str(row[4] or "").strip() if len(row) > 4 else "",
            "zones": zones,
            "remote": remote,
            "note": str(row[14] or "").strip() if len(row) > 14 else "",
        }
    return out


def _load_ground_zip_table() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    path = _find_ground_zip_xlsx()
    mtime = path.stat().st_mtime
    if (
        _ground_zip_cache["path"] == str(path)
        and _ground_zip_cache["mtime"] == mtime
        and _ground_zip_cache["zips"]
    ):
        return _ground_zip_cache["zips"], {"path_name": path.name, "zip_count": str(len(_ground_zip_cache["zips"]))}

    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if _GROUND_ACTIVE_SHEET not in wb.sheetnames:
            raise FileNotFoundError(f"Sheet {_GROUND_ACTIVE_SHEET!r} missing in {path.name}")
        zips = _read_ground_zip_sheet(wb[_GROUND_ACTIVE_SHEET])
    finally:
        wb.close()

    _ground_zip_cache["path"] = str(path)
    _ground_zip_cache["mtime"] = mtime
    _ground_zip_cache["zips"] = zips
    _ground_zip_cache["loaded_at"] = time.time()
    return zips, {"path_name": path.name, "zip_count": str(len(zips))}


def lookup_ground_zip(zip_code: str | None) -> dict[str, Any] | None:
    """Return Ground 可达邮编 row, or None if the workbook is missing."""
    zip_norm = normalize_zip(zip_code)
    if not zip_norm:
        return {"found": False, "status": "unreachable", "message": "Ship-to ZIP is missing."}
    try:
        table, meta = _load_ground_zip_table()
    except FileNotFoundError:
        return None
    info = table.get(zip_norm)
    if not info:
        return {
            "found": False,
            "status": "unreachable",
            "message": "ZIP is not on the GOFO Ground 可达邮编 list",
            "source_file": meta.get("path_name") or "",
        }
    bits = [part for part in (info.get("city"), info.get("state")) if part]
    zone = (info.get("zones") or {}).get(_GROUND_DEFAULT_PORT)
    message = "On GOFO Ground 可达邮编"
    if bits:
        message += f" ({', '.join(bits)})"
    if zone:
        message += f" · {_GROUND_DEFAULT_PORT} zone {zone}"
    remote = (info.get("remote") or "").strip()
    if remote and remote.upper() not in ("N", "NO"):
        message += f" · {remote}"
    return {
        "found": True,
        "status": "reachable",
        "message": message,
        "info": info,
        "source_file": meta.get("path_name") or "",
    }


def _interpret_verify(body: dict[str, Any]) -> dict[str, Any]:
    code = body.get("code")
    msg = (body.get("msgEn") or body.get("msg") or "").strip()
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None

    if code_int == 200:
        status = "reachable"
    elif code_int in (501, 500) and "cannot be reached" in msg.lower():
        # UAT returns 500 for unreachable; doc lists 501
        status = "unreachable"
    elif code_int == 502:
        status = "blocked"
    else:
        status = "error"

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return {
        "status": status,
        "code": code_int,
        "message": msg or status,
        "sorting_code": (data or {}).get("sortingCode") or "",
    }


def verify_delivery(
    *,
    zip_code: str,
    country: str = "US",
    state: str = "",
    city: str = "",
    street: str = "",
    timeout: float = 25.0,
) -> dict[str, Any]:
    """POST verifyDelivery — zip reachability (not product-specific)."""
    base, user, password = _credentials()
    if not user or not password:
        return {
            "ok": False,
            "error": "GOFO API credentials are not configured (GOFO_API_ACCOUNT / GOFO_API_PASSWORD).",
        }

    consignee: dict[str, str] = {
        "consigneeCountry": normalize_country(country),
        "consigneeCode": normalize_zip(zip_code),
    }
    if state and state.strip().upper() != "N/A":
        consignee["consigneeState"] = state.strip()
    if city and city.strip().upper() != "N/A":
        consignee["consigneeCity"] = city.strip()
    if street:
        consignee["consigneeStreet"] = street.strip()[:120]

    url = f"{base}{VERIFY_PATH}"
    try:
        resp = _http_session().post(
            url,
            headers=_auth_headers(user, password),
            json={"orderConsignee": consignee},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"GOFO request failed: {exc}"}

    if resp.status_code == 401:
        return {
            "ok": False,
            "error": (
                "GOFO authentication failed (401). "
                "Fashion may be prod-only; set GOFO_API_BASE_URL to the production host."
            ),
            "http_status": 401,
        }
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"GOFO HTTP {resp.status_code}",
            "http_status": resp.status_code,
            "raw": (resp.text or "")[:400],
        }

    try:
        body = resp.json()
    except ValueError:
        return {"ok": False, "error": "GOFO returned non-JSON response", "raw": (resp.text or "")[:400]}

    interpreted = _interpret_verify(body if isinstance(body, dict) else {})
    return {
        "ok": True,
        "base_url": base,
        "zip": consignee["consigneeCode"],
        "country": consignee["consigneeCountry"],
        **interpreted,
        "raw": body,
    }


def _cost_estimation_request(
    *,
    product_code: str,
    zip_code: str,
    country: str,
    state: str,
    city: str,
    street: str,
    phone: str,
    name: str,
    length: float,
    width: float,
    height: float,
    weight_lb: float,
    dim_unit: str = "INCH",
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Full costEstimation call; dims in INCH/CM, weight always LB."""
    base, user, password = _credentials()
    if not user or not password:
        return {"ok": False, "error": "GOFO API credentials are not configured."}

    phone_digits = re.sub(r"\D", "", phone or "") or "3235550100"
    if len(phone_digits) < 10:
        phone_digits = "3235550100"
    consignee_name = (name or "Customer").strip() or "Customer"
    address1 = (street or "Address").strip() or "Address"
    city_clean = city.strip() if city and city.strip().upper() != "N/A" else "City"
    state_clean = state.strip() if state and state.strip().upper() != "N/A" else "CA"
    unit = (dim_unit or "INCH").strip().upper()
    if unit not in ("INCH", "CM"):
        unit = "INCH"

    payload = {
        "productCode": product_code,
        "declaredValue": 1,
        "shippingType": "HDN",
        "orderShipper": dict(_DEFAULT_SHIPPER),
        "orderConsignee": {
            "consigneeName": consignee_name[:100],
            "consigneePhone": phone_digits[:14],
            "consigneeCountry": normalize_country(country),
            "consigneeState": state_clean[:35],
            "consigneeCity": city_clean[:50],
            "address1": address1[:120],
            "consigneeCode": normalize_zip(zip_code),
        },
        "orderGoods": {
            "weight": round(float(weight_lb), 3),
            "length": round(float(length), 2),
            "width": round(float(width), 2),
            "height": round(float(height), 2),
            "weightUnit": "LB",
            "dimensionalUnit": unit,
        },
        "orderItemList": [{"itemNameEn": "Goods", "itemQty": 1}],
    }
    url = f"{base}{COST_PATH}"
    try:
        resp = _http_session().post(
            url,
            headers=_auth_headers(user, password),
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}

    if resp.status_code == 401:
        return {"ok": False, "error": "Authentication failed (401)", "http_status": 401}

    try:
        body = resp.json()
    except ValueError:
        return {"ok": False, "error": f"HTTP {resp.status_code} non-JSON", "raw": (resp.text or "")[:400]}

    if not isinstance(body, dict):
        return {"ok": False, "error": "Unexpected response"}

    code = body.get("code")
    msg = (body.get("msgEn") or body.get("msg") or "").strip()
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None

    if code_int != 200:
        if code_int == 401:
            err = (
                msg
                or f"{product_code}: account not authorized for this product (GOFO code 401). "
                "Ask GOFO to enable Ground on the Fashion API account."
            )
        else:
            err = msg or f"GOFO code {code_int}"
        return {"ok": False, "error": err, "code": code_int, "raw": body}

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    fees = data.get("freightFeeResultList") if isinstance(data.get("freightFeeResultList"), list) else []
    return {
        "ok": True,
        "code": code_int,
        "message": msg or "Success",
        "total": data.get("totalTaxAmount"),
        "currency": data.get("currencyName") or "USD",
        "delivery_days": data.get("deliveryDays"),
        "zone": data.get("zoneRegion"),
        "fees": fees,
        "raw": body,
        "base_url": base,
    }


def _quote_cache_key(
    *,
    product_code: str,
    zip_code: str,
    length: float,
    width: float,
    height: float,
    weight_lb: float,
    dim_unit: str,
) -> tuple:
    return (
        product_code,
        normalize_zip(zip_code),
        *_dim_key(length, width, height, weight_lb),
        (dim_unit or "INCH").strip().upper(),
    )


def _cached_cost_estimation(**kwargs: Any) -> dict[str, Any]:
    """Reuse identical GOFO quotes for a few minutes (same ZIP + carton + product)."""
    key = _quote_cache_key(
        product_code=kwargs["product_code"],
        zip_code=kwargs["zip_code"],
        length=kwargs["length"],
        width=kwargs["width"],
        height=kwargs["height"],
        weight_lb=kwargs["weight_lb"],
        dim_unit=kwargs.get("dim_unit") or "INCH",
    )
    now = time.time()
    with _quote_cache_lock:
        hit = _quote_cache.get(key)
        if hit and now - hit[0] < _QUOTE_CACHE_TTL_SEC:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached

    result = _cost_estimation_request(**kwargs)
    cacheable = result.get("ok") or result.get("code") in (200, 301, 500, 501, 502)
    if cacheable:
        store = {k: v for k, v in result.items() if k != "raw"}
        with _quote_cache_lock:
            _quote_cache[key] = (now, store)
            if len(_quote_cache) > 400:
                oldest = sorted(_quote_cache.items(), key=lambda kv: kv[1][0])[:80]
                for old_key, _old in oldest:
                    _quote_cache.pop(old_key, None)
    return result


def _quote_error_is_unreachable(msg: str, code: Any = None) -> bool:
    lower = (msg or "").strip().lower()
    if not lower:
        return False
    markers = (
        "cannot be reached",
        "not reachable",
        "not within delivery",
        "not within our delivery",
        "delivery service area",
        "not within our delivery service area",
        "不可达",
        "不在派送",
    )
    if any(m in lower for m in markers):
        return True
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    return code_int == 301 and ("zip" in lower or "postal" in lower or "service area" in lower)


def _cost_estimation_probe(
    *,
    product_code: str,
    zip_code: str,
    country: str,
    state: str,
    city: str,
    street: str,
    phone: str,
    name: str,
    timeout: float,
) -> dict[str, Any]:
    """Best-effort product-specific probe via costEstimation."""
    result = _cached_cost_estimation(
        product_code=product_code,
        zip_code=zip_code,
        country=country,
        state=state,
        city=city,
        street=street,
        phone=phone,
        name=name,
        length=10,
        width=10,
        height=10,
        weight_lb=1,
        dim_unit="INCH",
        timeout=timeout,
    )
    if not result.get("ok"):
        msg = (result.get("error") or "").strip()
        lower = msg.lower()
        if _quote_error_is_unreachable(msg, result.get("code")):
            return {"ok": True, "status": "unreachable", "message": msg, "code": result.get("code")}
        if "maintaining" in lower or "try again later" in lower:
            return {"ok": False, "status": "unavailable", "message": msg, "code": result.get("code")}
        if "product code" in lower:
            return {"ok": False, "status": "error", "message": msg, "code": result.get("code")}
        return {"ok": False, "status": "inconclusive", "message": msg or "error", "code": result.get("code")}

    return {
        "ok": True,
        "status": "reachable",
        "message": result.get("message") or "OK",
        "code": result.get("code"),
    }


def estimate_prices_for_boxes(
    *,
    boxes: list[dict[str, Any]],
    dim_unit: str = "INCH",
    zip_code: str,
    country: str = "US",
    state: str = "",
    city: str = "",
    street: str = "",
    phone: str = "",
    name: str = "",
    timeout: float = 25.0,
    product_keys: list[str] | str | None = None,
) -> dict[str, Any]:
    """Estimate freight for entered carton rows (unit price × qty per row)."""
    zip_norm = normalize_zip(zip_code)
    if not zip_norm:
        return {"ok": False, "error": "Ship-to ZIP is missing."}

    wanted: set[str] | None = None
    if product_keys:
        if isinstance(product_keys, str):
            wanted = {product_keys.strip().lower()}
        else:
            wanted = {str(k).strip().lower() for k in product_keys if str(k).strip()}
        wanted.discard("")
    products = tuple(p for p in PRODUCTS if wanted is None or p["key"] in wanted)
    if wanted and not products:
        return {"ok": False, "error": "Unknown GOFO service. Use parcel or ground."}

    input_rows: list[dict[str, Any]] = []
    for box in boxes or []:
        try:
            length = float(box.get("length") or 0)
            width = float(box.get("width") or 0)
            height = float(box.get("height") or 0)
            weight = float(box.get("weight") or 0)
            qty = int(box.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        if qty < 1:
            qty = 1
        if length <= 0 or width <= 0 or height <= 0 or weight <= 0:
            return {
                "ok": False,
                "error": "Each box needs length, width, height, and weight greater than 0.",
            }
        style = str(box.get("style") or "").strip()
        label = str(box.get("label") or style or "").strip()
        input_rows.append(
            {
                "length": length,
                "width": width,
                "height": height,
                "weight": weight,
                "qty": qty,
                "sku": str(box.get("sku") or "").strip(),
                "style": style,
                "label": label,
            }
        )

    if not input_rows:
        return {"ok": False, "error": "Add at least one box with dimensions and weight."}

    unique_map: dict[tuple[float, float, float, float], dict[str, Any]] = {}
    unique_order: list[tuple[float, float, float, float]] = []
    for row in input_rows:
        key = _dim_key(row["length"], row["width"], row["height"], row["weight"])
        if key not in unique_map:
            unique_map[key] = {
                "length": key[0],
                "width": key[1],
                "height": key[2],
                "weight": key[3],
            }
            unique_order.append(key)

    jobs: list[tuple[dict[str, str], tuple[float, float, float, float]]] = []
    for product in products:
        for key in unique_order:
            jobs.append((product, key))

    quote_by_job: dict[tuple[str, tuple[float, float, float, float]], dict[str, Any]] = {}

    def _run_quote(product: dict[str, str], key: tuple[float, float, float, float]) -> tuple[str, tuple, dict[str, Any]]:
        box = unique_map[key]
        res = _cached_cost_estimation(
            product_code=product["product_code"],
            zip_code=zip_norm,
            country=country,
            state=state,
            city=city,
            street=street,
            phone=phone,
            name=name,
            length=box["length"],
            width=box["width"],
            height=box["height"],
            weight_lb=box["weight"],
            dim_unit=dim_unit,
            timeout=timeout,
        )
        return product["key"], key, res

    workers = min(_QUOTE_WORKERS, max(1, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_quote, product, key) for product, key in jobs]
        for fut in as_completed(futures):
            pk, key, res = fut.result()
            quote_by_job[(pk, key)] = res

    box_count = sum(int(r["qty"]) for r in input_rows)
    services_out: list[dict[str, Any]] = []
    for product in products:
        rows_out = []
        total = 0.0
        currency = "USD"
        delivery_days = None
        zone = None
        errors = []
        for idx, row in enumerate(input_rows, start=1):
            key = _dim_key(row["length"], row["width"], row["height"], row["weight"])
            res = quote_by_job.get((product["key"], key)) or {}
            qty = int(row["qty"])
            name = row["style"] or row["label"] or row["sku"] or f"Row {idx}"
            if not res.get("ok"):
                err_msg = res.get("error") or "failed"
                errors.append(err_msg)
                rows_out.append(
                    {
                        "row": idx,
                        "ok": False,
                        "style": row["style"],
                        "sku": row["sku"],
                        "label": name,
                        "qty": qty,
                        "unit_price": None,
                        "line_total": None,
                        "error": err_msg,
                    }
                )
                continue
            amount = res.get("total")
            try:
                unit = float(amount)
            except (TypeError, ValueError):
                unit = 0.0
            line_total = round(unit * qty, 2)
            total += line_total
            currency = res.get("currency") or currency
            if delivery_days is None:
                delivery_days = res.get("delivery_days")
            if zone is None:
                zone = res.get("zone")
            rows_out.append(
                {
                    "row": idx,
                    "ok": True,
                    "style": row["style"],
                    "sku": row["sku"],
                    "label": name,
                    "qty": qty,
                    "unit_price": round(unit, 2),
                    "line_total": line_total,
                    "cached": bool(res.get("cached")),
                }
            )

        services_out.append(
            {
                "key": product["key"],
                "label": product["label"],
                "product_code": product["product_code"],
                "ok": len(errors) == 0,
                "total": round(total, 2) if len(errors) == 0 else None,
                "currency": currency,
                "delivery_days": delivery_days,
                "zone": zone,
                "box_count": box_count,
                "rows": rows_out,
                "error": "; ".join(
                    f"{r.get('label') or ('Row ' + str(r.get('row')))}: {r.get('error')}"
                    for r in rows_out
                    if not r.get("ok")
                ),
            }
        )

    any_ok = any(s.get("ok") for s in services_out)
    return {
        "ok": any_ok,
        "zip": zip_norm,
        "dim_unit": (dim_unit or "INCH").upper(),
        "box_count": box_count,
        "unique_sizes": len(unique_order),
        "quote_calls": len(jobs),
        "product_keys": [p["key"] for p in products],
        "services": services_out,
        "error": None if any_ok else (services_out[0].get("error") if services_out else "Price check failed"),
    }


def check_parcel_and_ground_excel(
    *,
    zip_code: str,
    country: str = "US",
) -> dict[str, Any]:
    """Fallback: Parcel Excel + Ground 可达邮编 workbook."""
    zip_norm = normalize_zip(zip_code)
    if not zip_norm:
        return {"ok": False, "error": "Ship-to ZIP is missing."}

    try:
        active, closed, meta = _load_parcel_zip_tables()
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read GOFO ZIP Excel: {exc}"}

    if zip_norm in closed:
        parcel_status = "unreachable"
        parcel_message = "Postal code is on the pending-close / closed list"
        info = closed[zip_norm]
    elif zip_norm in active:
        parcel_status = "reachable"
        parcel_message = "This postal code can be reached"
        info = active[zip_norm]
    else:
        parcel_status = "unreachable"
        parcel_message = "Postal code is not in the GOFO Parcel reachable ZIP list"
        info = {}

    region = info.get("region") or ""
    state_code = info.get("state") or ""
    city_name = info.get("city") or ""
    detail_bits = []
    if city_name:
        detail_bits.append(city_name)
    if state_code:
        detail_bits.append(state_code)
    elif region:
        detail_bits.append(region)
    if detail_bits and parcel_status == "reachable":
        parcel_message = f"{parcel_message} ({', '.join(detail_bits)})"

    ground = lookup_ground_zip(zip_norm) or {
        "found": False,
        "status": parcel_status,
        "message": parcel_message + " (Ground workbook missing; used Parcel list)",
        "source_file": "",
    }

    services: list[dict[str, Any]] = []
    for product in PRODUCTS:
        if product["key"] == "ground":
            status = ground.get("status") or "unreachable"
            message = ground.get("message") or status
            source = "excel_ground"
            note = ground.get("source_file") or ""
        else:
            status = parcel_status
            message = parcel_message
            source = "excel"
            note = meta.get("path_name") or ""
        services.append(
            {
                "key": product["key"],
                "label": product["label"],
                "product_code": product["product_code"],
                "status": status,
                "message": message,
                "source": source,
                "probe_note": note,
            }
        )

    return {
        "ok": True,
        "zip": zip_norm,
        "country": normalize_country(country),
        "zip_check": {
            "status": parcel_status,
            "message": parcel_message,
            "sorting_code": "",
            "code": 200 if parcel_status == "reachable" else 501,
            "source_file": meta.get("path_name") or "",
            "city": city_name,
            "state": state_code,
            "region": region,
        },
        "services": services,
        "base_url": None,
        "lookup": "excel",
    }


def check_parcel_and_ground(
    *,
    zip_code: str,
    country: str = "US",
    state: str = "",
    city: str = "",
    street: str = "",
    phone: str = "",
    name: str = "",
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Check GOFO Parcel (API) and Ground (Fashion 可达邮编 sheet).

    verifyDelivery = ZIP in GOFO network (not product-specific).
    Parcel: costEstimation probe.
    Ground: 202609 GOFO-Ground 可达邮编 workbook (contracted ZIP list).
    """
    zip_norm = normalize_zip(zip_code)
    if not zip_norm:
        return {"ok": False, "error": "Ship-to ZIP is missing."}

    zip_result = verify_delivery(
        zip_code=zip_norm,
        country=country,
        state=state,
        city=city,
        street=street,
        timeout=timeout,
    )
    if not zip_result.get("ok"):
        # Fall back to Excel when API credentials/host fail
        excel = check_parcel_and_ground_excel(zip_code=zip_norm, country=country)
        if excel.get("ok"):
            excel["api_error"] = zip_result.get("error")
            excel["lookup"] = "excel_fallback"
            return excel
        return zip_result

    zip_status = zip_result.get("status")
    ground_excel = lookup_ground_zip(zip_norm)

    def _probe_product(product: dict[str, str]) -> dict[str, Any]:
        return _cost_estimation_probe(
            product_code=product["product_code"],
            zip_code=zip_norm,
            country=country,
            state=state,
            city=city,
            street=street,
            phone=phone,
            name=name,
            timeout=timeout,
        )

    def _status_from_probe(probe: dict[str, Any]) -> tuple[str, str, str]:
        probe_status = probe.get("status")
        if probe_status in ("reachable", "unreachable", "blocked"):
            return probe_status, probe.get("message") or probe_status, "costEstimation"
        if probe.get("message"):
            status = "unreachable" if _quote_error_is_unreachable(
                str(probe.get("message") or ""), probe.get("code")
            ) else "error"
            return status, str(probe.get("message")), "costEstimation"
        if zip_status == "reachable":
            message = zip_result.get("message") or "This postal code can be reached"
        elif zip_status == "unreachable":
            message = zip_result.get("message") or "Postal code cannot be reached"
        elif zip_status == "blocked":
            message = zip_result.get("message") or "Reachable but address type blocked"
        else:
            message = zip_result.get("message") or "Unable to determine"
        return zip_status or "unknown", message, "verifyDelivery"

    services: list[dict[str, Any]] = []
    for product in PRODUCTS:
        if product["key"] == "ground" and ground_excel is not None:
            status = ground_excel.get("status") or "unreachable"
            message = ground_excel.get("message") or status
            source = "excel_ground"
            probe_note = ground_excel.get("source_file") or ""
        else:
            probe = _probe_product(product)
            status, message, source = _status_from_probe(probe)
            probe_note = probe.get("message") if source == "verifyDelivery" else ""
        services.append(
            {
                "key": product["key"],
                "label": product["label"],
                "product_code": product["product_code"],
                "status": status,
                "message": message,
                "source": source,
                "probe_note": probe_note,
            }
        )

    return {
        "ok": True,
        "zip": zip_norm,
        "country": normalize_country(country),
        "zip_check": {
            "status": zip_status,
            "message": zip_result.get("message"),
            "sorting_code": zip_result.get("sorting_code") or "",
            "code": zip_result.get("code"),
        },
        "services": services,
        "base_url": zip_result.get("base_url"),
        "lookup": "api",
    }
