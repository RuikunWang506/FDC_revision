"""Process-wide DAM job lock. Claim before starting a thread; release in finally."""
from __future__ import annotations

from threading import Lock

_sync_run_lock = Lock()


def claim_sync_run(status: dict) -> bool:
    """Atomically mark a DAM job as running. Return False if one is already active."""
    with _sync_run_lock:
        if status.get("is_running"):
            return False
        status["is_running"] = True
        status["error"] = None
        status["logs"] = []
        status["current_progress"] = None
        return True


def release_sync_run(status: dict) -> None:
    with _sync_run_lock:
        status["is_running"] = False
