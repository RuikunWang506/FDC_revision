"""Record who hit every endpoint.

Authenticated requests → user.log with ***username***.
Unauthenticated illegal / probe / failed access → visitor.log.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from flask import Flask, request, session

LastKey = Tuple[str, str, str]
_last_ts: Dict[LastKey, float] = {}
_THROTTLE_SEC = 2.0
_PRUNE_INTERVAL_SEC = 300.0
_last_prune_mono = 0.0
_QS_MAX = 180

_SKIP_EXACT = frozenset(
    {
        "/favicon.ico",
        "/robots.txt",
        "/health",
        "/get_favorites",
        "/save_favorites",
        "/save_dashboard_background",
        "/user_dashboard_background",
        "/dashboard_background_from_url",
        "/upload_dashboard_background",
    }
)

_SKIP_PREFIXES = (
    "/static/",
    "/api/presence/",
    "/log/stream",
    "/image/",
    "/list_image/",
    "/bulk_catalog_thumbnail/",
    "/assets/",
)

_SKIP_POLL_PREFIXES = (
    "/boat/data",
    "/api/batch_fetch_status",
    "/api/batch_fetch_latest",
    "/api/china_ftp_task_status",
    "/api/china_directory/index_status",
    "/audit/api/status",
    "/print_center/status",
    "/4seller/session-status",
    "/v7_listcreator/html_job/",
    "/v7_listcreator/dropbox_desktop_status",
    "/export_workbook/jobs",
    "/dam/status",
    "/lori_automation/get_",
    "/tradeshowtool/lan_ping",
    "/login/lan-beacon",
)

_SKIP_ENDPOINTS_EXACT = frozenset(
    {
        "static",
        "robots_txt",
        "favicon",
        "auth.login",
        "auth.login_lan_beacon",
        "clientview.clientview_login",
    }
)

_PROBE_MARKERS = (
    "/wp-",
    "wp-login",
    "xmlrpc.php",
    "/.env",
    "/.git",
    "phpmyadmin",
    "/cgi-bin",
    "vendor/phpunit",
    "/actuator",
    "/solr",
    "/manager/html",
    "/owa/",
    "wp-content",
    "/config.json",
    "/debug/default",
    "/autodiscover",
    "/etc/passwd",
    "/eval-stdin",
    "/.aws",
    "/.docker",
)

_LOCAL_IPS = frozenset({"127.0.0.1", "::1", "localhost", "173.198.50.202"})


def _client_ip() -> str:
    xf = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    real = (request.headers.get("X-Real-IP") or "").strip()
    return xf or real or (request.remote_addr or "")


def _is_local_ip(ip: str) -> bool:
    value = (ip or "").strip().lower()
    return value in _LOCAL_IPS or value.startswith("127.")


def _username() -> str:
    return str(session.get("username") or session.get("user") or "").strip()


def _looks_like_probe(path: str) -> bool:
    lowered = (path or "").lower()
    return any(marker in lowered for marker in _PROBE_MARKERS)


def _should_skip(path: str, endpoint: str | None, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if endpoint in _SKIP_ENDPOINTS_EXACT or endpoint == "static":
        return True
    if path in _SKIP_EXACT:
        return True
    for prefix in _SKIP_PREFIXES:
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            return True
    for prefix in _SKIP_POLL_PREFIXES:
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            return True
    if method == "GET" and path.endswith("/status"):
        return True
    return False


def _action_label(method: str, path: str, status: int) -> str:
    if status in (401, 403):
        return "denied"
    if status == 404:
        return "not found"
    if status == 405:
        return "bad method"
    if status == 429:
        return "rate limited"
    if 400 <= status < 500:
        return "client error"
    if method == "GET":
        if path in ("", "/"):
            return "opened home menu"
        return "opened page" if _looks_like_page() else "called api"
    if method == "POST":
        return "submitted"
    if method == "DELETE":
        return "deleted"
    if method in ("PUT", "PATCH"):
        return "updated"
    return method.lower()


def _looks_like_page() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    if "text/html" in accept:
        return True
    if "application/json" in accept and "text/html" not in accept:
        return False
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return False
    return True


def _qs_part() -> str:
    qs = (request.query_string or b"").decode("utf-8", errors="replace").strip()
    if not qs:
        return ""
    if len(qs) > _QS_MAX:
        qs = qs[:_QS_MAX] + "…"
    return f" q={qs}"


def _is_login_redirect(response: Any) -> bool:
    loc = (getattr(response, "headers", None) or {}).get("Location") or ""
    try:
        path = urlparse(loc).path or loc
    except Exception:
        path = loc
    return "/login" in path.lower()


def _should_log_visitor(status: int, method: str, path: str, response: Any) -> bool:
    """Unauthenticated traffic that belongs on visitor.log (illegal / probe / blocked)."""
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    if status in (401, 403, 404, 405, 410, 429):
        return True
    if 400 <= status < 500:
        return True
    if status in (301, 302, 303, 307, 308) and _is_login_redirect(response):
        return True
    if _looks_like_probe(path):
        return True
    return False


def _prune(now: float) -> None:
    global _last_prune_mono
    if now - _last_prune_mono < _PRUNE_INTERVAL_SEC:
        return
    _last_prune_mono = now
    cutoff = now - 120.0
    stale = [k for k, t in _last_ts.items() if t < cutoff]
    for k in stale[:4000]:
        _last_ts.pop(k, None)


def _throttled(uid: str, method: str, path: str) -> bool:
    now = time.monotonic()
    _prune(now)
    key: LastKey = (uid, method, path)
    prev = _last_ts.get(key, 0.0)
    if now - prev < _THROTTLE_SEC:
        return True
    _last_ts[key] = now
    return False


def register_navigation_audit(app: Flask) -> None:
    def _touch_if_staff():
        uid = _username()
        if not uid:
            return
        try:
            from py.utilities.presence import touch_from_request

            touch_from_request()
        except Exception:
            pass

    @app.before_request
    def _touch_online_presence():
        method = (request.method or "GET").upper()
        path = request.path or ""
        endpoint = request.endpoint or ""
        if _should_skip(path, endpoint, method):
            return None
        _touch_if_staff()
        return None

    @app.after_request
    def _record_endpoint_access(response: Any) -> Any:
        method = (request.method or "GET").upper()
        path = request.path or ""
        endpoint = request.endpoint or ""
        if _should_skip(path, endpoint, method):
            return response

        uid = _username()
        if uid:
            _touch_if_staff()
            # Page visits belong in online duration, not User Activity.
            return response

        status = getattr(response, "status_code", 0) or 0
        ip = _client_ip()
        if not _should_log_visitor(status, method, path, response):
            return response
        if _is_local_ip(ip):
            return response
        if _throttled("anonymous", method, path):
            return response

        label = _action_label(method, path, status)
        ua = (request.headers.get("User-Agent") or "")[:180]
        line = (
            f"***anonymous***__@@@{label}@@@ "
            f"{method} path={path} endpoint={endpoint or '-'} "
            f"status={status} ip={ip}{_qs_part()}"
        )
        if ua:
            line += f" ua={ua}"
        try:
            from py.utilities.logging_setup import log_visitor_action

            log_visitor_action(line)
        except Exception:
            pass
        return response
