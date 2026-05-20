"""Local retention for burlington label processor uploads (original + processed PDFs)."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import current_app

RETENTION_DAYS = 5
ORIGINAL_NAME = "original.pdf"
PROCESSED_NAME = "processed.pdf"
META_NAME = "meta.json"


def _jobs_root() -> Path:
    root = Path(current_app.root_path) / "output" / "burlington_label_processor"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_created_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def cleanup_old_jobs() -> int:
    """Remove job folders older than RETENTION_DAYS. Returns count removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for job_dir in _jobs_root().iterdir():
        if not job_dir.is_dir():
            continue
        meta_path = job_dir / META_NAME
        created = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = _parse_created_at(meta.get("created_at", ""))
            except (OSError, json.JSONDecodeError):
                pass
        if created is None:
            created = datetime.fromtimestamp(job_dir.stat().st_mtime, tz=timezone.utc)
        if created < cutoff:
            shutil.rmtree(job_dir, ignore_errors=True)
            removed += 1
    return removed


def _summarize_parse_result(parse_result: dict[str, Any]) -> dict[str, Any]:
    po_numbers: set[str] = set()
    po_links_by_po: dict[str, str | None] = {}
    order_numbers: set[str] = set()
    bill_of_lading: set[str] = set()

    def _record_po(po: str | None, order_id: str | None = None) -> None:
        if not po or po == "(no PO)":
            return
        po_str = str(po)
        po_numbers.add(po_str)
        if po_str not in po_links_by_po:
            po_links_by_po[po_str] = str(order_id) if order_id else None
        elif order_id and not po_links_by_po[po_str]:
            po_links_by_po[po_str] = str(order_id)

    for page in parse_result.get("pages") or []:
        if page.get("po"):
            _record_po(str(page["po"]))
        if page.get("bill_of_lading"):
            bill_of_lading.add(str(page["bill_of_lading"]))

    for row in parse_result.get("po_overview") or []:
        _record_po(row.get("po"), row.get("order_id"))
        if row.get("order_id"):
            order_numbers.add(str(row["order_id"]))

    for row in parse_result.get("po_detail") or []:
        _record_po(row.get("po"), row.get("order_id"))
        bol = row.get("bill_of_lading")
        if bol:
            for part in str(bol).split(","):
                part = part.strip()
                if part:
                    bill_of_lading.add(part)
        if row.get("order_id"):
            order_numbers.add(str(row["order_id"]))

    po_links = [
        {"po": po, "order_id": po_links_by_po[po]}
        for po in sorted(po_links_by_po.keys())
    ]

    return {
        "po_numbers": sorted(po_numbers),
        "po_links": po_links,
        "order_numbers": sorted(order_numbers),
        "bill_of_lading": sorted(bill_of_lading),
    }


def save_job(
    *,
    original_bytes: bytes,
    processed_bytes: bytes,
    original_filename: str,
    download_name: str,
    username: str,
    parse_result: dict[str, Any],
) -> str:
    cleanup_old_jobs()
    job_id = uuid4().hex
    job_dir = _jobs_root() / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    summary = _summarize_parse_result(parse_result)
    meta = {
        "id": job_id,
        "created_at": _utc_now_iso(),
        "username": username,
        "original_filename": original_filename,
        "download_name": download_name,
        **summary,
        "page_count": parse_result.get("page_count"),
        "output_page_count": parse_result.get("output_page_count"),
        "parsed_count": parse_result.get("parsed_count"),
        "separator_count": parse_result.get("separator_count"),
        "summary_label_count": parse_result.get("summary_label_count"),
        "nonstandard_qty_count": parse_result.get("nonstandard_qty_count"),
        "parse_result": parse_result,
    }

    (job_dir / ORIGINAL_NAME).write_bytes(original_bytes)
    (job_dir / PROCESSED_NAME).write_bytes(processed_bytes)
    (job_dir / META_NAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return job_id


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    cleanup_old_jobs()
    items: list[dict[str, Any]] = []
    for job_dir in _jobs_root().iterdir():
        if not job_dir.is_dir():
            continue
        meta_path = job_dir / META_NAME
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not (job_dir / PROCESSED_NAME).is_file():
            continue
        items.append(
            {
                "id": meta.get("id", job_dir.name),
                "created_at": meta.get("created_at"),
                "username": meta.get("username"),
                "original_filename": meta.get("original_filename"),
                "download_name": meta.get("download_name"),
                "po_numbers": meta.get("po_numbers") or [],
                "po_links": meta.get("po_links") or [],
                "order_numbers": meta.get("order_numbers") or [],
                "bill_of_lading": meta.get("bill_of_lading") or [],
                "page_count": meta.get("page_count"),
                "output_page_count": meta.get("output_page_count"),
            }
        )
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items[:limit]


def _job_dir(job_id: str) -> Path | None:
    if not _is_valid_job_id(job_id):
        return None
    job_dir = _jobs_root() / job_id
    if not job_dir.is_dir():
        return None
    return job_dir


def _is_valid_job_id(job_id: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", job_id or ""))


def load_job_meta(job_id: str) -> dict[str, Any] | None:
    job_dir = _job_dir(job_id)
    if not job_dir:
        return None
    meta_path = job_dir / META_NAME
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def job_file_path(job_id: str, kind: str) -> Path | None:
    job_dir = _job_dir(job_id)
    if not job_dir:
        return None
    name = ORIGINAL_NAME if kind == "original" else PROCESSED_NAME
    path = job_dir / name
    return path if path.is_file() else None


def delete_job(job_id: str) -> bool:
    """Delete a saved job folder. Returns True if removed."""
    job_dir = _job_dir(job_id)
    if not job_dir:
        return False
    shutil.rmtree(job_dir, ignore_errors=True)
    return not job_dir.exists()
