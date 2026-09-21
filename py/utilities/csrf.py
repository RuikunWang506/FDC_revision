"""Session CSRF token for cookie-authenticated POST/PUT/PATCH/DELETE."""
from __future__ import annotations

import hmac
import re
import secrets
from html import escape as html_escape

from flask import Flask, abort, request, session

CSRF_SESSION_KEY = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

# Public or machine-authenticated POSTs (token/header auth, not browser session).
CSRF_EXEMPT_ENDPOINTS = frozenset(
    {
        "static",
        "auth.login_lan_beacon",
        "tradeshowtool.lan_ping",
        "tradeshowtool.lan_ping_png",
        "tradeshowtool.lan_claim",
        "whatnot_live.overlay",
        "whatnot_live.overlay_alias",
        "whatnot_live.camera",
        "whatnot_live.stream",
        "whatnot_live.ingest",
        "whatnot_live.public_state",
        "whatnot_live.webrtc_signal",
        "mobile_app.mobile_login",
    }
)


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


_CSRF_META_NAME_THEN_CONTENT = re.compile(
    r'(<meta\b[^>]*\bname=["\']csrf-token["\'][^>]*\bcontent=["\'])([^"\']*)(["\'])',
    re.IGNORECASE,
)
_CSRF_META_CONTENT_THEN_NAME = re.compile(
    r'(<meta\b[^>]*\bcontent=["\'])([^"\']*)(["\'][^>]*\bname=["\']csrf-token["\'])',
    re.IGNORECASE,
)
_CSRF_HIDDEN_INPUT = re.compile(
    r'(<input\b[^>]*\bname=["\']csrf_token["\'][^>]*\bvalue=["\'])([^"\']*)(["\'])',
    re.IGNORECASE,
)
FDC_CSRF_JS_URL = "/static/js/fdc-csrf.js?v=2"
_CSRF_SCRIPT_TAG = f'<script src="{FDC_CSRF_JS_URL}"></script>'
_CSRF_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bsrc=["\'][^"\']*fdc-csrf\.js(?:\?[^"\']*)?["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)


def rewrite_html_csrf_meta(html: str, token: str) -> str:
    """Replace a baked csrf-token meta (cached HTML snapshots) with the live session token.

    Snapshot files are rendered in a worker request context, so their meta tag is not the
    viewer's token. fetch()/XHR from those pages must send the current session value.
    """
    if not html or not token:
        return html or ""
    safe = html_escape(str(token), quote=True)

    def _sub(match: re.Match[str]) -> str:
        return match.group(1) + safe + match.group(3)

    if _CSRF_META_NAME_THEN_CONTENT.search(html):
        html = _CSRF_META_NAME_THEN_CONTENT.sub(_sub, html, count=1)
    elif _CSRF_META_CONTENT_THEN_NAME.search(html):
        html = _CSRF_META_CONTENT_THEN_NAME.sub(_sub, html, count=1)
    else:
        tags = f'<meta name="csrf-token" content="{safe}">\n'
        if not _CSRF_SCRIPT_RE.search(html):
            tags += _CSRF_SCRIPT_TAG + "\n"
        lower = html.lower()
        idx = lower.find("</head>")
        if idx < 0:
            idx = lower.find("<body")
        if idx >= 0:
            html = html[:idx] + tags + html[idx:]
        else:
            html = tags + html

    if _CSRF_SCRIPT_RE.search(html):
        html = _CSRF_SCRIPT_RE.sub(_CSRF_SCRIPT_TAG, html, count=1)

    if _CSRF_HIDDEN_INPUT.search(html):
        html = _CSRF_HIDDEN_INPUT.sub(_sub, html)
    return html


def _request_csrf_token() -> str:
    header = (request.headers.get(CSRF_HEADER) or "").strip()
    if header:
        return header
    form_token = (request.form.get(CSRF_FORM_FIELD) or "").strip()
    if form_token:
        return form_token
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            return str(body.get(CSRF_FORM_FIELD) or "").strip()
    return ""


def csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return None
    ep = request.endpoint or ""
    if ep in CSRF_EXEMPT_ENDPOINTS or ep.endswith(".static"):
        return None
    if (request.path or "").startswith("/dam/cron/"):
        return None
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        expected = get_csrf_token()
    got = _request_csrf_token()
    if not got or not hmac.compare_digest(got, expected):
        abort(400, description="Missing or invalid CSRF token")
    return None


def register_csrf(app: Flask) -> None:
    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": get_csrf_token}

    @app.before_request
    def _check_csrf():
        return csrf_protect()
