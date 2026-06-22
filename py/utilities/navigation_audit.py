"""
Audit trail for authenticated navigations (home dashboard tool cards and similar GET visits).

Uses the same log-viewer markers as Boat: ***user***__@@@action@@@ …
Skips static assets, JSON/XHR polling, high-volume image resize GETs, log viewer pages,
dashboard helper GETs (e.g. /get_favorites), and routes that already emit their own banner log.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from flask import Flask, current_app, request, session

LastKey = Tuple[str, str]
_last_nav_ts: Dict[LastKey, float] = {}
_THROTTLE_SEC = 3.0
_PRUNE_INTERVAL_SEC = 300.0
_last_prune_mono = 0.0

_SKIP_ENDPOINT_SUFFIXES = frozenset(
    (
        ".boat_service",  # already logs ***user***__@@@checked boat page@@@
        ".boat_special",  # already logs ***user***__@@@checked boat special (L) page@@@
        ".boat_data",
        ".static",
        ".logout",
    )
)

_SKIP_ENDPOINTS_EXACT = frozenset(
    (
        "robots_txt",
        "favicon",
    )
)

_SKIP_PATH_PREFIXES = ("/static/",)

_SKIP_PATH_FRAGMENTS = (
    "/boat/data",
)

# Avoid triple-logging file downloads that already have dedicated audit lines
_SKIP_PATH_PREFIXES_EXTRA = (
    "/boat/packslip/",
    "/boat/download/",
    "/image/",  # e.g. /image/<id>/800 — many parallel GETs from galleries
    "/log_viewer",
    "/api/batch_fetch",  # e.g. /api/batch_fetch_latest — polling / batch helpers
    "/lori_automation/get_",  # JSON helpers: get_package_info, get_orders, get_store_info, …
    "/api/search_images",  # dedicated ***user***__@@@searched images@@@ log in master_image_viewer
    "/api/fetch_china_ftp",
)

# POSTs that emit their own ***user***__@@@search SKU@@@__ audit line
_SKIP_POST_PATHS_EXACT = frozenset(
    (
        "/clientview/product_detail",
        "/product_detail",
        "/productdetail",
    )
)

_PRODUCT_DETAIL_PATH_PREFIXES = (
    "/product_detail",
    "/productdetail",
    "/clientview/product_detail",
)

# Dashboard / SPA helper GETs (not meaningful "opened page" audits)
_SKIP_GET_PATHS_EXACT = frozenset(
    (
        "/get_favorites",
        "/api/china_directory/index_status",
    )
)

# Authenticated POSTs worth auditing (avoid logging every minor AJAX / search POST).
_POST_AUDIT_PATH_PREFIXES = (
    "/manage_users",
    "/audit",
    "/lori_automation/",
    "/email/",
    "/boat/process",
    "/boat/mark_landed",
    "/boat/unland",
    "/boat/redo",
    "/order_processor",
    "/dam",
    "/v7_listcreator",
    "/customer",
    "/container",
    "/image_upload",
    "/mysql_connector",
    "/mobile_scanner",
    "/burlington_label_processor",
    "/location_massive_audit",
    "/warehousemap",
    "/tiktok",
    "/clientview",
    "/four_seller",
    "/vendor_app",
    "/delete_system_restarted",
    "/item_lookup",
    "/order_lookup",
    "/wipe_location",
    "/external_stock_check",
    "/internal_stock_check",
    "/inventory_manual_adjuster",
    "/switch_number",
    "/api/external_stock_check",
    "/api/internal_stock_check",
    "/api/inventory_manual_adjuster/search",
    "/api/inventory_manual_adjuster/transfer",
    "/api/inventory_manual_adjuster/adjust",
    "/api/switch_number/search",
    "/api/switch_number/transfer",
    "/bag_style_viewer",
    "/burlington_highlight",
    "/master_image_viewer",
    "/master_image_viewer/chinadirectory",
    "/api/china_directory/browse",
    "/api/china_directory/search",
)

_last_post_ts: Dict[Tuple[str, str, str], float] = {}


def _client_ip() -> str:
    xf = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xf or (request.remote_addr or "")


def _referrer_from_home() -> bool:
    ref = request.referrer or ""
    if not ref.strip():
        return False
    try:
        path = urlparse(ref).path or ""
    except Exception:
        return False
    return path in ("", "/")


def _json_only_accept() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    if not accept:
        return False
    if "text/html" in accept:
        return False
    return "application/json" in accept


def _is_log_viewer_path(path: str) -> bool:
    """True for log viewer URLs; never matches /login (use /log/ not /log prefix alone)."""
    if path == "/log":
        return True
    if path.startswith("/log/"):
        return True
    return False


def _is_product_detail_search_get(path: str) -> bool:
    """GET with a search term — product_detail / clientview already log the search."""
    for prefix in _PRODUCT_DETAIL_PATH_PREFIXES:
        if path.startswith(prefix + "/") and len(path) > len(prefix) + 1:
            return True
    if path not in _PRODUCT_DETAIL_PATH_PREFIXES:
        return False
    if request.args.get("search") or request.args.get("search_id"):
        return True
    return False


def _should_skip(endpoint: str | None, path: str) -> bool:
    if endpoint == "static":
        return True
    if _is_log_viewer_path(path):
        return True
    if path in _SKIP_GET_PATHS_EXACT:
        return True
    if path in ("/favicon.ico", "/robots.txt"):
        return True
    for prefix in _SKIP_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    for prefix in _SKIP_PATH_PREFIXES_EXTRA:
        if path.startswith(prefix):
            return True
    for frag in _SKIP_PATH_FRAGMENTS:
        if frag in path:
            return True
    if _is_product_detail_search_get(path):
        return True
    if not endpoint:
        return False
    if endpoint in _SKIP_ENDPOINTS_EXACT:
        return True
    for suf in _SKIP_ENDPOINT_SUFFIXES:
        if endpoint.endswith(suf):
            return True
    return False


def _prune_nav_ts(now: float) -> None:
    global _last_prune_mono
    if now - _last_prune_mono < _PRUNE_INTERVAL_SEC:
        return
    _last_prune_mono = now
    cutoff = now - 120.0
    stale = [k for k, t in _last_nav_ts.items() if t < cutoff]
    for k in stale[:2000]:
        _last_nav_ts.pop(k, None)


def register_navigation_audit(app: Flask) -> None:
    @app.after_request
    def _navigation_audit_after_post(response: Any) -> Any:
        if request.method != "POST":
            return response
        uid = session.get("username") or session.get("user")
        if not uid:
            return response
        path = request.path or ""
        if path.startswith("/static/"):
            return response
        ep = request.endpoint or ""
        low_ep = ep.lower()
        if ep == "static" or "login" in low_ep:
            return response

        if not any(path.startswith(p) for p in _POST_AUDIT_PATH_PREFIXES):
            return response
        if path in _SKIP_POST_PATHS_EXACT:
            return response

        now = time.monotonic()
        pkey: Tuple[str, str, str] = (str(uid), path, ep)
        prev_p = _last_post_ts.get(pkey, 0.0)
        if now - prev_p < _THROTTLE_SEC:
            return response
        _last_post_ts[pkey] = now

        try:
            current_app.logger.info(
                f"***{uid}***__@@@submitted form@@@ path={path} endpoint={ep} "
                f"status={response.status_code} ip={_client_ip()}"
            )
        except Exception:
            pass
        return response

    @app.before_request
    def _navigation_audit_before_request() -> None:
        if request.method != "GET":
            return
        uid = session.get("username") or session.get("user")
        if not uid:
            return
        path = request.path or ""
        endpoint = request.endpoint
        if _should_skip(endpoint, path):
            return
        if _json_only_accept():
            return
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return

        now = time.monotonic()
        key: LastKey = (str(uid), path)
        prev = _last_nav_ts.get(key, 0.0)
        if now - prev < _THROTTLE_SEC:
            return
        _last_nav_ts[key] = now
        _prune_nav_ts(now)

        from_home = _referrer_from_home()
        home_flag = "from_home=1" if from_home else "from_home=0"
        if path in ("", "/"):
            label = "opened home menu"
        else:
            label = "opened page"

        qs = (request.query_string or b"").decode("utf-8", errors="replace")
        qs_part = f" q={qs}" if qs else ""

        try:
            current_app.logger.info(
                f"***{uid}***__@@@{label}@@@ path={path}{qs_part} {home_flag} ip={_client_ip()}"
            )
        except Exception:
            pass
