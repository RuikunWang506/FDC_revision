"""Persist per-order label creation status (Burlington / Jennifer)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app


def _status_path() -> Path:
    return Path(current_app.instance_path) / "label_status.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_order_number(order_number: str | int | None) -> str:
    s = str(order_number or "").strip()
    if not s.isdigit():
        return s
    n = int(s)
    if n <= 0:
        return s
    if n < 10000:
        return str(n).zfill(4)
    return str(n).zfill(5)


def _load_store() -> dict[str, Any]:
    path = _status_path()
    if not path.is_file():
        return {"burlington": {}, "jennifer": {}, "ez": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"burlington": {}, "jennifer": {}, "ez": {}}
    if not isinstance(data, dict):
        return {"burlington": {}, "jennifer": {}, "ez": {}}
    data.setdefault("burlington", {})
    data.setdefault("jennifer", {})
    data.setdefault("ez", {})
    return data


def _save_store(data: dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_burlington_labels(
    order_entries: list[dict[str, Any]],
    username: str | None = None,
) -> None:
    """Record Burlington labels from burlington_label_processor PO overview rows."""
    store = _load_store()
    bucket = store["burlington"]
    now = _utc_now()
    user = (username or "").strip() or "unknown"
    for row in order_entries:
        order_id = row.get("order_id")
        if not order_id:
            continue
        key = _normalize_order_number(order_id)
        po = str(row.get("po") or "").strip()
        existing = bucket.get(key) or {}
        pos = set(existing.get("po_numbers") or [])
        if po and po != "(no PO)":
            pos.add(po)
        bucket[key] = {
            "updated_at": now,
            "updated_by": user,
            "po_numbers": sorted(pos),
        }
    _save_store(store)


def mark_jennifer_label(order_number: str | int, username: str | None = None) -> None:
    """Record Jennifer label when labels are downloaded from order lookup (Excel or PDF)."""
    key = _normalize_order_number(order_number)
    if not key:
        return
    store = _load_store()
    store["jennifer"][key] = {
        "updated_at": _utc_now(),
        "updated_by": (username or "").strip() or "unknown",
    }
    _save_store(store)


def mark_ez_label(
    order_number: str | int,
    username: str | None = None,
    *,
    service: str | None = None,
    total_boxes: int | None = None,
) -> None:
    """Record ez简易贴纸 when generated from order lookup."""
    key = _normalize_order_number(order_number)
    if not key:
        return
    store = _load_store()
    store["ez"][key] = {
        "updated_at": _utc_now(),
        "updated_by": (username or "").strip() or "unknown",
        "service": (service or "").strip(),
        "total_boxes": int(total_boxes) if total_boxes is not None else None,
    }
    _save_store(store)


def get_label_status(order_number: str | int | None) -> dict[str, Any]:
    key = _normalize_order_number(order_number)
    store = _load_store()
    b = store["burlington"].get(key) if key else None
    j = store["jennifer"].get(key) if key else None
    e = store["ez"].get(key) if key else None
    return {
        "label_burlington": bool(b),
        "label_jennifer": bool(j),
        "label_ez": bool(e),
        "label_burlington_at": b.get("updated_at") if b else None,
        "label_jennifer_at": j.get("updated_at") if j else None,
        "label_ez_at": e.get("updated_at") if e else None,
        "label_burlington_by": b.get("updated_by") if b else None,
        "label_jennifer_by": j.get("updated_by") if j else None,
        "label_ez_by": e.get("updated_by") if e else None,
        "label_ez_service": e.get("service") if e else None,
        "label_ez_boxes": e.get("total_boxes") if e else None,
        "label_burlington_pos": b.get("po_numbers") if b else [],
    }


def enrich_header_info(header_info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not header_info:
        return header_info
    status = get_label_status(header_info.get("order_number"))
    header_info.update(status)
    return header_info
