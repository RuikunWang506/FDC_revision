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
        now = time()
        with self._lock:
            hits = [t for t in self._hits[key] if now - t < window_sec]
            if len(hits) >= limit:
                self._hits[key] = hits
                return True
            hits.append(now)
            self._hits[key] = hits
            return False


auth_limiter = SlidingWindowLimiter()


def client_limit_key(ip: str | None, suffix: str) -> str:
    return f"{(ip or 'unknown').split(',')[0].strip()}:{suffix}"
