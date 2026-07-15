from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from flask import session

from py.utilities.database_utils import get_mysql_connection

_schema_ready = False


def _normalize_sku_key(sku: str) -> str:
    return " ".join(str(sku or "").strip().upper().split())


def _sku_style_key(sku: str) -> str:
    parts = _normalize_sku_key(sku).split()
    return parts[0] if parts else _normalize_sku_key(sku)


def _format_unique_costs(costs: list[float]) -> tuple[float | None, str | None]:
    unique = sorted({round(float(cost), 2) for cost in costs})
    if not unique:
        return None, None
    text = ", ".join(f"{cost:.2f}" for cost in unique)
    value = unique[0] if len(unique) == 1 else None
    return value, text


def ensure_bag_cost_override_table() -> None:
    global _schema_ready
    if _schema_ready:
        return
    connection = get_mysql_connection()
    if connection is None:
        return
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `4seller_tiktok_sku_bag_cost` (
                sku VARCHAR(120) NOT NULL PRIMARY KEY,
                bag_cost DECIMAL(12, 4) NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                updated_by VARCHAR(80) NULL
            )
            """
        )
        connection.commit()
        _schema_ready = True
    finally:
        cursor.close()
        connection.close()


def fetch_sku_bag_cost_overrides(skus: list[str] | None = None) -> dict[str, float]:
    ensure_bag_cost_override_table()
    connection = get_mysql_connection()
    if connection is None:
        return {}

    cursor = connection.cursor()
    try:
        if skus:
            keys = sorted({_normalize_sku_key(sku) for sku in skus if _normalize_sku_key(sku)})
            if not keys:
                return {}
            out: dict[str, float] = {}
            chunk_size = 500
            for start in range(0, len(keys), chunk_size):
                chunk = keys[start : start + chunk_size]
                placeholders = ", ".join(["%s"] * len(chunk))
                cursor.execute(
                    f"""
                    SELECT sku, bag_cost
                    FROM `4seller_tiktok_sku_bag_cost`
                    WHERE sku IN ({placeholders})
                    """,
                    chunk,
                )
                for sku, bag_cost in cursor.fetchall():
                    out[str(sku)] = round(float(bag_cost or 0), 4)
            return out

        cursor.execute("SELECT sku, bag_cost FROM `4seller_tiktok_sku_bag_cost`")
        return {
            str(sku): round(float(bag_cost or 0), 4)
            for sku, bag_cost in cursor.fetchall()
            if sku
        }
    finally:
        cursor.close()
        connection.close()


def save_sku_bag_cost_override(sku: str, bag_cost: Any, *, username: str = "") -> dict[str, Any]:
    ensure_bag_cost_override_table()
    sku_key = _normalize_sku_key(sku)
    if not sku_key:
        raise ValueError("SKU is required.")

    try:
        value = round(float(Decimal(str(bag_cost))), 4)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid bag cost value.") from exc
    if value < 0:
        raise ValueError("Bag cost cannot be negative.")

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO `4seller_tiktok_sku_bag_cost` (sku, bag_cost, updated_by)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                bag_cost = VALUES(bag_cost),
                updated_by = VALUES(updated_by),
                updated_at = CURRENT_TIMESTAMP
            """,
            (sku_key, value, username or None),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return {"sku": sku_key, "bag_cost": value}


