"""Persist per-order label creation status (Burlington / Jennifer)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import current_app, url_for


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


def _parse_bol_list(bol_display: str | None) -> list[str]:
    out: list[str] = []
    for part in str(bol_display or "").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


def _resolve_burlington_job_id(
    order_number: str | int | None,
    bols: list[str],
    stored_job_id: str | None,
) -> str | None:
    job_id = (stored_job_id or "").strip() or None
    if job_id:
        return job_id

    from py.other.burlington_label_processor_db import (
        find_latest_job_id_for_bol,
        find_latest_job_id_for_order,
    )
    from py.other.burlington_label_processor_storage import find_job_id_on_disk

    if order_number:
        job_id = find_latest_job_id_for_order(order_number)
        if job_id:
            return job_id
        job_id = find_job_id_on_disk(order_number=order_number)
        if job_id:
            return job_id

    for bol in bols:
        job_id = find_latest_job_id_for_bol(bol)
        if job_id:
            return job_id

    if bols:
        job_id = find_job_id_on_disk(bol=bols[0])
        if job_id:
            return job_id

    return None


def _burlington_processor_url(
    *,
    job_id: str | None,
    order_number: str | int | None,
    bol: str | None,
) -> str:
    base = url_for(
        "burlington_label_processor.burlington_label_processor_page",
        _external=False,
    )
    if job_id:
        return f"{base}?job={quote(job_id, safe='')}"
    params: list[str] = []
    if order_number:
        params.append(f"order={quote(str(order_number).strip(), safe='')}")
    if bol:
        params.append(f"bol={quote(str(bol).strip(), safe='')}")
    if params:
        return f"{base}?{'&'.join(params)}"
    return base


def mark_burlington_labels(
    po_detail_rows: list[dict[str, Any]],
    username: str | None = None,
    *,
    job_id: str | None = None,
) -> None:
    """Record Burlington labels and BOL from burlington_label_processor PO detail rows."""
    store = _load_store()
    bucket = store["burlington"]
    now = _utc_now()
    user = (username or "").strip() or "unknown"
    job_id = (job_id or "").strip() or None
    for row in po_detail_rows:
        order_id = row.get("order_id")
        if not order_id:
            continue
        key = _normalize_order_number(order_id)
        po = str(row.get("po") or "").strip()
        existing = bucket.get(key) or {}
        pos = set(existing.get("po_numbers") or [])
        if po and po != "(no PO)":
            pos.add(po)
        bols = set(existing.get("bill_of_lading") or [])
        bol_raw = str(row.get("bill_of_lading") or "").strip()
        if bol_raw:
            for part in bol_raw.split(","):
                part = part.strip()
                if part:
                    bols.add(part)
        entry = {
            "updated_at": now,
            "updated_by": user,
            "po_numbers": sorted(pos),
            "bill_of_lading": sorted(bols),
        }
        if job_id:
            entry["job_id"] = job_id
        bucket[key] = entry
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
    bol_display = (
        ", ".join(b.get("bill_of_lading") or []) if b and b.get("bill_of_lading") else None
    )
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
        "label_burlington_bol": bol_display,
        "label_burlington_job_id": b.get("job_id") if b else None,
    }


def enrich_header_info(header_info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not header_info:
        return header_info
    status = get_label_status(header_info.get("order_number"))
    header_info.update(status)

    bol_display = header_info.get("label_burlington_bol")
    if bol_display:
        bols = _parse_bol_list(bol_display)
        job_id = _resolve_burlington_job_id(
            header_info.get("order_number"),
            bols,
            header_info.get("label_burlington_job_id"),
        )
        if job_id:
            header_info["label_burlington_job_id"] = job_id
        header_info["label_burlington_url"] = _burlington_processor_url(
            job_id=job_id,
            order_number=header_info.get("order_number"),
            bol=bols[0] if bols else bol_display,
        )
    return header_info
