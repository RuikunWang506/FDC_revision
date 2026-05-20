"""Local retention for burlington label processor uploads (original + processed PDFs)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import current_app

from py.other import burlington_label_processor_db as blp_db

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


def _job_has_pdfs(job_dir: Path) -> bool:
    return (job_dir / PROCESSED_NAME).is_file() or (job_dir / ORIGINAL_NAME).is_file()


def cleanup_old_jobs() -> int:
    """
    After RETENTION_DAYS, remove only original/processed PDFs.
    Job metadata is kept in MySQL for traceability.
    Returns count of jobs whose PDFs were removed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for job_dir in _jobs_root().iterdir():
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        if not _is_valid_job_id(job_id):
            continue
        meta_path = job_dir / META_NAME
        created = None
        meta: dict[str, Any] | None = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = _parse_created_at(meta.get("created_at", ""))
            except (OSError, json.JSONDecodeError):
                pass
        if created is None:
            created = datetime.fromtimestamp(job_dir.stat().st_mtime, tz=timezone.utc)
        if created >= cutoff:
            continue
        if meta:
            blp_db.save_job_record(job_id, meta)
        for name in (ORIGINAL_NAME, PROCESSED_NAME):
            path = job_dir / name
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        if meta_path.is_file():
            try:
                meta_path.unlink()
            except OSError:
                pass
        blp_db.mark_pdfs_removed(job_id)
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
    try:
        blp_db.save_job_record(job_id, meta, parse_result=parse_result)
    except Exception:
        current_app.logger.exception("Failed to save burlington label job to database")
    return job_id


def _meta_from_disk(job_dir: Path) -> dict[str, Any] | None:
    meta_path = job_dir / META_NAME
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    cleanup_old_jobs()
    db_items = blp_db.list_job_summaries(limit=limit)
    if db_items:
        for item in db_items:
            job_dir = _jobs_root() / str(item.get("id") or "")
            item["pdfs_available"] = _job_has_pdfs(job_dir) if job_dir.is_dir() else False
        return db_items

    items: list[dict[str, Any]] = []
    for job_dir in _jobs_root().iterdir():
        if not job_dir.is_dir():
            continue
        meta = _meta_from_disk(job_dir)
        if not meta:
            continue
        if not _job_has_pdfs(job_dir):
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
                "pdfs_available": True,
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
    if job_dir:
        meta = _meta_from_disk(job_dir)
        if meta:
            return meta
    return blp_db.load_job_meta(job_id)


def job_file_path(job_id: str, kind: str) -> Path | None:
    job_dir = _job_dir(job_id)
    if not job_dir:
        return None
    name = ORIGINAL_NAME if kind == "original" else PROCESSED_NAME
    path = job_dir / name
    return path if path.is_file() else None


def _order_id_candidates(order_number: str | int | None) -> set[str]:
    raw = str(order_number or "").strip()
    if not raw:
        return set()
    out = {raw}
    if raw.isdigit():
        n = int(raw)
        if n < 10000:
            out.add(str(n).zfill(4))
        out.add(str(n).zfill(5))
    return out


def find_job_id_by_query(query: str) -> str | None:
    """Resolve job id from DB, then on-disk meta (order / PO / BOL)."""
    job_id = blp_db.find_job_id_by_query(query)
    if job_id:
        return job_id
    return _find_job_id_on_disk_by_query(query)


def _find_job_id_on_disk_by_query(query: str) -> str | None:
    raw = str(query or "").strip()
    if not raw:
        return None
    raw_upper = raw.upper()
    order_candidates = _order_id_candidates(raw) if raw.isdigit() else {raw}

    best_id: str | None = None
    best_created: datetime | None = None
    for job_dir in _jobs_root().iterdir():
        if not job_dir.is_dir() or not _is_valid_job_id(job_dir.name):
            continue
        meta = _meta_from_disk(job_dir)
        if not meta:
            continue
        matched = False
        if order_candidates:
            orders = {str(x).strip() for x in (meta.get("order_numbers") or [])}
            for link in meta.get("po_links") or []:
                if link.get("order_id"):
                    orders.add(str(link["order_id"]).strip())
            if order_candidates & orders:
                matched = True
        for po in meta.get("po_numbers") or []:
            po_s = str(po).strip()
            if po_s == raw or raw in po_s or po_s in raw:
                matched = True
                break
        for link in meta.get("po_links") or []:
            po_s = str(link.get("po") or "").strip()
            if po_s == raw or raw in po_s:
                matched = True
                break
        for bol in meta.get("bill_of_lading") or []:
            bol_s = str(bol).strip().upper()
            if bol_s == raw_upper or raw_upper in bol_s:
                matched = True
                break
        if not matched:
            continue
        created = _parse_created_at(meta.get("created_at", "")) or datetime.fromtimestamp(
            job_dir.stat().st_mtime, tz=timezone.utc
        )
        if best_created is None or created > best_created:
            best_created = created
            best_id = job_dir.name
    return best_id


def find_job_id_on_disk(
    *,
    order_number: str | int | None = None,
    bol: str | None = None,
) -> str | None:
    """Find newest on-disk job meta matching order and/or BOL."""
    order_candidates = _order_id_candidates(order_number)
    bol_clean = str(bol or "").strip().upper()
    if not order_candidates and not bol_clean:
        return None

    best_id: str | None = None
    best_created: datetime | None = None
    for job_dir in _jobs_root().iterdir():
        if not job_dir.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", job_dir.name):
            continue
        meta = _meta_from_disk(job_dir)
        if not meta:
            continue
        matched = False
        if order_candidates:
            orders = {str(x).strip() for x in (meta.get("order_numbers") or [])}
            for link in meta.get("po_links") or []:
                if link.get("order_id"):
                    orders.add(str(link["order_id"]).strip())
            if order_candidates & orders:
                matched = True
        if bol_clean:
            bols = {str(x).strip().upper() for x in (meta.get("bill_of_lading") or [])}
            if bol_clean in bols:
                matched = True
            else:
                for part in bol_clean.split(","):
                    if part.strip() in bols:
                        matched = True
                        break
        if not matched:
            continue
        created = _parse_created_at(meta.get("created_at", "")) or datetime.fromtimestamp(
            job_dir.stat().st_mtime, tz=timezone.utc
        )
        if best_created is None or created > best_created:
            best_created = created
            best_id = job_dir.name
    return best_id


def delete_job(job_id: str) -> bool:
    """Delete a saved job folder and database record."""
    removed = False
    job_dir = _job_dir(job_id)
    if job_dir:
        import shutil

        shutil.rmtree(job_dir, ignore_errors=True)
        removed = not job_dir.exists()
    if blp_db.delete_job_record(job_id):
        removed = True
    return removed
