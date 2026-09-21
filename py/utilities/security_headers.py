"""Baseline security headers for HTML responses."""
from __future__ import annotations

from flask import Flask, request

# Allow the CDNs templates already load (jsDelivr, DataTables, jQuery, unpkg, stackpath).
# blob: is required for Print Center PDF preview iframes and scanner workers.
# Cloudflare Insights is injected at the edge.
CSP_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https: http://127.0.0.1:5555 http://localhost:5555 http://192.168.254.30:5555; "
    "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com "
    "https://cdn.datatables.net https://cdn.jsdelivr.net https://stackpath.bootstrapcdn.com; "
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' https://cdnjs.cloudflare.com https://challenges.cloudflare.com "
    "https://cdn.datatables.net https://cdn.jsdelivr.net https://code.jquery.com "
    "https://unpkg.com https://stackpath.bootstrapcdn.com https://static.cloudflareinsights.com; "
    "connect-src 'self' blob: https://challenges.cloudflare.com https://ipinfo.io "
    "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://fonts.googleapis.com "
    "https://fonts.gstatic.com https://static.cloudflareinsights.com "
    "https://tessdata.projectnaptha.com https://unpkg.com; "
    "frame-src 'self' blob: https://challenges.cloudflare.com; "
    "worker-src 'self' blob: https://cdn.jsdelivr.net https://unpkg.com; "
    "object-src 'none'; "
    "base-uri 'self'"
)


def register_security_headers(app: Flask) -> None:
    @app.after_request
    def _set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(self), geolocation=()",
        )
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "").lower()
        if proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # 'unsafe-inline' is a known follow-up: templates still use inline JS/CSS.
        response.headers.setdefault("Content-Security-Policy", CSP_POLICY)
        return response
