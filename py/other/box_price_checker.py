"""Admin tool: compare corrugated box prices from Uline and 8NET."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from itertools import permutations
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from flask import Blueprint, jsonify, render_template, request, session

from py.utilities.decorators import login_required
from py.utilities.logging_setup import log_event

box_price_checker_bp = Blueprint("box_price_checker", __name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FETCH_TIMEOUT = 18
CACHE_TTL_SEC = 30 * 60
MAX_BOXES = 12
MAX_MATCHES = 5
DEFAULT_TOLERANCE = 0.5

ULINE_PAGES: List[Tuple[float, float, str]] = [
    (1, 8, "https://www.uline.com/Super_Carton1_190.htm"),
    (9, 11, "https://www.uline.com/Super_Carton2_190.htm"),
    (12, 13, "https://www.uline.com/Super_Carton3_190.htm"),
    (14, 17, "https://www.uline.com/Super_Carton4_190.htm"),
    (18, 23, "https://www.uline.com/Super_Carton5_190.htm"),
    (24, 26, "https://www.uline.com/Super_Carton6_190.htm"),
    (27, 96, "https://www.uline.com/Super_Carton7_190.htm"),
]

EIGHTNET_PAGES: List[Tuple[float, float, str]] = [
    (1, 9, "https://www.8net.com/corrugated/over-500-box-1/3-9.html"),
    (9, 11, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/over-500-box-1/9-11.html"),
    (12, 13, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/over-500-box-1/12-13.html"),
    (14, 15, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/over-500-box-1/14-15.html"),
    (16, 17, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/corrugated-boxes/over-500-box-1/corrugated-box-16-17.html"),
    (18, 19, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/over-500-box-1/18-19.html"),
    (20, 23, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/corrugated-boxes/over-500-box-1/corrugated-box-20-23.html"),
    (24, 33, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/over-500-box-1/24-33.html"),
    (34, 96, "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/corrugated-boxes/over-500-box-1/corrugated-box-36-60.html"),
]

_cache_lock = Lock()
_page_cache: Dict[str, Tuple[float, str]] = {}
_http = requests.Session()
_http.headers.update(BROWSER_HEADERS)

_INCH_TOKEN = r"(\d+(?:\s+\d+\s*/\s*\d+|\s*-\s*\d+\s*/\s*\d+|\.\d+)?)"
_DIM_X = re.compile(
    rf"{_INCH_TOKEN}\s*[xX×]\s*{_INCH_TOKEN}\s*[xX×]\s*{_INCH_TOKEN}",
    re.I,
)
_LWH_LABELED = re.compile(
    rf'{_INCH_TOKEN}\s*"?\s*L\b.*?{_INCH_TOKEN}\s*"?\s*W\b.*?{_INCH_TOKEN}\s*"?\s*D\b',
    re.I | re.S,
)
_MONEY = re.compile(r"\$?\s*(\d+(?:\.\d+)?|\.\d+)")
_ULINE_MODEL = re.compile(r"model=(S-\d+)", re.I)
_ULINE_PRICE_CELL = re.compile(
    r"class=['\"]PriceCell[^'\"]*['\"][^>]*>\s*(?:<attrib[^>]*>)?\s*(\$[\d.,]+)",
    re.I,
)
_ULINE_QTY_HEAD = re.compile(
    r"ChartheadLargeItemW10H18[^>]*>.*?type=\"2\">\s*([\d,]+(?:\+)?)\s*<",
    re.I | re.S,
)
_ULINE_BUNDLE_PAIR = re.compile(
    r"type=\"2\">\s*(\d+)\s*</attrib>\s*/\s*<attrib[^>]*type=\"2\">\s*(\d+)",
    re.I | re.S,
)
_EIGHTNET_MODEL = re.compile(r"class=\"col model[^\"]*\"[^>]*>\s*([A-Z0-9-]+)", re.I)
_EIGHTNET_DIM = re.compile(r"class=\"col inside_dim\"[^>]*>\s*([^<]+)", re.I)
_EIGHTNET_DESC = re.compile(r"class=\"col description\"[^>]*>(.*?)</td>", re.I | re.S)
_EIGHTNET_PRICE = re.compile(r"class=\"align-right-25\"[^>]*>\s*([^<]+)", re.I)
_EIGHTNET_QUOTE = re.compile(r"class=\"same-price-label[^\"]*\"[^>]*>\s*([^<]+)", re.I)
_EIGHTNET_BUNDLE = re.compile(r"class=\"col qty_bndl\"[^>]*>\s*([^<]+)", re.I)
_EIGHTNET_SKID = re.compile(r"class=\"col qty_skid\"[^>]*>\s*([^<]+)", re.I)

EIGHTNET_TIER_LABELS = (
    "$100 / 1 bndl",
    "$500 / 1/2 skid",
    "$1,000 / 1 skid",
    "$2,500 / 2 skids",
    "4 skids+",
)


def parse_inch_value(text: Any) -> Optional[float]:
    """Parse 12, 11 1/4, 3-1/2, or 8.5 into inches."""
    raw = unescape(str(text or "")).strip()
    raw = raw.replace("″", '"').replace("”", '"').replace("“", '"')
    raw = raw.replace("–", "-").replace("—", "-")
    raw = re.split(r'["\']', raw, maxsplit=1)[0].strip()
    raw = re.split(r"\s+-\s+", raw, maxsplit=1)[0].strip()
    if not raw:
        return None
    mixed = re.match(r"^\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s*$", raw)
    if mixed:
        den = float(mixed.group(3))
        return None if den == 0 else float(mixed.group(1)) + float(mixed.group(2)) / den
    hyphen_frac = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\s*$", raw)
    if hyphen_frac:
        den = float(hyphen_frac.group(3))
        return None if den == 0 else float(hyphen_frac.group(1)) + float(hyphen_frac.group(2)) / den
    only_frac = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", raw)
    if only_frac:
        den = float(only_frac.group(2))
        return None if den == 0 else float(only_frac.group(1)) / den
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0 or value > 200:
        return None
    return value


def parse_lwh(text: str) -> Optional[Tuple[float, float, float]]:
    blob = unescape(text or "")
    labeled = _LWH_LABELED.search(blob)
    if labeled:
        dims = [parse_inch_value(labeled.group(i)) for i in (1, 2, 3)]
        if all(d is not None for d in dims):
            return dims[0], dims[1], dims[2]  # type: ignore[return-value]
    plain = _DIM_X.search(blob.replace("&quot;", '"'))
    if not plain:
        return None
    dims = [parse_inch_value(plain.group(i)) for i in (1, 2, 3)]
    if any(d is None for d in dims):
        return None
    return dims[0], dims[1], dims[2]  # type: ignore[return-value]


def parse_money(text: str) -> Optional[float]:
    blob = unescape(text or "").replace(",", "")
    if re.search(r"email\s+for\s+quote|call|n/?a", blob, re.I):
        return None
    match = _MONEY.search(blob)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _dim_delta(
    wanted: Sequence[float], found: Sequence[float]
) -> Tuple[float, float]:
    """Best orientation-aware distance: (sum abs, max abs)."""
    best_sum = None
    best_max = None
    for perm in set(permutations(found)):
        diffs = [abs(w - f) for w, f in zip(wanted, perm)]
        total = sum(diffs)
        peak = max(diffs) if diffs else 0.0
        if best_sum is None or total < best_sum or (total == best_sum and peak < best_max):
            best_sum = total
            best_max = peak
    return (best_sum or 0.0, best_max or 0.0)


def match_offers(
    wanted: Sequence[float],
    offers: Iterable[Dict[str, Any]],
    tolerance: float,
    limit: int = MAX_MATCHES,
    fallback_nearest: int = 0,
) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    nearest: List[Dict[str, Any]] = []
    for offer in offers:
        dims = offer.get("dims")
        if not dims or len(dims) != 3:
            continue
        total, peak = _dim_delta(wanted, dims)
        same_sum = sum(abs(float(w) - float(f)) for w, f in zip(wanted, dims))
        exact = peak <= 0.02
        close = peak <= max(tolerance, 0.0)
        row = dict(offer)
        row["exact"] = exact
        row["delta_in"] = round(peak, 3)
        row["delta_sum"] = round(total, 3)
        row["same_sum"] = round(same_sum, 3)
        nearest.append(row)
        if close:
            scored.append(row)

    def _sort_key(item: Dict[str, Any]):
        return (
            0 if item["exact"] else 1,
            item["delta_sum"],
            item.get("same_sum", 99),
            item["delta_in"],
            item.get("unit_price") if item.get("unit_price") is not None else 9999,
        )

    if scored:
        scored.sort(key=_sort_key)
        return scored[:limit]
    nearest.sort(key=_sort_key)
    return nearest[: max(0, fallback_nearest)]


def _page_for_longest(pages: Sequence[Tuple[float, float, str]], longest: float) -> str:
    for low, high, url in pages:
        if low - 0.01 <= longest <= high + 0.01:
            return url
    return pages[-1][2]


def _cached_get(url: str, params: Optional[dict] = None) -> str:
    key = url if not params else url + "?" + json.dumps(params, sort_keys=True)
    now = time.time()
    with _cache_lock:
        hit = _page_cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1]
    resp = _http.get(url, params=params, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    text = resp.text or ""
    with _cache_lock:
        _page_cache[key] = (now, text)
    return text


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _inner_text(html_fragment: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", html_fragment or ""))


def _int_qty(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    try:
        return int(digits) or None
    except ValueError:
        return None


def _tier_list(labels: Sequence[str], values: Sequence[Any]) -> List[Dict[str, Any]]:
    tiers: List[Dict[str, Any]] = []
    for idx, raw in enumerate(values):
        label = labels[idx] if idx < len(labels) else f"Tier {idx + 1}"
        if isinstance(raw, (int, float)):
            tiers.append({"qty": label, "price": float(raw), "note": None})
            continue
        blob = _clean_text(str(raw or ""))
        if not blob:
            continue
        if re.search(r"email|quote|call", blob, re.I):
            tiers.append({"qty": label, "price": None, "note": blob})
            continue
        price = parse_money(blob)
        tiers.append({"qty": label, "price": price, "note": None if price is not None else blob})
    return tiers


def parse_uline_catalog(html: str) -> List[Dict[str, Any]]:
    offers: List[Dict[str, Any]] = []
    for chunk in re.split(r"<tr\b", html or "", flags=re.I):
        model_m = _ULINE_MODEL.search(chunk)
        if not model_m:
            continue
        sku = model_m.group(1).upper()
        dims = parse_lwh(chunk)
        if not dims:
            continue
        price_m = re.search(
            r"bodytext1_sale[^>]*>\s*<b>\s*([\d.]+)\s*</b>", chunk, re.I
        ) or re.search(r"<b>\s*(\d+\.\d+)\s*</b>\s*</td>", chunk, re.I)
        price = float(price_m.group(1)) if price_m else None
        titles = [
            _clean_text(t)
            for t in re.findall(
                r"ProductDetail\.aspx\?model=S-\d+'[^>]*>\s*([^<]+)", chunk, re.I
            )
        ]
        title = next((t for t in titles if t.upper() != sku), None) or (
            titles[0] if titles else f'{dims[0]:g} x {dims[1]:g} x {dims[2]:g}"'
        )
        offers.append(
            {
                "sku": sku,
                "title": title,
                "description": "Corrugated box",
                "dims": list(dims),
                "unit_price": price,
                "price_tiers": _tier_list(["25+"], [price] if price is not None else []),
                "bundle_qty": 25,
                "bale_qty": None,
                "skid_qty": None,
                "url": f"https://www.uline.com/Product/ProductDetail.aspx?model={sku}",
            }
        )
    return offers


def parse_uline_detail(html: str, sku: str = "") -> Dict[str, Any]:
    """Quantity breaks from a Uline product detail chart."""
    labels = [_clean_text(h) for h in _ULINE_QTY_HEAD.findall(html or "")]
    labels = [h for h in labels if re.match(r"^[\d,]+", h)]
    sku_u = (sku or "").upper()
    chunk = html or ""
    if sku_u:
        marker = f">{sku_u}<"
        idx = chunk.upper().find(marker)
        if idx >= 0:
            chunk = chunk[idx : idx + 8000]
    prices = [parse_money(p) for p in _ULINE_PRICE_CELL.findall(chunk)]
    prices = [p for p in prices if p is not None]
    if labels and prices:
        labels = labels[: len(prices)]
    elif prices and not labels:
        labels = [f"Qty {i + 1}" for i in range(len(prices))]
    bundle_qty = None
    bale_qty = None
    pair = _ULINE_BUNDLE_PAIR.search(chunk)
    if pair:
        bundle_qty = _int_qty(pair.group(1))
        bale_qty = _int_qty(pair.group(2))
    tiers = _tier_list(labels, prices)
    unit = tiers[0]["price"] if tiers else None
    return {
        "price_tiers": tiers,
        "unit_price": unit,
        "bundle_qty": bundle_qty,
        "bale_qty": bale_qty,
    }


def parse_eightnet_catalog(html: str, page_url: str = "") -> List[Dict[str, Any]]:
    offers: List[Dict[str, Any]] = []
    base = (page_url or "").split("#")[0].split("?")[0].rstrip("/")
    for chunk in re.split(r"<tr\b", html or "", flags=re.I):
        model_m = _EIGHTNET_MODEL.search(chunk)
        dim_m = _EIGHTNET_DIM.search(chunk)
        if not model_m or not dim_m:
            continue
        sku = _clean_text(model_m.group(1)).upper()
        dims = parse_lwh(dim_m.group(1))
        if not sku or not dims:
            continue
        desc_m = _EIGHTNET_DESC.search(chunk)
        raw_prices = list(_EIGHTNET_PRICE.findall(chunk))
        quote = _EIGHTNET_QUOTE.search(chunk)
        if quote:
            raw_prices.append(_clean_text(quote.group(1)))
        tiers = _tier_list(EIGHTNET_TIER_LABELS, raw_prices)
        unit = next((t["price"] for t in tiers if t.get("price") is not None), None)
        bundle_qty = _int_qty(bundle_m.group(1)) if (bundle_m := _EIGHTNET_BUNDLE.search(chunk)) else None
        skid_qty = _int_qty(skid_m.group(1)) if (skid_m := _EIGHTNET_SKID.search(chunk)) else None
        if base:
            product_url = f"{base}?a={sku}"
        else:
            product_url = f"https://www.8net.com/catalogsearch/result/?q={requests.utils.quote(sku)}"
        offers.append(
            {
                "sku": sku,
                "title": _clean_text(dim_m.group(1)),
                "description": _inner_text(desc_m.group(1) if desc_m else "Corrugated box"),
                "dims": list(dims),
                "unit_price": unit,
                "price_tiers": tiers,
                "bundle_qty": bundle_qty,
                "bale_qty": None,
                "skid_qty": skid_qty,
                "url": product_url,
            }
        )
    return offers


def _enrich_uline_match(match: Dict[str, Any]) -> Dict[str, Any]:
    sku = match.get("sku") or ""
    url = f"https://www.uline.com/Product/ProductDetail.aspx?model={sku}"
    try:
        html = _cached_get(url)
        detail = parse_uline_detail(html, sku)
        if detail.get("price_tiers"):
            match["price_tiers"] = detail["price_tiers"]
            match["unit_price"] = detail.get("unit_price")
        if detail.get("bundle_qty"):
            match["bundle_qty"] = detail["bundle_qty"]
        if detail.get("bale_qty"):
            match["bale_qty"] = detail["bale_qty"]
        match["url"] = url
        match["description"] = "Sold in bundle quantities"
    except Exception:
        pass
    return match


def fetch_uline(wanted: Sequence[float], tolerance: float) -> Dict[str, Any]:
    longest = max(wanted)
    page = _page_for_longest(ULINE_PAGES, longest)
    search_url = page
    try:
        html = _cached_get(page)
        offers = parse_uline_catalog(html)
        matches = match_offers(wanted, offers, tolerance)
        if matches:
            with ThreadPoolExecutor(max_workers=min(5, len(matches))) as pool:
                matches = list(pool.map(_enrich_uline_match, matches))
        return {
            "ok": True,
            "vendor": "uline",
            "label": "Uline",
            "search_url": search_url,
            "matches": matches,
            "error": None if matches else "No Uline box within the size tolerance.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "vendor": "uline",
            "label": "Uline",
            "search_url": search_url,
            "matches": [],
            "error": f"Could not load Uline catalog ({exc.__class__.__name__}).",
        }


def fetch_eightnet(wanted: Sequence[float], tolerance: float) -> Dict[str, Any]:
    longest = max(wanted)
    page = _page_for_longest(EIGHTNET_PAGES, longest)
    q = f"{wanted[0]:g} x {wanted[1]:g} x {wanted[2]:g}"
    search_url = f"https://www.8net.com/catalogsearch/result/?q={requests.utils.quote(q)}"
    try:
        html = _cached_get(page)
        offers = parse_eightnet_catalog(html, page)
        matches = match_offers(wanted, offers, tolerance)
        return {
            "ok": True,
            "vendor": "eightnet",
            "label": "8NET",
            "search_url": page,
            "matches": matches,
            "error": None if matches else "No 8NET box within the size tolerance.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "vendor": "eightnet",
            "label": "8NET",
            "search_url": search_url,
            "matches": [],
            "error": f"Could not load 8NET catalog ({exc.__class__.__name__}).",
        }


VENDOR_FETCHERS = {
    "uline": fetch_uline,
    "eightnet": fetch_eightnet,
}


def _validate_box(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    length = parse_inch_value(raw.get("length"))
    width = parse_inch_value(raw.get("width"))
    height = parse_inch_value(raw.get("height") if raw.get("height") is not None else raw.get("depth"))
    if length is None or width is None or height is None:
        return None
    if min(length, width, height) < 0.25 or max(length, width, height) > 120:
        return None
    return {
        "id": str(raw.get("id") or "").strip()[:32],
        "length": length,
        "width": width,
        "height": height,
        "dims": [length, width, height],
    }


def check_boxes(boxes: List[Dict[str, Any]], tolerance: float) -> List[Dict[str, Any]]:
    jobs: List[Tuple[int, str, Sequence[float]]] = []
    results: List[Dict[str, Any]] = []
    for idx, box in enumerate(boxes):
        results.append(
            {
                "id": box["id"],
                "length": box["length"],
                "width": box["width"],
                "height": box["height"],
                "vendors": {},
            }
        )
        for vendor in ("uline", "eightnet"):
            jobs.append((idx, vendor, box["dims"]))

    def _run(job: Tuple[int, str, Sequence[float]]):
        idx, vendor, dims = job
        return idx, vendor, VENDOR_FETCHERS[vendor](dims, tolerance)

    with ThreadPoolExecutor(max_workers=min(9, max(3, len(jobs)))) as pool:
        futures = [pool.submit(_run, job) for job in jobs]
        for fut in as_completed(futures):
            idx, vendor, payload = fut.result()
            results[idx]["vendors"][vendor] = payload
    return results


@box_price_checker_bp.route("/box_price_checker", methods=["GET"])
@login_required
def box_price_checker_page():
    return render_template(
        "box_price_checker.html",
        current_user=session.get("username"),
    )


@box_price_checker_bp.route("/box_price_checker/api/check", methods=["POST"])
@login_required
def box_price_checker_api():
    body = request.get_json(silent=True) or {}
    raw_boxes = body.get("boxes") if isinstance(body, dict) else None
    if not isinstance(raw_boxes, list) or not raw_boxes:
        return jsonify(ok=False, error="Add at least one box with length, width, and height."), 400
    if len(raw_boxes) > MAX_BOXES:
        return jsonify(ok=False, error=f"Maximum {MAX_BOXES} boxes per check."), 400
    try:
        tolerance = float(body.get("tolerance", DEFAULT_TOLERANCE))
    except (TypeError, ValueError):
        tolerance = DEFAULT_TOLERANCE
    tolerance = min(max(tolerance, 0.0), 6.0)

    boxes = []
    for raw in raw_boxes:
        parsed = _validate_box(raw)
        if parsed:
            boxes.append(parsed)
    if not boxes:
        return jsonify(ok=False, error="Enter valid dimensions in inches (0.25 to 120)."), 400

    user = session.get("username") or "?"
    summary = ", ".join(f"{b['length']:g}x{b['width']:g}x{b['height']:g}" for b in boxes)
    log_event("INFO", f"BOX_PRICE_CHECK user={user} boxes={summary}")

    return jsonify(ok=True, tolerance=tolerance, results=check_boxes(boxes, tolerance))
