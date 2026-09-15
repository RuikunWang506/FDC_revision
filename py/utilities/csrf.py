"""Session CSRF token for cookie-authenticated POST/PUT/PATCH/DELETE."""
from __future__ import annotations

import hmac
import secrets

from flask import Flask, abort, request, session

CSRF_SESSION_KEY = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

# Public or machine-authenticated POSTs (token/header auth, not browser session).
CSRF_EXEMPT_ENDPOINTS = frozenset(
    {
        "static",
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
