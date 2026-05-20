"""Persist DAM sync progress summaries (daily + complete rebuild)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

DAILY_MAX_ENTRIES = 10
REBUILD_RETENTION_DAYS = 30
DETAIL_LIST_CAP = 250
LOG_PREVIEW_LINES = 80

_lock = Lock()


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _instance_dir() -> str:
    d = os.path.join(_repo_root(), "instance")
    os.makedirs(d, exist_ok=True)
    return d


def _store_path() -> str:
    return os.path.join(_instance_dir(), "dam_progress_history.json")


def _logs_dir() -> str:
    d = os.path.join(_instance_dir(), "dam_progress_logs")
    os.makedirs(d, exist_ok=True)
    return d


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _load_store() -> dict[str, Any]:
    path = _store_path()
    if not os.path.isfile(path):
        return {"daily": [], "rebuild": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"daily": [], "rebuild": []}
    if not isinstance(data, dict):
        return {"daily": [], "rebuild": []}
    data.setdefault("daily", [])
    data.setdefault("rebuild", [])
    return data


def _save_store(data: dict[str, Any]) -> None:
    with open(_store_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _prune_daily(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = sorted(
        entries,
        key=lambda e: e.get("finished_at") or e.get("started_at") or "",
        reverse=True,
    )
    return entries[:DAILY_MAX_ENTRIES]


def _prune_rebuild(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=REBUILD_RETENTION_DAYS)
    kept: list[dict[str, Any]] = []
    for entry in entries:
        finished = _parse_iso(entry.get("finished_at")) or _parse_iso(entry.get("started_at"))
        if finished is None:
            kept.append(entry)
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if finished >= cutoff:
            kept.append(entry)
    return sorted(
        kept,
        key=lambda e: e.get("finished_at") or e.get("started_at") or "",
        reverse=True,
    )


def _cap_items(items: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], int]:
    if not items:
        return [], 0
    total = len(items)
    return items[:DETAIL_LIST_CAP], total


def _write_full_log(run_id: str, lines: list[str]) -> None:
    if not lines:
        return
    path = os.path.join(_logs_dir(), f"{run_id}.log")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass


def read_full_log(run_id: str) -> str | None:
    path = os.path.join(_logs_dir(), f"{run_id}.log")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def record_completed_run(
    *,
    kind: str,
    started_at: str,
    finished_at: str,
    log_source: str,
    triggered_by: str,
    options: dict[str, Any],
    stats: dict[str, Any] | None,
    added: list[dict[str, Any]] | None,
    inactivated: list[dict[str, Any]] | None,
    cleanup_count: int,
    error: str | None,
    log_lines: list[str] | None,
) -> dict[str, Any]:
    """Append one finished run; prune per kind rules. Returns saved entry."""
    run_id = uuid.uuid4().hex
    added_capped, added_total = _cap_items(added)
    inact_capped, inact_total = _cap_items(inactivated)
    st = stats or {}
    summary = (
        f"Added {st.get('total_added', 0)}, updated {st.get('total_updated', 0)}, "
        f"skipped {st.get('total_skipped', 0)}, errors {st.get('total_errors', 0)}"
    )
    if cleanup_count:
        summary += f"; cleanup marked inactive: {cleanup_count}"

    preview = (log_lines or [])[-LOG_PREVIEW_LINES:]

    entry: dict[str, Any] = {
        "id": run_id,
        "kind": kind,
        "started_at": started_at,
        "finished_at": finished_at,
        "log_source": log_source,
        "triggered_by": triggered_by,
        "options": options,
        "stats": st,
        "summary": summary,
        "added": added_capped,
        "added_total": added_total,
        "added_truncated": added_total > len(added_capped),
        "inactivated": inact_capped,
        "inactivated_total": inact_total,
        "inactivated_truncated": inact_total > len(inact_capped),
        "cleanup_count": cleanup_count,
        "error": error,
        "log_preview": preview,
        "has_full_log": bool(log_lines),
    }

    _write_full_log(run_id, log_lines or [])

    bucket = "rebuild" if kind == "rebuild" else "daily"
    with _lock:
        store = _load_store()
        store[bucket].insert(0, entry)
        if bucket == "daily":
            store["daily"] = _prune_daily(store["daily"])
        else:
            store["rebuild"] = _prune_rebuild(store["rebuild"])
        _save_store(store)

    return entry


def list_history(kind: str | None = None) -> dict[str, Any]:
    with _lock:
        store = _load_store()
    if kind == "daily":
        return {"daily": store.get("daily", [])}
    if kind == "rebuild":
        return {"rebuild": store.get("rebuild", [])}
    return {"daily": store.get("daily", []), "rebuild": store.get("rebuild", [])}


def get_entry(run_id: str) -> dict[str, Any] | None:
    with _lock:
        store = _load_store()
    for bucket in ("daily", "rebuild"):
        for entry in store.get(bucket, []):
            if entry.get("id") == run_id:
                out = dict(entry)
                full = read_full_log(run_id)
                if full is not None:
                    out["full_log"] = full
                return out
    return None