def delete_sku_bag_cost_override(sku: str) -> bool:
    ensure_bag_cost_override_table()
    sku_key = _normalize_sku_key(sku)
    if not sku_key:
        raise ValueError("SKU is required.")

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    try:
        cursor.execute(
            "DELETE FROM `4seller_tiktok_sku_bag_cost` WHERE sku = %s",
            (sku_key,),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        cursor.close()
        connection.close()


def save_sku_bag_cost_override_for_user(sku: str, bag_cost: Any) -> dict[str, Any]:
    username = str(session.get("username") or "")
    return save_sku_bag_cost_override(sku, bag_cost, username=username)


def save_style_bag_cost_override_for_user(
    style: str,
    bag_cost: Any,
    variant_skus: list[str] | None = None,
) -> dict[str, Any]:
    style_key = _sku_style_key(style)
    result = save_sku_bag_cost_override_for_user(style_key, bag_cost)
    for sku in variant_skus or []:
        sku_key = _normalize_sku_key(sku)
        if sku_key and sku_key != style_key:
            delete_sku_bag_cost_override(sku_key)
    return result


def delete_style_bag_cost_override(style: str, variant_skus: list[str] | None = None) -> bool:
    style_key = _sku_style_key(style)
    removed = delete_sku_bag_cost_override(style_key)
    for sku in variant_skus or []:
        sku_key = _normalize_sku_key(sku)
        if sku_key and sku_key != style_key:
            removed = delete_sku_bag_cost_override(sku_key) or removed
    return removed


def fetch_unique_order_skus(filters: dict[str, Any] | None = None) -> list[str]:
    import importlib

    sales = importlib.import_module("py.other.4seller_sales")
    sales._ensure_4seller_orders_table()

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    where_sql, params = ("", [])
    if filters:
        where_sql, params = sales._build_db_where(filters)

    sku_clause = "sku IS NOT NULL AND TRIM(sku) <> ''"
    if where_sql:
        query = f"""
            SELECT DISTINCT sku
            FROM `4seller_orders`
            {where_sql} AND {sku_clause}
            ORDER BY sku
        """
    else:
        query = f"""
            SELECT DISTINCT sku
            FROM `4seller_orders`
            WHERE {sku_clause}
            ORDER BY sku
        """

    cursor = connection.cursor()
    try:
        cursor.execute(query, params)
        return [str(row[0]).strip() for row in cursor.fetchall() if row and str(row[0] or "").strip()]
    finally:
        cursor.close()
        connection.close()


def build_cost_map(filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    import importlib
    from collections import defaultdict

    finance = importlib.import_module("py.other.4seller_finance")
    skus = fetch_unique_order_skus(filters)
    style_groups: dict[str, list[str]] = defaultdict(list)
    for sku in skus:
        style = _sku_style_key(sku)
        if style:
            style_groups[style].append(sku)

    if not style_groups:
        return []

    all_skus = [sku for variants in style_groups.values() for sku in variants]
    system_costs = finance.fetch_sku_unit_costs(all_skus)
    sku_msrps = finance.fetch_sku_msrps(all_skus)
    lookup_keys = sorted(
        {
            key
            for sku in all_skus
            for key in (_normalize_sku_key(sku), _sku_style_key(sku))
            if key
        }
    )
    overrides = fetch_sku_bag_cost_overrides(lookup_keys)

    rows: list[dict[str, Any]] = []
    for style in sorted(style_groups.keys()):
        variant_skus = sorted(style_groups[style], key=lambda value: value.upper())
        variant_keys = [_normalize_sku_key(sku) for sku in variant_skus]
        system_values = [
            system_costs[key]
            for key in variant_keys
            if key in system_costs and system_costs[key] is not None
        ]
        system_cost, system_costs_text = _format_unique_costs(system_values)
        msrp_values = [
            sku_msrps[key]
            for key in variant_keys
            if key in sku_msrps and sku_msrps[key] is not None
        ]
        msrp, msrps_text = _format_unique_costs(msrp_values)

        if style in overrides:
            xingxing_cost = overrides[style]
            xingxing_costs_text = f"{xingxing_cost:.2f}"
            has_xingxing_cost = True
        else:
            variant_xingxing = [
                overrides[key]
                for key in variant_keys
                if key in overrides and overrides[key] is not None
            ]
            xingxing_cost, xingxing_costs_text = _format_unique_costs(variant_xingxing)
            has_xingxing_cost = bool(variant_xingxing)

        rows.append(
            {
                "style": style,
                "sku": style,
                "sku_key": style,
                "variant_skus": variant_skus,
                "system_cost": system_cost,
                "system_costs_text": system_costs_text,
                "msrp": msrp,
                "msrps_text": msrps_text,
                "xingxing_cost": xingxing_cost,
                "xingxing_costs_text": xingxing_costs_text,
                "has_xingxing_cost": has_xingxing_cost,
            }
        )
    return rows


def build_cost_map_all_available() -> list[dict[str, Any]]:
    from collections import defaultdict

    all_overrides = fetch_sku_bag_cost_overrides()
    style_groups: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for key, bag_cost in all_overrides.items():
        style = _sku_style_key(key)
        if style:
            style_groups[style].append((key, bag_cost))

    rows: list[dict[str, Any]] = []
    for style in sorted(style_groups.keys()):
        entries = style_groups[style]
        style_override = next(
            (cost for sku_key, cost in entries if _normalize_sku_key(sku_key) == style),
            None,
        )
        if style_override is not None:
            xingxing_cost = style_override
            xingxing_costs_text = f"{xingxing_cost:.2f}"
        else:
            xingxing_cost, xingxing_costs_text = _format_unique_costs([cost for _, cost in entries])

        variant_skus = sorted(
            {
                _normalize_sku_key(sku_key)
                for sku_key, _ in entries
                if _normalize_sku_key(sku_key) != style
            }
        )
        rows.append(
            {
                "style": style,
                "sku": style,
                "sku_key": style,
                "variant_skus": variant_skus,
                "xingxing_cost": xingxing_cost,
                "xingxing_costs_text": xingxing_costs_text,
                "has_xingxing_cost": True,
            }
        )
    return rows


def parse_bulk_cost_lines(text: str) -> list[dict[str, Any]]:
    import re

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "\t" in line:
            parts = [part.strip() for part in line.split("\t") if part.strip()]
        elif "," in line:
            parts = [part.strip() for part in re.split(r",\s*", line) if part.strip()]
        else:
            parts = line.split()

        if len(parts) < 2:
            continue

        cost_raw = parts[-1].replace("$", "").replace(",", "").strip()
        style = " ".join(parts[:-1]).strip()
        if not style:
            continue

        try:
            bag_cost = round(float(Decimal(cost_raw)), 4)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if bag_cost < 0:
            continue

        style_key = _sku_style_key(style)
        if not style_key or style_key in seen:
            continue
        seen.add(style_key)
        entries.append({"style": style_key, "bag_cost": bag_cost})
    return entries


def bulk_save_style_costs(entries: list[dict[str, Any]]) -> dict[str, Any]:
    saved = 0
    errors: list[str] = []
    for entry in entries:
        style = str(entry.get("style") or "").strip()
        if not style:
            continue
        try:
            save_sku_bag_cost_override_for_user(style, entry.get("bag_cost"))
            saved += 1
        except Exception as exc:
            errors.append(f"{style}: {exc}")
    return {"saved": saved, "errors": errors, "total": len(entries)}
