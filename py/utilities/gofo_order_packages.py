"""MySQL persistence for GOFO order package dimensions / weights (order lookup)."""
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
        raise RuntimeError("MySQL connection unavailable.")
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gofo_order_packages (
                order_number VARCHAR(32) NOT NULL PRIMARY KEY,
                package_mode VARCHAR(16) NOT NULL DEFAULT 'mix',
                dim_unit VARCHAR(8) NOT NULL DEFAULT 'INCH',
                boxes_json LONGTEXT NOT NULL,
                updated_by VARCHAR(128) NULL,
                updated_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_gofo_pkg_updated (updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.commit()
        _SCHEMA_READY = True
    except MySQLError as exc:
        raise RuntimeError(f"Failed to ensure gofo_order_packages schema: {exc}") from exc
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def load_packages(order_number: str) -> dict[str, Any] | None:
    ensure_schema()
    order_number = str(order_number or "").strip()
    if not order_number:
        return None
    conn = get_mysql_connection()
    if not conn:
        raise RuntimeError("MySQL connection unavailable.")
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT order_number, package_mode, dim_unit, boxes_json, updated_by, updated_at
            FROM gofo_order_packages
            WHERE order_number = %s
            """,
            (order_number,),
        )
        row = cur.fetchone()
        if not row:
            return None
        boxes = []
        try:
            boxes = json.loads(row.get("boxes_json") or "[]")
        except (TypeError, ValueError):
            boxes = []
        if not isinstance(boxes, list):
            boxes = []
        return {
            "order_number": row["order_number"],
            "package_mode": (row.get("package_mode") or "mix").strip().lower(),
            "dim_unit": (row.get("dim_unit") or "INCH").strip().upper(),
            "boxes": boxes,
            "updated_by": row.get("updated_by") or "",
            "updated_at": row["updated_at"].isoformat(sep=" ") if row.get("updated_at") else "",
        }
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def save_packages(
    *,
    order_number: str,
    package_mode: str,
    dim_unit: str,
    boxes: list[dict[str, Any]],
    updated_by: str = "",
) -> dict[str, Any]:
    ensure_schema()
    order_number = str(order_number or "").strip()
    if not order_number:
        raise ValueError("order_number is required")

    mode = (package_mode or "mix").strip().lower()
    if mode not in ("mix", "whole"):
        mode = "mix"
    unit = (dim_unit or "INCH").strip().upper()
    if unit not in ("INCH", "CM"):
        unit = "INCH"

    cleaned_boxes: list[dict[str, Any]] = []
    for box in boxes or []:
        if not isinstance(box, dict):
            continue
        try:
            length = float(box.get("length") or 0)
            width = float(box.get("width") or 0)
            height = float(box.get("height") or 0)
            weight = float(box.get("weight") or 0)
            qty = int(box.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        if qty < 1:
            qty = 1
        cleaned_boxes.append(
            {
                "length": round(length, 2) if length else "",
                "width": round(width, 2) if width else "",
                "height": round(height, 2) if height else "",
                "weight": round(weight, 2) if weight else "",
                "qty": qty,
                "sku": str(box.get("sku") or "").strip()[:64],
                "style": str(box.get("style") or "").strip()[:64],
                "label": str(box.get("label") or "").strip()[:120],
            }
        )

    now = _utc_now()
    payload_json = json.dumps(cleaned_boxes, ensure_ascii=False)
    conn = get_mysql_connection()
    if not conn:
        raise RuntimeError("MySQL connection unavailable.")
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO gofo_order_packages
                (order_number, package_mode, dim_unit, boxes_json, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                package_mode = VALUES(package_mode),
                dim_unit = VALUES(dim_unit),
                boxes_json = VALUES(boxes_json),
                updated_by = VALUES(updated_by),
                updated_at = VALUES(updated_at)
            """,
            (order_number, mode, unit, payload_json, (updated_by or "")[:128], now),
        )
        conn.commit()
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    return {
        "order_number": order_number,
        "package_mode": mode,
        "dim_unit": unit,
        "boxes": cleaned_boxes,
        "updated_by": updated_by,
        "updated_at": now.isoformat(sep=" "),
    }
