"""MySQL persistence for Burlington label processor jobs (traceability after PDF removal)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from mysql.connector import Error as MySQLError

from py.utilities.database_utils import get_mysql_connection

_SCHEMA_READY = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS burlington_label_jobs (
                id CHAR(32) NOT NULL PRIMARY KEY,
                created_at DATETIME NOT NULL,
                username VARCHAR(128) NULL,
                original_filename VARCHAR(512) NULL,
                download_name VARCHAR(512) NULL,
                page_count INT NULL,
                output_page_count INT NULL,
                parsed_count INT NULL,
                separator_count INT NULL,
                summary_label_count INT NULL,
                nonstandard_qty_count INT NULL,
                po_numbers_json TEXT NULL,
                bill_of_lading_json TEXT NULL,
                parse_result_json LONGTEXT NULL,
                pdfs_removed_at DATETIME NULL,
                created_at_db DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_blp_jobs_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS burlington_label_job_po (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                job_id CHAR(32) NOT NULL,
                po VARCHAR(64) NOT NULL DEFAULT '',
                order_id VARCHAR(16) NULL,
                bill_of_lading VARCHAR(256) NULL,
                page_count INT NULL,
                total_qty INT NULL,
                styles TEXT NULL,
                skus_json TEXT NULL,
                INDEX idx_blp_po_job (job_id),
                INDEX idx_blp_po_order (order_id),
                INDEX idx_blp_po_po (po),
                CONSTRAINT fk_blp_po_job
                    FOREIGN KEY (job_id) REFERENCES burlington_label_jobs(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.commit()
        _SCHEMA_READY = True
    except MySQLError:
        conn.rollback()
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def save_job_record(
    job_id: str,
    meta: dict[str, Any],
    *,
    parse_result: dict[str, Any] | None = None,
) -> bool:
    """Insert or replace job metadata and PO rows."""
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return False
    pr = parse_result if parse_result is not None else meta.get("parse_result") or {}
    created_raw = meta.get("created_at") or ""
    try:
        created_dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        if created_dt.tzinfo:
            created_dt = created_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        created_dt = _utc_now().replace(tzinfo=None)

    summary_pos = []
    for row in pr.get("po_detail") or []:
        style_set: set[str] = set()
        for sku_row in row.get("skus") or []:
            sku = str(sku_row.get("sku") or "").strip()
            if "-" in sku:
                style_set.add(sku.split("-", 1)[0].strip())
            elif sku:
                style_set.add(sku)
        summary_pos.append(
            {
                "po": row.get("po"),
                "order_id": row.get("order_id"),
                "bill_of_lading": row.get("bill_of_lading"),
                "page_count": row.get("page_count"),
                "total_qty": row.get("total_qty"),
                "styles": ", ".join(sorted(style_set)) if style_set else None,
                "skus": row.get("skus"),
            }
        )

    po_numbers = meta.get("po_numbers") or []
    bill_of_lading = meta.get("bill_of_lading") or []

    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO burlington_label_jobs (
                id, created_at, username, original_filename, download_name,
                page_count, output_page_count, parsed_count, separator_count,
                summary_label_count, nonstandard_qty_count,
                po_numbers_json, bill_of_lading_json, parse_result_json
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                username = VALUES(username),
                original_filename = VALUES(original_filename),
                download_name = VALUES(download_name),
                page_count = VALUES(page_count),
                output_page_count = VALUES(output_page_count),
                parsed_count = VALUES(parsed_count),
                separator_count = VALUES(separator_count),
                summary_label_count = VALUES(summary_label_count),
                nonstandard_qty_count = VALUES(nonstandard_qty_count),
                po_numbers_json = VALUES(po_numbers_json),
                bill_of_lading_json = VALUES(bill_of_lading_json),
                parse_result_json = VALUES(parse_result_json)
            """,
            (
                job_id,
                created_dt,
                meta.get("username"),
                meta.get("original_filename"),
                meta.get("download_name"),
                meta.get("page_count"),
                meta.get("output_page_count"),
                meta.get("parsed_count"),
                meta.get("separator_count"),
                meta.get("summary_label_count"),
                meta.get("nonstandard_qty_count"),
                json.dumps(po_numbers, ensure_ascii=False),
                json.dumps(bill_of_lading, ensure_ascii=False),
                json.dumps(pr, ensure_ascii=False),
            ),
        )
        cur.execute(
            "DELETE FROM burlington_label_job_po WHERE job_id = %s",
            (job_id,),
        )
        for row in summary_pos:
            cur.execute(
                """
                INSERT INTO burlington_label_job_po (
                    job_id, po, order_id, bill_of_lading,
                    page_count, total_qty, styles, skus_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    str(row.get("po") or ""),
                    str(row["order_id"]) if row.get("order_id") else None,
                    str(row["bill_of_lading"]) if row.get("bill_of_lading") else None,
                    row.get("page_count"),
                    row.get("total_qty"),
                    row.get("styles"),
                    json.dumps(row.get("skus") or [], ensure_ascii=False),
                ),
            )
        conn.commit()
        return True
    except MySQLError:
        conn.rollback()
        return False
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def mark_pdfs_removed(job_id: str) -> None:
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE burlington_label_jobs
            SET pdfs_removed_at = %s
            WHERE id = %s
            """,
            (_utc_now().replace(tzinfo=None), job_id),
        )
        conn.commit()
    except MySQLError:
        conn.rollback()
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def load_job_meta(job_id: str) -> dict[str, Any] | None:
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, created_at, username, original_filename, download_name,
                   page_count, output_page_count, parsed_count, separator_count,
                   summary_label_count, nonstandard_qty_count,
                   po_numbers_json, bill_of_lading_json, parse_result_json,
                   pdfs_removed_at
            FROM burlington_label_jobs
            WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        parse_result = {}
        if row.get("parse_result_json"):
            try:
                parse_result = json.loads(row["parse_result_json"])
            except json.JSONDecodeError:
                parse_result = {}
        po_numbers = []
        bill_of_lading = []
        try:
            if row.get("po_numbers_json"):
                po_numbers = json.loads(row["po_numbers_json"])
        except json.JSONDecodeError:
            pass
        try:
            if row.get("bill_of_lading_json"):
                bill_of_lading = json.loads(row["bill_of_lading_json"])
        except json.JSONDecodeError:
            pass
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            created_iso = created_at.replace(tzinfo=timezone.utc).isoformat()
        else:
            created_iso = str(created_at or "")
        po_links = []
        for pr in parse_result.get("po_detail") or []:
            po = pr.get("po")
            if po and po != "(no PO)":
                po_links.append({"po": po, "order_id": pr.get("order_id")})
        order_numbers = sorted(
            {
                str(pr.get("order_id"))
                for pr in parse_result.get("po_detail") or []
                if pr.get("order_id")
            }
        )
        return {
            "id": row["id"],
            "created_at": created_iso,
            "username": row.get("username"),
            "original_filename": row.get("original_filename"),
            "download_name": row.get("download_name"),
            "po_numbers": po_numbers,
            "po_links": po_links,
            "order_numbers": order_numbers,
            "bill_of_lading": bill_of_lading,
            "page_count": row.get("page_count"),
            "output_page_count": row.get("output_page_count"),
            "parsed_count": row.get("parsed_count"),
            "separator_count": row.get("separator_count"),
            "summary_label_count": row.get("summary_label_count"),
            "nonstandard_qty_count": row.get("nonstandard_qty_count"),
            "pdfs_removed_at": row.get("pdfs_removed_at"),
            "parse_result": parse_result,
        }
    except MySQLError:
        return None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def _load_po_links_by_job_ids(job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """PO + order_id rows for history sidebar (batch)."""
    if not job_ids:
        return {}
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return {}
    cur = None
    try:
        placeholders = ", ".join(["%s"] * len(job_ids))
        cur = conn.cursor(dictionary=True)
        cur.execute(
            f"""
            SELECT job_id, po, order_id
            FROM burlington_label_job_po
            WHERE job_id IN ({placeholders})
            ORDER BY po
            """,
            tuple(job_ids),
        )
        by_job: dict[str, list[dict[str, Any]]] = {}
        for row in cur.fetchall() or []:
            jid = str(row.get("job_id") or "")
            if not jid:
                continue
            by_job.setdefault(jid, []).append(
                {
                    "po": str(row.get("po") or ""),
                    "order_id": str(row["order_id"]) if row.get("order_id") else None,
                }
            )
        return by_job
    except MySQLError:
        return {}
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def _po_links_and_orders(
    po_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    po_links = [r for r in po_rows if r.get("po")]
    order_numbers = sorted(
        {str(r["order_id"]) for r in po_rows if r.get("order_id")}
    )
    return po_links, order_numbers


def list_job_summaries(limit: int = 50) -> list[dict[str, Any]]:
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, created_at, username, original_filename, download_name,
                   page_count, output_page_count, po_numbers_json, bill_of_lading_json,
                   pdfs_removed_at
            FROM burlington_label_jobs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall() or []
        job_ids = [str(row["id"]) for row in rows if row.get("id")]
        po_by_job = _load_po_links_by_job_ids(job_ids)
        items: list[dict[str, Any]] = []
        for row in rows:
            po_numbers = []
            bill_of_lading = []
            try:
                if row.get("po_numbers_json"):
                    po_numbers = json.loads(row["po_numbers_json"])
            except json.JSONDecodeError:
                pass
            try:
                if row.get("bill_of_lading_json"):
                    bill_of_lading = json.loads(row["bill_of_lading_json"])
            except json.JSONDecodeError:
                pass
            created_at = row.get("created_at")
            if isinstance(created_at, datetime):
                created_iso = created_at.replace(tzinfo=timezone.utc).isoformat()
            else:
                created_iso = str(created_at or "")
            jid = str(row["id"])
            po_links, order_numbers = _po_links_and_orders(po_by_job.get(jid, []))
            items.append(
                {
                    "id": row["id"],
                    "created_at": created_iso,
                    "username": row.get("username"),
                    "original_filename": row.get("original_filename"),
                    "download_name": row.get("download_name"),
                    "po_numbers": po_numbers,
                    "po_links": po_links,
                    "order_numbers": order_numbers,
                    "bill_of_lading": bill_of_lading,
                    "page_count": row.get("page_count"),
                    "output_page_count": row.get("output_page_count"),
                    "pdfs_available": row.get("pdfs_removed_at") is None,
                }
            )
        return items
    except MySQLError:
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def find_latest_job_id_for_bol(bol: str | None) -> str | None:
    """Most recent job whose PO rows or summary include this BOL."""
    bol_clean = str(bol or "").strip().upper()
    if not bol_clean:
        return None
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return None
    cur = None
    like = f"%{bol_clean}%"
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.job_id
            FROM burlington_label_job_po AS p
            INNER JOIN burlington_label_jobs AS j ON j.id = p.job_id
            WHERE UPPER(IFNULL(p.bill_of_lading, '')) LIKE %s
               OR UPPER(IFNULL(j.bill_of_lading_json, '')) LIKE %s
            ORDER BY j.created_at DESC
            LIMIT 1
            """,
            (like, like),
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except MySQLError:
        return None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def find_job_id_by_query(query: str) -> str | None:
    """Find newest job matching order #, PO #, or BOL (partial BOL/PO ok)."""
    raw = str(query or "").strip()
    if not raw:
        return None

    order_candidates: set[str] = {raw}
    if raw.isdigit():
        n = int(raw)
        if n < 10000:
            order_candidates.add(str(n).zfill(4))
        order_candidates.add(str(n).zfill(5))

    po_exact = raw
    po_like = f"%{raw}%"
    bol_like = f"%{raw.upper()}%"

    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return None
    cur = None
    try:
        order_ph = ", ".join(["%s"] * len(order_candidates))
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT p.job_id
            FROM burlington_label_job_po AS p
            INNER JOIN burlington_label_jobs AS j ON j.id = p.job_id
            WHERE p.order_id IN ({order_ph})
               OR p.po = %s
               OR p.po LIKE %s
               OR UPPER(IFNULL(p.bill_of_lading, '')) LIKE %s
               OR UPPER(IFNULL(j.bill_of_lading_json, '')) LIKE %s
            ORDER BY j.created_at DESC
            LIMIT 1
            """,
            (
                *tuple(order_candidates),
                po_exact,
                po_like,
                bol_like,
                bol_like,
            ),
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except MySQLError:
        return None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def find_latest_job_id_for_order(order_number: str | int | None) -> str | None:
    """Most recent Burlington label job that matched this order."""
    raw = str(order_number or "").strip()
    if not raw:
        return None
    candidates = {raw}
    if raw.isdigit():
        n = int(raw)
        if n < 10000:
            candidates.add(str(n).zfill(4))
        candidates.add(str(n).zfill(5))
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return None
    cur = None
    try:
        placeholders = ", ".join(["%s"] * len(candidates))
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT p.job_id
            FROM burlington_label_job_po AS p
            INNER JOIN burlington_label_jobs AS j ON j.id = p.job_id
            WHERE p.order_id IN ({placeholders})
            ORDER BY j.created_at DESC
            LIMIT 1
            """,
            tuple(candidates),
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except MySQLError:
        return None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def delete_job_record(job_id: str) -> bool:
    ensure_schema()
    conn = get_mysql_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM burlington_label_jobs WHERE id = %s", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    except MySQLError:
        conn.rollback()
        return False
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
