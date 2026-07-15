from __future__ import annotations

import re
from typing import Any

from py.utilities.database_utils import get_mysql_connection

_tags_schema_ready = False
MAX_TAG_LENGTH = 80


def _normalize_tag(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:MAX_TAG_LENGTH]


def ensure_order_tags_table() -> None:
    global _tags_schema_ready
    if _tags_schema_ready:
        return
    connection = get_mysql_connection()
    if connection is None:
        return
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `4seller_order_tags` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id BIGINT NOT NULL,
                tag VARCHAR(80) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_order_tag (order_id, tag),
                INDEX idx_order_id (order_id),
                INDEX idx_tag (tag)
            )
            """
        )
        connection.commit()
        _tags_schema_ready = True
    finally:
        cursor.close()
        connection.close()


def fetch_tags_for_order_ids(order_ids: list[int]) -> dict[int, list[str]]:
    ensure_order_tags_table()
    ids = sorted({int(order_id) for order_id in order_ids if order_id})
    if not ids:
        return {}

    connection = get_mysql_connection()
    if connection is None:
        return {}

    cursor = connection.cursor()
    tag_map: dict[int, list[str]] = {order_id: [] for order_id in ids}
    try:
        chunk_size = 500
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            placeholders = ", ".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT order_id, tag
                FROM `4seller_order_tags`
                WHERE order_id IN ({placeholders})
                ORDER BY tag
                """,
                chunk,
            )
            for order_id, tag in cursor.fetchall():
                tag_map[int(order_id)].append(str(tag))
    finally:
        cursor.close()
        connection.close()
    return tag_map


def attach_order_tags(orders: list[dict[str, Any]]) -> None:
    order_ids = [int(order["order_id"]) for order in orders if order.get("order_id") is not None]
    tag_map = fetch_tags_for_order_ids(order_ids)
    for order in orders:
        order_id = int(order.get("order_id") or 0)
        order["order_tags"] = tag_map.get(order_id, [])


def _normalize_tag_list(raw_tags: Any) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        parts = re.split(r"[,;\n]+", raw_tags)
    elif isinstance(raw_tags, list):
        parts = [str(item) for item in raw_tags]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = _normalize_tag(part)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def resolve_order_ids_from_platform_nos(platform_order_nos: list[str]) -> list[int]:
    ensure_order_tags_table()
    values = [str(value or "").strip() for value in platform_order_nos if str(value or "").strip()]
    if not values:
        return []

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    order_ids: list[int] = []
    try:
        chunk_size = 200
        for start in range(0, len(values), chunk_size):
            chunk = values[start : start + chunk_size]
            placeholders = ", ".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT DISTINCT order_id
                FROM `4seller_orders`
                WHERE platform_order_no IN ({placeholders})
                """,
                chunk,
            )
            order_ids.extend(int(row[0]) for row in cursor.fetchall() if row and row[0] is not None)
    finally:
        cursor.close()
        connection.close()
    return sorted(set(order_ids))


def add_tags_to_orders(order_ids: list[int], tags: list[str]) -> dict[str, Any]:
    ensure_order_tags_table()
    normalized_tags = _normalize_tag_list(tags)
    ids = sorted({int(order_id) for order_id in order_ids if order_id})
    if not ids:
        return {"orders_matched": 0, "tags_added": 0, "tags": []}
    if not normalized_tags:
        return {"orders_matched": len(ids), "tags_added": 0, "tags": []}

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    inserted = 0
    try:
        for order_id in ids:
            for tag in normalized_tags:
                cursor.execute(
                    """
                    INSERT IGNORE INTO `4seller_order_tags` (order_id, tag)
                    VALUES (%s, %s)
                    """,
                    (order_id, tag),
                )
                inserted += cursor.rowcount
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return {
        "orders_matched": len(ids),
        "tags_added": inserted,
        "tags": normalized_tags,
    }
