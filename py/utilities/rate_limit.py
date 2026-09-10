"""In-process sliding-window rate limiter for auth endpoints."""
from __future__ import annotations

from collections import defaultdict
from threading import Lock
from time import time


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def too_many(self, key: str, limit: int, window_sec: int) -> bool:
        """Record a hit and return True when the key is over the limit."""
        if self.is_limited(key, limit, window_sec):
            return True
        self.record(key)
        return False

    def is_limited(self, key: str, limit: int, window_sec: int) -> bool:
        """Check the window without recording a new attempt."""
        now = time()
        with self._lock:
            hits = [t for t in self._hits[key] if now - t < window_sec]
            self._hits[key] = hits
            return len(hits) >= limit

    def record(self, key: str) -> None:
        now = time()
        with self._lock:
            self._hits[key].append(now)


auth_limiter = SlidingWindowLimiter()


def client_limit_key(ip: str | None, suffix: str) -> str:
    return f"{(ip or 'unknown').split(',')[0].strip()}:{suffix}"


def request_client_ip() -> str:
    """Client IP after Flask/ProxyFix. Do not read raw X-Forwarded-For."""
    try:
        from flask import has_request_context, request

        if has_request_context():
            return (request.remote_addr or "").strip() or "unknown"
    except Exception:
        pass
    return "unknown"
