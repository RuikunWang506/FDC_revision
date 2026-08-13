from __future__ import annotations

import importlib
import json
import re
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Any

import pandas as pd
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import login_required

_fs = importlib.import_module("py.other.4_seller")

ORDER_PAGE_URL = "https://www.4seller.com/api/v2/order/page"
ORDER_HISTORY_PAGE_URL = "https://www.4seller.com/api/v2/order/history/page"

four_seller_sales_bp = Blueprint("four_seller_sales", __name__)

ORDER_STATUS_OPTIONS = [
    "all",
    "pending",
    "to_ship",
    "shipped",
    "delivered",
    "cancelled",
    "exception",
]

DEFAULT_DB_LIMIT = 50

ORDER_SORT_COLUMNS: dict[str, str] = {
    "paid_time": "MAX(COALESCE(paid_time, create_time))",
    "create_time": "MAX(create_time)",
    "ship_time": "MAX(ship_time)",
    "synced_at": "MAX(synced_at)",
    "platform_order_no": "MAX(platform_order_no)",
    "platform": "MAX(platform)",
    "order_status": "MAX(order_status)",
    "platform_order_status": "MAX(platform_order_status)",
    "order_id": "order_id",
    "shop_id": "MAX(shop_id)",
    "amount": "MAX(amount)",
    "currency": "MAX(currency)",
    "sku": "MIN(NULLIF(sku, ''))",
    "sku_line": "MIN(NULLIF(sku, ''))",
    "item_title": "MIN(NULLIF(item_title, ''))",
    "item_qty": "SUM(item_qty)",
    "item_price": "MAX(item_price)",
    "order_item_id": "MIN(order_item_id)",
    "shipping_method": "MAX(shipping_method)",
    "shipping_service": "MAX(shipping_service)",
    "platform_shipment_method": "MAX(platform_shipment_method)",
    "shipping_fee": "MAX(shipping_fee)",
    "shipping_label_price": "MAX(shipping_label_price)",
    "shipping_estimated_price": "MAX(shipping_estimated_price)",
    "tracking_no": "MAX(tracking_no)",
    "buyer_name": "MAX(buyer_name)",
    "buyer_state": "MAX(buyer_state)",
    "buyer_country": "MAX(buyer_country)",
    "orderPaidTime": "MAX(COALESCE(paid_time, create_time))",
    "finance_net_sales": "MAX(CAST(COALESCE(`tiktok_net_sales`, 0) AS DECIMAL(18,4)))",
    "finance_shipping": "MAX(CAST(COALESCE(`tiktok_shipping`, 0) AS DECIMAL(18,4)))",
    "finance_fees": "MAX(CAST(COALESCE(`tiktok_fees`, 0) AS DECIMAL(18,4)))",
    "finance_adjustment": "MAX(CAST(COALESCE(`tiktok_adjustment_amount`, 0) AS DECIMAL(18,4)))",
    "finance_final_payout": "MAX(CAST(COALESCE(`tiktok_total_settlement_amount`, 0) AS DECIMAL(18,4)))",
    "finance_other_fee": "MAX(ABS(COALESCE(CAST(shipping_label_price AS DECIMAL(18,4)), 0)))",
}
DEFAULT_ORDER_SORT = "paid_time"
FINANCE_DEFAULT_ORDER_SORT = "finance_final_payout"
FINANCE_MEMORY_SORT_COLUMNS = frozenset({"finance_bag_cost", "finance_xingxing_payout"})

SAVED_FILTER_LIST_KEYS = frozenset(
    {
        "order_statuses",
        "platforms",
        "platform_order_statuses",
        "shipping_methods",
        "shipping_services",
    }
)
SAVED_FILTER_STRING_KEYS = frozenset(
    {
        "shop_id",
        "order_id",
        "search_value",
        "sku",
        "item_title",
        "currency",
        "paid_start",
        "paid_end",
        "create_start",
        "create_end",
        "ship_start",
        "ship_end",
        "tracking_no",
        "buyer_name",
        "buyer_state",
        "buyer_country",
        "est_shipping_min",
        "est_shipping_max",
        "quick_search",
        "order_by",
        "desc",
        "client_platform",
        "client_status",
    }
)

_prefs_schema_ready = False


def _safe_json(response: requests.Response) -> Any:
    return _fs._safe_json(response)


def _session_from_cookie(user_cookie: str = "") -> tuple[requests.Session, str]:
    cookie_header, _from_cache = _fs._resolve_cookie_header_with_auto_login(user_cookie)
    if not cookie_header:
        raise RuntimeError("Missing 4Seller session. Configure FOURSELLER_EMAIL/PASSWORD or paste a cookie.")
    if not _fs._verify_4seller_session(_fs._build_cookie_session(cookie_header)):
        cookie_header = _fs._auto_login_cookie()
    if not cookie_header:
        raise RuntimeError("4Seller session invalid or expired.")
    return _fs._build_cookie_session(cookie_header), cookie_header


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _normalize_filters(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}

    def _date_start(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if len(value) == 10:
            return f"{value} 00:00:00"
        return value

    def _date_end(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if len(value) == 10:
            return f"{value} 23:59:59"
        return value

    def _parse_order_statuses(raw: dict[str, Any]) -> list[str]:
        allowed = {s for s in ORDER_STATUS_OPTIONS if s != "all"}
        if "order_statuses" in raw:
            value = raw.get("order_statuses")
            if isinstance(value, list):
                items = value
            elif isinstance(value, str):
                items = [part.strip() for part in value.split(",")]
            else:
                items = []
            return [str(item).strip().lower() for item in items if str(item).strip().lower() in allowed]

        single = str(raw.get("order_status", "all") or "all").strip().lower()
        if single in ("", "all"):
            return []
        return [single] if single in allowed else []

    def _parse_string_list(raw: dict[str, Any], key: str, *, legacy_key: str | None = None) -> list[str]:
        if key in raw:
            value = raw.get(key)
            if isinstance(value, list):
                items = value
            elif isinstance(value, str):
                items = [part.strip() for part in value.split(",")]
            else:
                items = []
            return [str(item).strip() for item in items if str(item).strip()]

        if legacy_key:
            legacy = str(raw.get(legacy_key, "") or "").strip()
            if legacy:
                return [legacy]
        return []

    order_statuses = _parse_order_statuses(raw)
    platforms = _parse_string_list(raw, "platforms", legacy_key="platform")
    platform_order_statuses = _parse_string_list(
        raw, "platform_order_statuses", legacy_key="platform_order_status"
    )
    shipping_methods = _parse_string_list(raw, "shipping_methods", legacy_key="shipping_method")
    shipping_services = _parse_string_list(raw, "shipping_services", legacy_key="shipping_service")

    return {
        "order_status": "all" if not order_statuses else order_statuses[0],
        "order_statuses": order_statuses,
        "platform": platforms[0] if len(platforms) == 1 else "",
        "platforms": platforms,
        "platform_order_status": platform_order_statuses[0] if len(platform_order_statuses) == 1 else "",
        "platform_order_statuses": platform_order_statuses,
        "shipping_method": shipping_methods[0] if len(shipping_methods) == 1 else "",
        "shipping_methods": shipping_methods,
        "shipping_service": shipping_services[0] if len(shipping_services) == 1 else "",
        "shipping_services": shipping_services,
        "shop_id": str(raw.get("shop_id", "") or "").strip(),
        "order_id": str(raw.get("order_id", "") or "").strip(),
        "search_type": str(raw.get("search_type", "orderNo") or "orderNo").strip(),
        "search_method": str(raw.get("search_method", "exact") or "exact").strip(),
        "search_value": str(raw.get("search_value", "") or "").strip(),
        "quick_search": str(raw.get("quick_search", "") or "").strip(),
        "sku": str(raw.get("sku", "") or "").strip(),
        "item_title": str(raw.get("item_title", "") or "").strip(),
        "item_qty_min": "",
        "item_price_min": "",
        "item_price_max": "",
        "currency": str(raw.get("currency", "") or "").strip(),
        "paid_start": _date_start(raw.get("paid_start", "")),
        "paid_end": _date_end(raw.get("paid_end", "")),
        "create_start": _date_start(raw.get("create_start", "")),
        "create_end": _date_end(raw.get("create_end", "")),
        "ship_start": _date_start(raw.get("ship_start", "")),
        "ship_end": _date_end(raw.get("ship_end", "")),
        "amount_min": "",
        "amount_max": "",
        "tracking_no": str(raw.get("tracking_no", "") or "").strip(),
        "buyer_name": str(raw.get("buyer_name", "") or "").strip(),
        "buyer_state": str(raw.get("buyer_state", "") or "").strip(),
        "buyer_country": str(raw.get("buyer_country", "") or "").strip(),
        "shipping_fee_min": "",
        "shipping_fee_max": "",
        "label_price_min": "",
        "label_price_max": "",
        "est_shipping_min": str(raw.get("est_shipping_min", "") or "").strip(),
        "est_shipping_max": str(raw.get("est_shipping_max", "") or "").strip(),
        "order_by": str(raw.get("order_by", DEFAULT_ORDER_SORT) or DEFAULT_ORDER_SORT).strip(),
        "desc": (
            "asc"
            if str(raw.get("desc", "desc") or "desc").strip().lower() in ("asc", "ascending", "0", "false")
            else "desc"
        ),
        "page_size": min(max(int(raw.get("page_size", 100) or 100), 1), 200),
        "max_pages": min(max(int(raw.get("max_pages", 50) or 50), 1), 200),
        "limit": _parse_db_limit(raw),
        "page": max(int(raw.get("page", 1) or 1), 1),
    }


def _parse_db_limit(raw: dict[str, Any]) -> int:
    value = raw.get("limit", DEFAULT_DB_LIMIT)
    if str(value).strip().lower() in ("all", "0"):
        return 0
    try:
        return min(max(int(value or DEFAULT_DB_LIMIT), 1), 200)
    except (TypeError, ValueError):
        return DEFAULT_DB_LIMIT


def _build_order_sort_clause(filters: dict[str, Any]) -> str:
    order_by = str(filters.get("order_by") or DEFAULT_ORDER_SORT).strip()
    sort_expr = ORDER_SORT_COLUMNS.get(order_by, ORDER_SORT_COLUMNS[DEFAULT_ORDER_SORT])
    direction = "ASC" if filters.get("desc") == "asc" else "DESC"
    tie_breaker = "ASC" if direction == "DESC" else "DESC"
    return f"{sort_expr} {direction}, order_id {tie_breaker}"


def _order_sort_needs_memory(filters: dict[str, Any], *, include_bag_cost: bool) -> bool:
    if not include_bag_cost:
        return False
    order_by = str(filters.get("order_by") or "").strip()
    return order_by in FINANCE_MEMORY_SORT_COLUMNS


def _memory_sort_value(order: dict[str, Any], order_by: str) -> float:
    finance = importlib.import_module("py.other.4seller_finance")
    if order_by == "finance_bag_cost":
        if finance.order_is_returned(order):
            return 0.0
        value = finance._to_float(order.get("finance_bag_cost"))
        return value if value is not None else float("-inf")
    if order_by == "finance_xingxing_payout":
        value = finance._to_float(order.get("finance_xingxing_payout"))
        if value is not None:
            return value
        payout = finance._to_float(order.get("finance_final_payout"))
        if payout is None:
            return float("-inf")
        bag = 0.0 if finance.order_is_returned(order) else (finance._to_float(order.get("finance_bag_cost")) or 0.0)
        return payout - bag
    return 0.0


def _sort_orders_in_memory(orders: list[dict[str, Any]], filters: dict[str, Any]) -> None:
    order_by = str(filters.get("order_by") or "").strip()
    reverse = str(filters.get("desc") or "desc").lower() != "asc"

    def sort_key(order: dict[str, Any]) -> tuple[Any, ...]:
        primary = _memory_sort_value(order, order_by)
        order_id = int(order.get("order_id") or 0)
        return (primary, -order_id if reverse else order_id)

    orders.sort(key=sort_key, reverse=reverse)


def _pagination_meta(matched_total: int, *, page: int, per_page: int) -> dict[str, int]:
    if matched_total <= 0:
        return {
            "page": 1,
            "per_page": per_page,
            "total_pages": 1,
            "matched_total": 0,
            "range_start": 0,
            "range_end": 0,
        }
    total_pages = max(1, (matched_total + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    range_start = (page - 1) * per_page + 1
    range_end = min(page * per_page, matched_total)
    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "matched_total": matched_total,
        "range_start": range_start,
        "range_end": range_end,
    }


def _build_page_payload(filters: dict[str, Any], *, page_current: int) -> dict[str, Any]:
    return {
        "pageCurrent": page_current,
        "pageSize": filters["page_size"],
        "orderStatus": filters["order_status"],
        "platform": filters["platform"],
        "shopIdList": [],
        "warehouseId": "",
        "searchType": filters["search_type"],
        "searchMethod": filters["search_method"],
        "searchValue": filters["search_value"],
        "orderBy": filters["order_by"],
        "desc": filters["desc"],
        "dateQueryPaidStartTime": filters["paid_start"],
        "dateQueryPaidEndTime": filters["paid_end"],
        "dateQueryShipStartTime": filters["ship_start"],
        "dateQueryShipEndTime": filters["ship_end"],
        "amountMin": filters["amount_min"],
        "amountMax": filters["amount_max"],
    }


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_base_fields(record: dict[str, Any]) -> dict[str, Any]:
    time_info = record.get("timeInfo") or {}
    addr = record.get("orderAddressVo") or {}
    shipping_info = record.get("shippingServiceInfo") or {}
    return {
        "order_id": record.get("orderId"),
        "platform_order_no": record.get("platformOrderNo") or record.get("platformOrderId"),
        "platform": record.get("platform"),
        "shop_id": record.get("shopId"),
        "order_status": record.get("orderStatus"),
        "platform_order_status": record.get("platformOrderStatus"),
        "amount": _float_or_none(record.get("amount")) or 0.0,
        "currency": record.get("currency"),
        "tracking_no": record.get("trackingNo"),
        "shipping_method": record.get("shippingMethod")
        or shipping_info.get("logisticsServiceName")
        or shipping_info.get("shippingService"),
        "platform_shipment_method": record.get("platformShipmentMethod"),
        "shipping_service": shipping_info.get("logisticsServiceName") or shipping_info.get("shippingService"),
        "shipping_fee": _float_or_none(shipping_info.get("shippingFee")),
        "shipping_label_price": _float_or_none(shipping_info.get("labelPrice")),
        "shipping_estimated_price": _float_or_none(shipping_info.get("estimatedShippingPrice")),
        "paid_time": time_info.get("orderPaidTime"),
        "ship_time": time_info.get("orderShippedTime"),
        "create_time": time_info.get("orderCreateTime"),
        "buyer_name": addr.get("name"),
        "buyer_state": addr.get("state"),
        "buyer_country": addr.get("countryCode"),
    }


def _flatten_order_lines(record: dict[str, Any]) -> list[dict[str, Any]]:
    base = _order_base_fields(record)
    items = record.get("orderItemInfoList") or []
    lines: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku") or item.get("sellerSku") or "").strip()
        lines.append(
            {
                **base,
                "order_item_id": item.get("orderItemId"),
                "sku": sku,
                "item_qty": int(item.get("quantity") or 0),
                "item_price": _float_or_none(item.get("itemPrice")),
                "item_title": item.get("title"),
            }
        )

    if not lines:
        lines.append(
            {
                **base,
                "order_item_id": None,
                "sku": "",
                "item_qty": 0,
                "item_price": None,
                "item_title": None,
            }
        )
    return lines


def _flatten_order(record: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible single-row view of first line item."""
    line = _flatten_order_lines(record)[0]
    return line


def _fetch_order_page(
    session: requests.Session,
    payload: dict[str, Any],
    *,
    page_url: str = ORDER_PAGE_URL,
) -> dict[str, Any]:
    response = session.post(page_url, json=payload, timeout=90)
    data = _safe_json(response)
    if not isinstance(data, dict) or data.get("code") != 0:
        raise RuntimeError(f"4Seller order page failed: {data}")
    inner = data.get("data") or {}
    if not isinstance(inner, dict):
        raise RuntimeError(f"Unexpected order page payload: {data}")
    return inner


def _sync_orders_from_api(
    session: requests.Session,
    *,
    page_url: str,
    page_size: int = 200,
    max_pages: int = 200,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    fetch_filters = _normalize_filters(
        {
            "order_status": "all",
            "page_size": page_size,
            "max_pages": max_pages,
        }
    )
    api_total = 0
    fetched = 0
    page_current = 1

    while page_current <= fetch_filters["max_pages"]:
        page_data = _fetch_order_page(
            session,
            _build_page_payload(fetch_filters, page_current=page_current),
            page_url=page_url,
        )
        api_total = int(page_data.get("total", 0) or 0)
        records = page_data.get("records") or []
        if not isinstance(records, list):
            break

        rows: list[dict[str, Any]] = []
        for record in records:
            if isinstance(record, dict):
                rows.extend(_flatten_order_lines(record))
        if rows:
            _upsert_orders(rows)
            fetched += len(rows)
            if progress_callback:
                progress_callback(page_current, api_total, fetched)

        if not records or fetched >= api_total:
            break
        page_current += 1

    stats = _get_db_stats()
    return {
        "fetched_from_api": fetched,
        "api_total": api_total,
        "stats": stats,
        "pages_fetched": page_current,
    }


def _fetch_all_recent_orders(session: requests.Session, filters: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    total = 0
    page_current = 1
    while page_current <= filters["max_pages"]:
        page_data = _fetch_order_page(
            session,
            _build_page_payload(filters, page_current=page_current),
            page_url=ORDER_PAGE_URL,
        )
        total = int(page_data.get("total", 0) or 0)
        records = page_data.get("records") or []
        if not isinstance(records, list):
            break
        for record in records:
            if isinstance(record, dict):
                rows.extend(_flatten_order_lines(record))
        if not records or len(rows) >= total:
            break
        page_current += 1
    return rows, total


def _ensure_4seller_orders_table() -> None:
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = '4seller_orders'
          AND COLUMN_NAME = 'skus'
        """
    )
    has_legacy_schema = cursor.fetchone()[0] > 0
    if has_legacy_schema:
        cursor.execute("DROP TABLE `4seller_orders`")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `4seller_orders` (
            id INT AUTO_INCREMENT PRIMARY KEY,
            order_id BIGINT NOT NULL,
            order_item_id BIGINT NULL,
            platform_order_no VARCHAR(120) NULL,
            platform VARCHAR(50) NULL,
            shop_id BIGINT NULL,
            order_status VARCHAR(50) NULL,
            platform_order_status VARCHAR(80) NULL,
            amount DECIMAL(12, 2) NULL,
            currency VARCHAR(10) NULL,
            tracking_no VARCHAR(120) NULL,
            shipping_method VARCHAR(200) NULL,
            platform_shipment_method VARCHAR(120) NULL,
            shipping_service VARCHAR(200) NULL,
            shipping_fee DECIMAL(12, 2) NULL,
            shipping_label_price DECIMAL(12, 2) NULL,
            shipping_estimated_price DECIMAL(12, 2) NULL,
            paid_time DATETIME NULL,
            ship_time DATETIME NULL,
            create_time DATETIME NULL,
            buyer_name VARCHAR(200) NULL,
            buyer_state VARCHAR(50) NULL,
            buyer_country VARCHAR(10) NULL,
            sku VARCHAR(120) NULL,
            item_qty INT NOT NULL DEFAULT 0,
            item_price DECIMAL(12, 2) NULL,
            item_title VARCHAR(500) NULL,
            synced_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_order_line (order_id, order_item_id),
            INDEX idx_paid_time (paid_time),
            INDEX idx_order_status (order_status),
            INDEX idx_platform (platform),
            INDEX idx_sku (sku)
        )
        """
    )
    connection.commit()
    cursor.close()
    connection.close()


def _row_to_dict(row: tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    data = dict(zip(columns, row))
    for key in ("paid_time", "ship_time", "create_time", "synced_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.strftime("%Y-%m-%d %H:%M:%S")
    if data.get("amount") is not None:
        data["amount"] = float(data["amount"])
    for key in ("item_price", "shipping_fee", "shipping_label_price", "shipping_estimated_price"):
        if data.get(key) is not None:
            data[key] = float(data[key])
    for key, value in list(data.items()):
        if key in {
            "order_id",
            "order_item_id",
            "shop_id",
            "item_qty",
            "id",
        }:
            continue
        if isinstance(value, Decimal):
            data[key] = float(value)
    return data


def _db_row_columns() -> list[str]:
    return [
        "order_id",
        "order_item_id",
        "platform_order_no",
        "platform",
        "shop_id",
        "order_status",
        "platform_order_status",
        "amount",
        "currency",
        "tracking_no",
        "shipping_method",
        "platform_shipment_method",
        "shipping_service",
        "shipping_fee",
        "shipping_label_price",
        "shipping_estimated_price",
        "paid_time",
        "ship_time",
        "create_time",
        "buyer_name",
        "buyer_state",
        "buyer_country",
        "sku",
        "item_qty",
        "item_price",
        "item_title",
        "synced_at",
    ]


_table_columns_cache: list[str] | None = None


def invalidate_table_columns_cache() -> None:
    global _table_columns_cache
    _table_columns_cache = None


def _get_select_columns() -> list[str]:
    global _table_columns_cache
    if _table_columns_cache is not None:
        return list(_table_columns_cache)

    _ensure_4seller_orders_table()
    connection = get_mysql_connection()
    if connection is None:
        return _db_row_columns()

    cursor = connection.cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM `4seller_orders`")
        columns = [str(row[0]) for row in cursor.fetchall() if str(row[0]) != "id"]
        _table_columns_cache = columns or _db_row_columns()
        return list(_table_columns_cache)
    finally:
        cursor.close()
        connection.close()


def _upsert_orders(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    _ensure_4seller_orders_table()
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    sql = """
        INSERT INTO `4seller_orders` (
            order_id, order_item_id, platform_order_no, platform, shop_id, order_status,
            platform_order_status, amount, currency, tracking_no, shipping_method,
            platform_shipment_method, shipping_service, shipping_fee, shipping_label_price,
            shipping_estimated_price, paid_time, ship_time, create_time, buyer_name,
            buyer_state, buyer_country, sku, item_qty, item_price, item_title
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        ON DUPLICATE KEY UPDATE
            platform_order_no = VALUES(platform_order_no),
            platform = VALUES(platform),
            shop_id = VALUES(shop_id),
            order_status = VALUES(order_status),
            platform_order_status = VALUES(platform_order_status),
            amount = VALUES(amount),
            currency = VALUES(currency),
            tracking_no = VALUES(tracking_no),
            shipping_method = VALUES(shipping_method),
            platform_shipment_method = VALUES(platform_shipment_method),
            shipping_service = VALUES(shipping_service),
            shipping_fee = VALUES(shipping_fee),
            shipping_label_price = VALUES(shipping_label_price),
            shipping_estimated_price = VALUES(shipping_estimated_price),
            paid_time = VALUES(paid_time),
            ship_time = VALUES(ship_time),
            create_time = VALUES(create_time),
            buyer_name = VALUES(buyer_name),
            buyer_state = VALUES(buyer_state),
            buyer_country = VALUES(buyer_country),
            sku = VALUES(sku),
            item_qty = VALUES(item_qty),
            item_price = VALUES(item_price),
            item_title = VALUES(item_title),
            synced_at = CURRENT_TIMESTAMP
    """

    values = []
    for row in rows:
        order_id = row.get("order_id")
        if not order_id:
            continue
        order_item_id = row.get("order_item_id")
        values.append(
            (
                int(order_id),
                int(order_item_id) if order_item_id is not None else 0,
                row.get("platform_order_no"),
                row.get("platform"),
                row.get("shop_id"),
                row.get("order_status"),
                row.get("platform_order_status"),
                row.get("amount"),
                row.get("currency"),
                row.get("tracking_no"),
                row.get("shipping_method"),
                row.get("platform_shipment_method"),
                row.get("shipping_service"),
                row.get("shipping_fee"),
                row.get("shipping_label_price"),
                row.get("shipping_estimated_price"),
                _parse_dt(row.get("paid_time")),
                _parse_dt(row.get("ship_time")),
                _parse_dt(row.get("create_time")),
                row.get("buyer_name"),
                row.get("buyer_state"),
                row.get("buyer_country"),
                row.get("sku"),
                int(row.get("item_qty") or 0),
                row.get("item_price"),
                row.get("item_title"),
            )
        )

    cursor = connection.cursor()
    if values:
        cursor.executemany(sql, values)
    connection.commit()
    affected = cursor.rowcount
    cursor.close()
    connection.close()
    return affected


def _build_db_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if filters["order_statuses"]:
        placeholders = ", ".join(["%s"] * len(filters["order_statuses"]))
        clauses.append(f"order_status IN ({placeholders})")
        params.extend(filters["order_statuses"])
    elif filters["order_status"] and filters["order_status"] != "all":
        clauses.append("order_status = %s")
        params.append(filters["order_status"])

    if filters["platforms"]:
        placeholders = ", ".join(["%s"] * len(filters["platforms"]))
        clauses.append(f"platform IN ({placeholders})")
        params.extend(filters["platforms"])
    elif filters["platform"]:
        clauses.append("platform = %s")
        params.append(filters["platform"])

    if filters["platform_order_statuses"]:
        placeholders = ", ".join(["%s"] * len(filters["platform_order_statuses"]))
        clauses.append(f"platform_order_status IN ({placeholders})")
        params.extend(filters["platform_order_statuses"])
    elif filters["platform_order_status"]:
        clauses.append("platform_order_status = %s")
        params.append(filters["platform_order_status"])

    if filters["shop_id"]:
        clauses.append("CAST(shop_id AS CHAR) LIKE %s")
        params.append(f"%{filters['shop_id']}%")

    if filters["order_id"]:
        clauses.append("CAST(order_id AS CHAR) LIKE %s")
        params.append(f"%{filters['order_id']}%")

    if filters["search_value"]:
        clauses.append("(platform_order_no = %s OR sku = %s)")
        params.extend([filters["search_value"], filters["search_value"]])

    if filters["quick_search"]:
        term = f"%{filters['quick_search']}%"
        quick_fields = [
            "platform_order_no",
            "platform",
            "order_status",
            "platform_order_status",
            "CAST(order_id AS CHAR)",
            "CAST(shop_id AS CHAR)",
            "sku",
            "item_title",
            "tracking_no",
            "buyer_name",
            "buyer_state",
            "buyer_country",
            "shipping_service",
            "shipping_method",
        ]
        clauses.append("(" + " OR ".join(f"{field} LIKE %s" for field in quick_fields) + ")")
        params.extend([term] * len(quick_fields))

    if filters["sku"]:
        clauses.append("sku LIKE %s")
        params.append(f"%{filters['sku']}%")

    if filters["item_title"]:
        clauses.append("item_title LIKE %s")
        params.append(f"%{filters['item_title']}%")

    if filters["currency"]:
        clauses.append("currency = %s")
        params.append(filters["currency"])

    if filters["paid_start"]:
        clauses.append("COALESCE(paid_time, create_time) >= %s")
        params.append(filters["paid_start"])

    if filters["paid_end"]:
        clauses.append("COALESCE(paid_time, create_time) <= %s")
        params.append(filters["paid_end"])

    if filters["create_start"]:
        clauses.append("create_time >= %s")
        params.append(filters["create_start"])

    if filters["create_end"]:
        clauses.append("create_time <= %s")
        params.append(filters["create_end"])

    if filters["ship_start"]:
        clauses.append("ship_time >= %s")
        params.append(filters["ship_start"])

    if filters["ship_end"]:
        clauses.append("ship_time <= %s")
        params.append(filters["ship_end"])

    if filters["tracking_no"]:
        clauses.append("tracking_no LIKE %s")
        params.append(f"%{filters['tracking_no']}%")

    if filters["buyer_name"]:
        clauses.append("buyer_name LIKE %s")
        params.append(f"%{filters['buyer_name']}%")

    if filters["buyer_state"]:
        clauses.append("buyer_state LIKE %s")
        params.append(f"%{filters['buyer_state']}%")

    if filters["buyer_country"]:
        clauses.append("buyer_country LIKE %s")
        params.append(f"%{filters['buyer_country']}%")

    if filters["shipping_methods"]:
        placeholders = ", ".join(["%s"] * len(filters["shipping_methods"]))
        clauses.append(f"shipping_method IN ({placeholders})")
        params.extend(filters["shipping_methods"])
    elif filters["shipping_method"]:
        clauses.append("shipping_method LIKE %s")
        params.append(f"%{filters['shipping_method']}%")

    if filters["shipping_services"]:
        placeholders = ", ".join(["%s"] * len(filters["shipping_services"]))
        clauses.append(f"shipping_service IN ({placeholders})")
        params.extend(filters["shipping_services"])
    elif filters["shipping_service"]:
        clauses.append("(shipping_service LIKE %s OR shipping_method LIKE %s)")
        params.extend([f"%{filters['shipping_service']}%", f"%{filters['shipping_service']}%"])

    if filters["est_shipping_min"]:
        clauses.append("shipping_estimated_price >= %s")
        params.append(float(filters["est_shipping_min"]))

    if filters["est_shipping_max"]:
        clauses.append("shipping_estimated_price <= %s")
        params.append(float(filters["est_shipping_max"]))

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def _aggregate_lines_by_order(line_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    order_sequence: list[int] = []

    for row in line_rows:
        order_id = int(row["order_id"])
        if order_id not in grouped:
            grouped[order_id] = {
                k: v
                for k, v in row.items()
                if k not in ("sku", "item_qty", "item_price", "item_title", "order_item_id")
            }
            grouped[order_id]["sku_lines"] = []
            order_sequence.append(order_id)

        sku = str(row.get("sku") or "").strip()
        item_qty = int(row.get("item_qty") or 0)
        if sku or item_qty:
            grouped[order_id]["sku_lines"].append(
                {
                    "sku": sku,
                    "item_qty": item_qty,
                    "item_price": row.get("item_price"),
                    "item_title": row.get("item_title"),
                    "order_item_id": row.get("order_item_id"),
                }
            )

    import importlib

    finance = importlib.import_module("py.other.4seller_finance")
    for order_id in order_sequence:
        grouped[order_id]["has_finance"] = finance.order_row_has_finance(grouped[order_id])
        finance.attach_finance_summary_fields(grouped[order_id])

    finance.attach_returned_flags([grouped[order_id] for order_id in order_sequence])

    return [grouped[order_id] for order_id in order_sequence]


def _get_db_stats() -> dict[str, int]:
    _ensure_4seller_orders_table()
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT
            COUNT(DISTINCT order_id) AS total,
            COUNT(DISTINCT CASE WHEN COALESCE(paid_time, create_time) >= CURDATE() THEN order_id END) AS today_count,
            COUNT(
                DISTINCT CASE
                    WHEN COALESCE(paid_time, create_time) >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
                    THEN order_id
                END
            ) AS week_count,
            COUNT(
                DISTINCT CASE
                    WHEN YEAR(COALESCE(paid_time, create_time)) = YEAR(CURDATE())
                     AND MONTH(COALESCE(paid_time, create_time)) = MONTH(CURDATE())
                    THEN order_id
                END
            ) AS month_count
        FROM `4seller_orders`
        """
    )
    row = cursor.fetchone()
    cursor.close()
    connection.close()
    return {
        "total": int(row[0] or 0),
        "today": int(row[1] or 0),
        "this_week": int(row[2] or 0),
        "this_month": int(row[3] or 0),
    }


def _query_lines_from_db(
    filters: dict[str, Any],
    *,
    order_ids: list[int] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    columns = _get_select_columns()
    select_cols = ", ".join(columns)

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()

    if order_ids:
        placeholders = ", ".join(["%s"] * len(order_ids))
        line_where = f"WHERE order_id IN ({placeholders})"
        line_params: list[Any] = list(order_ids)
    else:
        where_sql, params = _build_db_where(filters)
        line_where = where_sql
        line_params = list(params)

    query = f"""
        SELECT {select_cols}
        FROM `4seller_orders`
        {line_where}
        ORDER BY COALESCE(paid_time, create_time) DESC, order_id DESC, order_item_id DESC
    """
    if limit is not None:
        query += " LIMIT %s"
        line_params.append(limit)

    cursor.execute(query, line_params)
    rows = [_row_to_dict(row, columns) for row in cursor.fetchall()]
    cursor.close()
    connection.close()
    return rows


def _filters_without_dimension(filters: dict[str, Any], dimension: str) -> dict[str, Any]:
    out = dict(filters)
    if dimension == "order_status":
        out["order_status"] = "all"
        out["order_statuses"] = []
    elif dimension == "platform":
        out["platform"] = ""
        out["platforms"] = []
    elif dimension == "platform_order_status":
        out["platform_order_status"] = ""
        out["platform_order_statuses"] = []
    elif dimension == "shipping_method":
        out["shipping_method"] = ""
        out["shipping_methods"] = []
    elif dimension == "shipping_service":
        out["shipping_service"] = ""
        out["shipping_services"] = []
    return out


def _distinct_facet_values(filters: dict[str, Any], column: str, dimension: str) -> list[str]:
    _ensure_4seller_orders_table()
    facet_filters = _filters_without_dimension(filters, dimension)
    where_sql, params = _build_db_where(facet_filters)
    column_clause = f"{column} IS NOT NULL AND TRIM({column}) <> ''"
    if where_sql:
        query = f"""
            SELECT DISTINCT {column}
            FROM `4seller_orders`
            {where_sql} AND {column_clause}
            ORDER BY {column}
        """
    else:
        query = f"""
            SELECT DISTINCT {column}
            FROM `4seller_orders`
            WHERE {column_clause}
            ORDER BY {column}
        """

    connection = get_mysql_connection()
    if connection is None:
        return []

    cursor = connection.cursor()
    cursor.execute(query, params)
    values = [str(row[0]).strip() for row in cursor.fetchall() if row and row[0]]
    cursor.close()
    connection.close()
    return values


def _default_page_filters(saved: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(saved or {})
    if not str(raw.get("ship_start") or "").strip() and not str(raw.get("ship_end") or "").strip():
        today = datetime.now().date()
        raw.setdefault("ship_start", f"{today.year}-01-01")
        raw.setdefault("ship_end", today.isoformat())
    return _normalize_filters(raw)


def _get_filter_option_lists(filters: dict[str, Any] | None = None) -> dict[str, list[str]]:
    filters = filters or _normalize_filters({})
    return {
        "order_statuses": _distinct_facet_values(filters, "order_status", "order_status"),
        "platforms": _distinct_facet_values(filters, "platform", "platform"),
        "platform_order_statuses": _distinct_facet_values(
            filters, "platform_order_status", "platform_order_status"
        ),
        "shipping_methods": _distinct_facet_values(filters, "shipping_method", "shipping_method"),
        "shipping_services": _distinct_facet_values(filters, "shipping_service", "shipping_service"),
    }


def _query_orders_from_db(
    filters: dict[str, Any],
    *,
    include_bag_cost: bool = False,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    _ensure_4seller_orders_table()
    where_sql, params = _build_db_where(filters)

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    cursor.execute(f"SELECT COUNT(DISTINCT order_id) FROM `4seller_orders` {where_sql}", params)
    matched_total = int(cursor.fetchone()[0] or 0)
    page = filters["page"]
    memory_sort = _order_sort_needs_memory(filters, include_bag_cost=include_bag_cost)

    if memory_sort:
        per_page = matched_total if filters["limit"] <= 0 else filters["limit"]
        pagination = _pagination_meta(matched_total, page=page, per_page=per_page if per_page > 0 else max(matched_total, 1))
        cursor.execute(
            f"""
            SELECT order_id
            FROM `4seller_orders`
            {where_sql}
            GROUP BY order_id
            ORDER BY order_id ASC
            """,
            params,
        )
        order_ids = [int(row[0]) for row in cursor.fetchall()]
    elif filters["limit"] <= 0:
        order_sort = _build_order_sort_clause(filters)
        per_page = matched_total if matched_total > 0 else 1
        page = 1
        pagination = _pagination_meta(matched_total, page=page, per_page=per_page)
        cursor.execute(
            f"""
            SELECT order_id
            FROM `4seller_orders`
            {where_sql}
            GROUP BY order_id
            ORDER BY {order_sort}
            """,
            params,
        )
        order_ids = [int(row[0]) for row in cursor.fetchall()]
    else:
        order_sort = _build_order_sort_clause(filters)
        per_page = filters["limit"]
        pagination = _pagination_meta(matched_total, page=page, per_page=per_page)
        offset = (pagination["page"] - 1) * per_page
        cursor.execute(
            f"""
            SELECT order_id
            FROM `4seller_orders`
            {where_sql}
            GROUP BY order_id
            ORDER BY {order_sort}
            LIMIT %s OFFSET %s
            """,
            [*params, per_page, offset],
        )
        order_ids = [int(row[0]) for row in cursor.fetchall()]

    platform_where = (
        f"{where_sql} AND platform IS NOT NULL AND platform <> ''"
        if where_sql
        else "WHERE platform IS NOT NULL AND platform <> ''"
    )
    cursor.execute(
        f"SELECT DISTINCT platform FROM `4seller_orders` {platform_where}",
        params,
    )
    platforms = sorted({str(r[0]).strip() for r in cursor.fetchall() if r and r[0]})
    cursor.close()
    connection.close()

    if not order_ids:
        return [], matched_total, platforms, pagination

    line_rows = _query_lines_from_db({}, order_ids=order_ids)
    orders = _aggregate_lines_by_order(line_rows)
    order_map = {int(order["order_id"]): order for order in orders}
    orders = [order_map[order_id] for order_id in order_ids if order_id in order_map]
    if include_bag_cost:
        finance = importlib.import_module("py.other.4seller_finance")
        finance.attach_bag_cost_fields(orders)
        finance.attach_xingxing_finance_fields(orders)
        tags_mod = importlib.import_module("py.other.4seller_order_tags")
        tags_mod.attach_order_tags(orders)
    if memory_sort:
        _sort_orders_in_memory(orders, filters)
        if filters["limit"] > 0 and matched_total > 0:
            per_page = filters["limit"]
            page = min(max(1, filters["page"]), pagination["total_pages"])
            pagination = _pagination_meta(matched_total, page=page, per_page=per_page)
            start = (pagination["page"] - 1) * per_page
            orders = orders[start : start + per_page]
        elif filters["limit"] <= 0:
            pagination = _pagination_meta(
                matched_total,
                page=1,
                per_page=matched_total if matched_total > 0 else 1,
            )
    return orders, matched_total, platforms, pagination


def _query_all_lines_from_db(filters: dict[str, Any], *, max_rows: int = 50000) -> list[dict[str, Any]]:
    _ensure_4seller_orders_table()
    return _query_lines_from_db(filters, limit=max_rows)


def _sync_recent_orders_from_api(session: requests.Session) -> dict[str, Any]:
    return _sync_orders_from_api(session, page_url=ORDER_PAGE_URL, page_size=200, max_pages=200)


def _sync_historical_orders_from_api(
    session: requests.Session,
    *,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """One-time style import for orders older than 90 days."""
    return _sync_orders_from_api(
        session,
        page_url=ORDER_HISTORY_PAGE_URL,
        page_size=200,
        max_pages=200,
        progress_callback=progress_callback,
    )


def _query_all_orders_for_export(filters: dict[str, Any]) -> list[dict[str, Any]]:
    export_filters = dict(filters)
    export_filters["limit"] = 0
    export_filters["page"] = 1
    orders, _, _, _ = _query_orders_from_db(export_filters, include_bag_cost=True)
    return orders


def _resolve_order_ids_from_filters(filters: dict[str, Any]) -> list[int]:
    _ensure_4seller_orders_table()
    export_filters = dict(filters)
    export_filters["limit"] = 0
    export_filters["page"] = 1
    where_sql, params = _build_db_where(export_filters)

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            SELECT DISTINCT order_id
            FROM `4seller_orders`
            {where_sql}
            ORDER BY order_id
            """,
            params,
        )
        return [int(row[0]) for row in cursor.fetchall() if row and row[0] is not None]
    finally:
        cursor.close()
        connection.close()


def _parse_platform_order_nos(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"[,;\n]+", raw)
    elif isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        parts = []
    return [part.strip() for part in parts if str(part or "").strip()]


def _finance_export_numeric(order: dict[str, Any], finance: Any, field: str) -> float | None:
    if field == "finance_bag_cost":
        if finance.order_is_returned(order):
            return 0.0
        return finance._to_float(order.get("finance_bag_cost"))
    if field == "finance_xingxing_payout":
        value = finance._to_float(order.get("finance_xingxing_payout"))
        if value is not None:
            return value
        payout = finance._to_float(order.get("finance_final_payout"))
        if payout is None:
            return None
        bag = _finance_export_numeric(order, finance, "finance_bag_cost") or 0.0
        return round(payout - bag, 4)
    return finance._to_float(order.get(field))


def _format_finance_export_order_date(order: dict[str, Any]) -> str | None:
    value = order.get("create_time")
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        text = value.strftime("%Y-%m-%d %H:%M:%S")
    else:
        text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text)
    if match:
        return f"{match.group(1)}\n{match.group(2)}"
    return text


def _format_finance_platform_order_cell(order: dict[str, Any]) -> str:
    order_no = str(order.get("platform_order_no") or "").strip()
    platform = str(order.get("platform") or "").strip()
    if order_no and platform:
        return f"{order_no}\n{platform}"
    return order_no or platform


def _format_finance_order_tags(order: dict[str, Any]) -> str:
    tags = order.get("order_tags") or []
    if not isinstance(tags, list):
        return str(tags or "").strip()
    return "\n".join(str(tag).strip() for tag in tags if str(tag or "").strip())


def _format_finance_sku_lines(order: dict[str, Any]) -> str:
    """Match UI SKU / Qty / Price: `SKU X qty X $price`."""
    currency = str(order.get("currency") or "USD").upper()
    lines: list[str] = []
    for line in order.get("sku_lines") or []:
        sku = str(line.get("sku") or "").strip() or "—"
        qty = line.get("item_qty")
        if qty is None or qty == "":
            qty = 0
        price = line.get("item_price")
        if price is None or price == "":
            lines.append(f"{sku} X {qty}")
            continue
        try:
            price_value = f"{float(price):.2f}"
        except (TypeError, ValueError):
            price_value = str(price)
        price_text = f"${price_value}" if currency == "USD" else f"{currency} {price_value}"
        lines.append(f"{sku} X {qty} X {price_text}")
    return "\n".join(lines)


def _format_finance_export_date(order: dict[str, Any]) -> str | None:
    for key in ("paid_time", "ship_time", "create_time"):
        value = order.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        text = str(value).strip()
        if text:
            return text
    return None


def _attach_finance_cell_comment(cell: Any, text: str) -> None:
    from openpyxl.comments import Comment

    note = str(text or "").strip()
    if not note:
        return
    comment = Comment(note, "FDC")
    comment.width = 280
    comment.height = max(90, min(420, note.count("\n") * 16 + 50))
    cell.comment = comment


# Mirrors FINANCE_VIEW_COLUMNS on /4seller/sales/finance
FINANCE_VIEW_EXPORT_HEADERS: list[tuple[str, str, str | None]] = [
    ("Platform Order No / Platform", "平台单号", "platform_order_no"),
    ("Order Date", "订单日期", "create_time"),
    ("SKU / Qty / Price", "sku 数量 价格", "sku_line"),
    ("Net Sales", "销售额", "finance_net_sales"),
    ("Shipping", "运费", "finance_shipping"),
    ("Fees", "费用", "finance_fees"),
    ("Adjustment", "平台审计", "finance_adjustment"),
    ("Final Payout", "最终收入", "finance_final_payout"),
    ("Bag Cost", "包成本", "finance_bag_cost"),
    ("Other Fee", "其他费用", "finance_other_fee"),
    ("Xingxing Payout", "星星收入", "finance_xingxing_payout"),
    ("Tags", "标签", "order_tags"),
]
FINANCE_VIEW_NUMERIC_FIELDS = frozenset(
    {
        "finance_net_sales",
        "finance_shipping",
        "finance_fees",
        "finance_adjustment",
        "finance_final_payout",
        "finance_bag_cost",
        "finance_other_fee",
        "finance_xingxing_payout",
    }
)
FINANCE_VIEW_COLUMN_BY_ID = {field: label for label, _, field in FINANCE_VIEW_EXPORT_HEADERS}
FINANCE_VIEW_ZH_BY_ID = {field: zh for _, zh, field in FINANCE_VIEW_EXPORT_HEADERS}


def _finance_view_row_values(order: dict[str, Any], finance: Any) -> list[Any]:
    return [
        _format_finance_platform_order_cell(order),
        _format_finance_export_order_date(order),
        _format_finance_sku_lines(order),
        _finance_export_numeric(order, finance, "finance_net_sales"),
        _finance_export_numeric(order, finance, "finance_shipping"),
        _finance_export_numeric(order, finance, "finance_fees"),
        _finance_export_numeric(order, finance, "finance_adjustment"),
        _finance_export_numeric(order, finance, "finance_final_payout"),
        _finance_export_numeric(order, finance, "finance_bag_cost"),
        _finance_export_numeric(order, finance, "finance_other_fee"),
        _finance_export_numeric(order, finance, "finance_xingxing_payout"),
        _format_finance_order_tags(order),
    ]


def _write_finance_table_sheet(
    workbook: Any,
    *,
    columns: list[dict[str, Any]],
    rows: list[list[Any]],
    row_notes: list[dict[str, str]] | None = None,
    sheet_name: str = "Finance",
) -> None:
    """Write a Finance sheet from pre-built view columns (sums + labels + body)."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ws = workbook.create_sheet(sheet_name)
    if not columns:
        return

    sum_fill = PatternFill(start_color="F5F8FF", end_color="F5F8FF", fill_type="solid")
    sum_font = Font(bold=True, size=14, color="1F8A55")
    sum_font_negative = Font(bold=True, size=14, color="B24444")
    header_fill = PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid")
    header_font = Font(bold=True, color="2B3F75")
    zh_header_fill = PatternFill(start_color="F7F9FF", end_color="F7F9FF", fill_type="solid")
    zh_header_font = Font(bold=True, color="4A5F8C")
    returned_fill = PatternFill(start_color="FFF0F0", end_color="FFF0F0", fill_type="solid")
    thin = Border(
        left=Side(style="thin", color="D7DEEE"),
        right=Side(style="thin", color="D7DEEE"),
        top=Side(style="thin", color="D7DEEE"),
        bottom=Side(style="thin", color="D7DEEE"),
    )
    wrap_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    wrap_top = Alignment(wrap_text=True, vertical="top")

    # Row 1: sums (same position as the on-screen header sum line)
    for col_idx, column in enumerate(columns, start=1):
        cell = ws.cell(1, col_idx)
        cell.fill = sum_fill
        cell.border = thin
        cell.alignment = wrap_center
        sum_value = column.get("sum_value")
        sum_text = column.get("sum_text")
        is_numeric = bool(column.get("numeric"))
        if sum_value is not None and sum_value != "" and is_numeric:
            try:
                total = round(float(sum_value), 2)
            except (TypeError, ValueError):
                cell.value = sum_text or sum_value
                cell.font = sum_font
            else:
                cell.value = total
                cell.number_format = "#,##0.00"
                cell.font = sum_font_negative if total < 0 else sum_font
        elif sum_text:
            cell.value = sum_text
            cell.font = sum_font
        else:
            cell.value = None

    # Row 2: English labels
    for col_idx, column in enumerate(columns, start=1):
        cell = ws.cell(2, col_idx, column.get("label") or "")
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = wrap_center
        cell.border = thin

    # Row 3: Chinese labels
    for col_idx, column in enumerate(columns, start=1):
        col_id = str(column.get("id") or "")
        zh_label = str(column.get("label_zh") or FINANCE_VIEW_ZH_BY_ID.get(col_id) or "").strip()
        cell = ws.cell(3, col_idx, zh_label)
        cell.font = zh_header_font
        cell.fill = zh_header_fill
        cell.alignment = wrap_center
        cell.border = thin

    notes = row_notes or []

    # Data rows start at row 4
    for row_offset, row_values in enumerate(rows):
        row_idx = row_offset + 4
        is_returned = bool(row_values and len(row_values) > len(columns) and row_values[-1] is True)
        values = row_values[: len(columns)]
        cell_notes = notes[row_offset] if row_offset < len(notes) else {}
        for col_idx, value in enumerate(values, start=1):
            column = columns[col_idx - 1]
            cell = ws.cell(row_idx, col_idx, value)
            cell.border = thin
            if column.get("numeric") and isinstance(value, (int, float)) and not isinstance(value, bool):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="top")
            elif column.get("wrap"):
                cell.alignment = wrap_top
            else:
                cell.alignment = Alignment(vertical="top")
            if is_returned:
                cell.fill = returned_fill
            field_key = str(column.get("id") or "")
            if field_key and field_key in cell_notes:
                _attach_finance_cell_comment(cell, cell_notes[field_key])

    default_widths = {
        "platform_order_no": 24,
        "create_time": 14,
        "sku_line": 34,
        "finance_net_sales": 12,
        "finance_shipping": 12,
        "finance_fees": 10,
        "finance_adjustment": 12,
        "finance_final_payout": 14,
        "finance_bag_cost": 12,
        "finance_other_fee": 11,
        "finance_xingxing_payout": 14,
        "order_tags": 18,
    }
    for col_idx, column in enumerate(columns, start=1):
        col_id = str(column.get("id") or "")
        width = float(column.get("width") or default_widths.get(col_id) or 14)
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 28
    # Freeze sum + English + Chinese header rows
    ws.freeze_panes = "A4"
    last_data_row = max(3, len(rows) + 3)
    ws.auto_filter.ref = f"A2:{get_column_letter(len(columns))}{last_data_row}"


def _write_finance_export_sheet(workbook: Any, orders: list[dict[str, Any]]) -> None:
    finance = importlib.import_module("py.other.4seller_finance")

    columns: list[dict[str, Any]] = []
    field_keys = [field for _, _, field in FINANCE_VIEW_EXPORT_HEADERS]
    numeric_fields = [field for field in field_keys if field in FINANCE_VIEW_NUMERIC_FIELDS]

    sums = {field: 0.0 for field in numeric_fields}
    for order in orders:
        for field in sums:
            value = _finance_export_numeric(order, finance, field)
            if value is not None:
                sums[field] += value

    for label, label_zh, field in FINANCE_VIEW_EXPORT_HEADERS:
        column: dict[str, Any] = {
            "id": field,
            "label": label,
            "label_zh": label_zh,
            "numeric": field in FINANCE_VIEW_NUMERIC_FIELDS,
            "wrap": field in {"platform_order_no", "create_time", "sku_line", "order_tags"},
        }
        if field == "platform_order_no":
            column["sum_text"] = f"{len(orders)} orders"
        elif field in FINANCE_VIEW_NUMERIC_FIELDS:
            column["sum_value"] = round(sums[field], 2)
        columns.append(column)

    rows: list[list[Any]] = []
    row_notes: list[dict[str, str]] = []
    for order in orders:
        values = _finance_view_row_values(order, finance)
        values.append(bool(finance.order_is_returned(order)))
        rows.append(values)
        row_notes.append(finance.build_finance_export_cell_notes(order))

    _write_finance_table_sheet(workbook, columns=columns, rows=rows, row_notes=row_notes)


def _finance_view_to_xlsx_bytes(orders: list[dict[str, Any]]) -> bytes:
    """Export only the Finance table view (same headers as the UI table)."""
    from openpyxl import Workbook

    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    _write_finance_export_sheet(workbook, orders)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output.getvalue()


def _finance_table_payload_to_xlsx_bytes(payload: dict[str, Any]) -> bytes:
    """Build xlsx from a client-prepared finance table (current view columns + sums)."""
    from openpyxl import Workbook

    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    if not isinstance(columns, list) or not columns:
        raise ValueError("No columns provided for finance table export.")
    if not isinstance(rows, list):
        raise ValueError("Invalid finance table rows.")

    normalized_columns: list[dict[str, Any]] = []
    for column in columns:
        if not isinstance(column, dict):
            continue
        label = str(column.get("label") or "").strip()
        col_id = str(column.get("id") or "").strip()
        if not label and col_id:
            label = FINANCE_VIEW_COLUMN_BY_ID.get(col_id, col_id)
        if not label:
            continue
        normalized_columns.append(
            {
                "id": col_id,
                "label": label,
                "label_zh": column.get("label_zh") or FINANCE_VIEW_ZH_BY_ID.get(col_id, ""),
                "numeric": bool(column.get("numeric")),
                "wrap": bool(column.get("wrap")),
                "sum_text": column.get("sum_text"),
                "sum_value": column.get("sum_value"),
                "width": column.get("width"),
            }
        )
    if not normalized_columns:
        raise ValueError("No valid columns provided for finance table export.")

    normalized_rows: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        normalized_rows.append(row)

    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    _write_finance_table_sheet(workbook, columns=normalized_columns, rows=normalized_rows)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output.getvalue()


def _orders_to_xlsx_bytes(
    rows: list[dict[str, Any]],
    *,
    finance_orders: list[dict[str, Any]] | None = None,
) -> bytes:
    export_rows = [
        {
            "Platform Order No": r.get("platform_order_no"),
            "Platform": r.get("platform"),
            "Order Status": r.get("order_status"),
            "Platform Status": r.get("platform_order_status"),
            "Paid Time": r.get("paid_time"),
            "Ship Time": r.get("ship_time"),
            "Order Amount": r.get("amount"),
            "Currency": r.get("currency"),
            "SKU": r.get("sku"),
            "Item Qty": r.get("item_qty"),
            "Item Price": r.get("item_price"),
            "Item Title": r.get("item_title"),
            "Shipping Method": r.get("shipping_method"),
            "Shipping Service": r.get("shipping_service"),
            "Shipping Fee": r.get("shipping_fee"),
            "Label Price": r.get("shipping_label_price"),
            "Est. Shipping Price": r.get("shipping_estimated_price"),
            "Tracking No": r.get("tracking_no"),
            "Buyer": r.get("buyer_name"),
            "State": r.get("buyer_state"),
            "Country": r.get("buyer_country"),
            "Order ID": r.get("order_id"),
            "Order Item ID": r.get("order_item_id"),
        }
        for r in rows
    ]
    output = BytesIO()
    df = pd.DataFrame(export_rows)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Orders")
        if finance_orders is not None:
            _write_finance_export_sheet(writer.book, finance_orders)
    output.seek(0)
    return output.getvalue()


def _cookie_error_response(exc: Exception):
    error_text = str(exc)
    cookie_invalid = "Validation_Failed" in error_text or "invalid or expired" in error_text.lower()
    if cookie_invalid:
        _fs._delete_cookie_cache()
    return jsonify({"success": False, "cookie_required": cookie_invalid, "message": error_text}), 500


def _ensure_four_seller_sales_prefs_schema() -> None:
    global _prefs_schema_ready
    if _prefs_schema_ready:
        return
    connection = get_mysql_connection()
    if connection is None:
        return
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'four_seller_sales_prefs'
            """
        )
        if int(cursor.fetchone()[0] or 0) == 0:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN four_seller_sales_prefs JSON NULL DEFAULT NULL"
            )
            connection.commit()
        _prefs_schema_ready = True
    except Exception as exc:
        current_app.logger.warning(f"4Seller sales prefs schema ensure failed: {exc}")
    finally:
        cursor.close()
        connection.close()


def _sanitize_saved_filters(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = {}
    for key in SAVED_FILTER_LIST_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if not isinstance(value, list):
            continue
        out[key] = [str(item).strip() for item in value if str(item).strip()]

    for key in SAVED_FILTER_STRING_KEYS:
        if key not in raw:
            continue
        out[key] = str(raw.get(key) or "").strip()

    limit_raw = raw.get("limit")
    if str(limit_raw).strip().lower() in ("all", "0"):
        out["limit"] = 0
    else:
        try:
            limit_value = int(limit_raw)
            if limit_value == 0:
                out["limit"] = 0
            elif 1 <= limit_value <= 200:
                out["limit"] = limit_value
        except (TypeError, ValueError):
            pass

    return out


def _load_user_sales_filters(username: str) -> dict[str, Any]:
    username = str(username or "").strip()
    if not username:
        return {}
    _ensure_four_seller_sales_prefs_schema()
    connection = get_mysql_connection()
    if connection is None:
        return {}
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT four_seller_sales_prefs FROM users WHERE username = %s",
            (username,),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return {}
        raw = row[0]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            raw = json.loads(raw)
        return _sanitize_saved_filters(raw)
    except Exception as exc:
        current_app.logger.warning(f"4Seller sales filter load failed: {exc}")
        return {}
    finally:
        cursor.close()
        connection.close()


def _save_user_sales_filters(username: str, filters: dict[str, Any]) -> None:
    username = str(username or "").strip()
    if not username:
        raise RuntimeError("Missing username.")
    cleaned = _sanitize_saved_filters(filters)
    _ensure_four_seller_sales_prefs_schema()
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET four_seller_sales_prefs = %s WHERE username = %s",
            (json.dumps(cleaned, ensure_ascii=False), username),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@four_seller_sales_bp.route("/4seller/sales", methods=["GET"])
@login_required
def four_seller_sales_page():
    return _render_sales_page(finance_view=False)


@four_seller_sales_bp.route("/4seller/sales/finance", methods=["GET"])
@login_required
def four_seller_sales_finance_page():
    return _render_sales_page(finance_view=True)


def _render_sales_page(*, finance_view: bool):
    saved_filters = _load_user_sales_filters(session.get("username", ""))
    page_filters = _default_page_filters(saved_filters)
    filter_options = _get_filter_option_lists(page_filters)
    return render_template(
        "4seller_sales.html",
        current_user=session.get("username"),
        order_status_options=ORDER_STATUS_OPTIONS,
        filter_order_statuses=filter_options["order_statuses"],
        filter_platforms=filter_options["platforms"],
        filter_platform_statuses=filter_options["platform_order_statuses"],
        filter_shipping_methods=filter_options["shipping_methods"],
        filter_shipping_services=filter_options["shipping_services"],
        filter_options=filter_options,
        finance_platforms=_get_finance_platform_options(),
        auto_login_configured=_fs._credentials_configured(),
        saved_filters=saved_filters,
        finance_view=finance_view,
    )


@four_seller_sales_bp.route("/4seller/sales/filters", methods=["GET", "POST"])
@login_required
def four_seller_sales_filters():
    username = session.get("username", "")
    if request.method == "GET":
        return jsonify({"success": True, "filters": _load_user_sales_filters(username)})

    payload = request.get_json(silent=True) or {}
    try:
        _save_user_sales_filters(username, payload.get("filters") or {})
        return jsonify({"success": True})
    except Exception as exc:
        current_app.logger.error(f"4Seller sales filter save error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/orders", methods=["POST"])
@login_required
def four_seller_sales_orders():
    payload = request.get_json(silent=True) or {}
    filters = _normalize_filters(payload.get("filters"))
    include_bag_cost = bool(payload.get("finance_view"))

    try:
        rows, matched_total, platforms, pagination = _query_orders_from_db(
            filters,
            include_bag_cost=include_bag_cost,
        )
        stats = _get_db_stats()
        filter_options = _get_filter_option_lists(filters)
        return jsonify(
            {
                "success": True,
                "orders": rows,
                "matched_total": matched_total,
                "loaded": len(rows),
                "platforms": platforms,
                "filter_options": filter_options,
                "stats": stats,
                "pagination": pagination,
                "source": "database",
            }
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller sales orders query error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/fetch", methods=["POST"])
@login_required
def four_seller_sales_fetch():
    payload = request.get_json(silent=True) or {}
    user_cookie = str(payload.get("cookie", "") or "").strip()

    try:
        session_client, _cookie_header = _session_from_cookie(user_cookie)
        result = _sync_recent_orders_from_api(session_client)
        filters = _normalize_filters(payload.get("filters"))
        include_bag_cost = bool(payload.get("finance_view"))
        rows, matched_total, platforms, pagination = _query_orders_from_db(
            filters,
            include_bag_cost=include_bag_cost,
        )
        return jsonify(
            {
                "success": True,
                "orders": rows,
                "matched_total": matched_total,
                "loaded": len(rows),
                "platforms": platforms,
                "filter_options": _get_filter_option_lists(filters),
                "stats": result["stats"],
                "pagination": pagination,
                "fetched_from_api": result["fetched_from_api"],
                "api_total": result["api_total"],
                "fetched_at": datetime.now().isoformat(),
            }
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller sales fetch error: {exc}")
        return _cookie_error_response(exc)


@four_seller_sales_bp.route("/4seller/sales/export", methods=["POST"])
@login_required
def four_seller_sales_export():
    payload = request.get_json(silent=True) or {}
    export_scope = str(payload.get("export_scope", "filtered") or "filtered").strip().lower()
    finance_view = bool(payload.get("finance_view"))
    if export_scope == "all":
        filters = _normalize_filters({})
    else:
        filters = _normalize_filters(payload.get("filters"))
    filters["limit"] = 50000

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if finance_view:
            # Same order rows + headers as the Finance View table below.
            finance_orders = _query_all_orders_for_export(filters)
            xlsx_bytes = _finance_view_to_xlsx_bytes(finance_orders)
            filename = f"4seller_orders_finance_{timestamp}.xlsx"
        else:
            rows = _query_all_lines_from_db(filters)
            include_finance = export_scope == "filtered"
            finance_orders = _query_all_orders_for_export(filters) if include_finance else None
            xlsx_bytes = _orders_to_xlsx_bytes(rows, finance_orders=finance_orders)
            filename = (
                f"4seller_orders_finance_{timestamp}.xlsx"
                if include_finance
                else f"4seller_orders_{timestamp}.xlsx"
            )
        output = BytesIO(xlsx_bytes)
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller sales export error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/export/finance-table", methods=["POST"])
@login_required
def four_seller_sales_export_finance_table():
    """Export a client-built Finance View table (visible columns, sums, formatting)."""
    payload = request.get_json(silent=True) or {}
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        xlsx_bytes = _finance_table_payload_to_xlsx_bytes(payload)
        output = BytesIO(xlsx_bytes)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"4seller_orders_finance_filtered_{timestamp}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller finance table export error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


def _get_finance_platform_options() -> list[str]:
    _ensure_4seller_orders_table()
    connection = get_mysql_connection()
    if connection is None:
        return ["TIKTOK"]
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT DISTINCT platform
            FROM `4seller_orders`
            WHERE platform IS NOT NULL AND TRIM(platform) <> ''
            ORDER BY platform
            """
        )
        platforms = [str(row[0]).strip() for row in cursor.fetchall() if row and row[0]]
        return platforms or ["TIKTOK"]
    finally:
        cursor.close()
        connection.close()


@four_seller_sales_bp.route("/4seller/sales/finance/upload", methods=["POST"])
@login_required
def four_seller_sales_finance_upload():
    import importlib

    finance = importlib.import_module("py.other.4seller_finance")
    platform = str(request.form.get("platform", "") or "").strip()
    if not platform:
        return jsonify({"success": False, "message": "Platform is required."}), 400

    uploads = request.files.getlist("files")
    if not uploads:
        single = request.files.get("file")
        uploads = [single] if single and single.filename else []

    files: list[tuple[str, bytes]] = []
    for upload in uploads:
        filename = str(upload.filename or "").strip()
        if not filename:
            continue
        if not filename.lower().endswith((".xlsx", ".xls")):
            return jsonify({"success": False, "message": f"Unsupported file type: {filename}"}), 400
        files.append((filename, upload.read()))

    if not files:
        return jsonify({"success": False, "message": "Upload at least one Excel file."}), 400

    try:
        result = finance.process_finance_upload(platform, files)
        return jsonify({"success": True, **result})
    except Exception as exc:
        current_app.logger.error(f"4Seller finance upload error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/tags", methods=["POST"])
@login_required
def four_seller_sales_tags():
    tags_mod = importlib.import_module("py.other.4seller_order_tags")
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags")
    apply_filter = bool(payload.get("apply_filter"))

    try:
        if apply_filter:
            filters = _normalize_filters(payload.get("filters") or {})
            order_ids = _resolve_order_ids_from_filters(filters)
        else:
            platform_order_nos = _parse_platform_order_nos(payload.get("platform_order_nos"))
            if not platform_order_nos:
                return jsonify({"success": False, "message": "Enter at least one platform order number."}), 400
            order_ids = tags_mod.resolve_order_ids_from_platform_nos(platform_order_nos)

        if not order_ids:
            return jsonify({"success": False, "message": "No matching orders found."}), 400

        result = tags_mod.add_tags_to_orders(order_ids, tags)
        if not result.get("tags"):
            return jsonify({"success": False, "message": "Enter at least one tag."}), 400

        return jsonify({"success": True, **result})
    except Exception as exc:
        current_app.logger.error(f"4Seller order tags save error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


def _cost_map_cell_value(row: dict[str, Any], field: str, text_field: str) -> Any:
    value = row.get(field)
    if value is not None and value != "":
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            pass
    text = row.get(text_field)
    if text:
        return str(text)
    return None


def _cost_map_to_xlsx_bytes(rows: list[dict[str, Any]], *, include_system_fields: bool = True) -> bytes:
    export_rows: list[dict[str, Any]] = []
    for row in rows:
        out: dict[str, Any] = {"Style": row.get("style") or row.get("sku") or ""}
        if include_system_fields:
            out["System Cost"] = _cost_map_cell_value(row, "system_cost", "system_costs_text")
            out["MSRP"] = _cost_map_cell_value(row, "msrp", "msrps_text")
        out["Xingxing Cost"] = _cost_map_cell_value(row, "xingxing_cost", "xingxing_costs_text")
        export_rows.append(out)

    output = BytesIO()
    df = pd.DataFrame(export_rows)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Map Cost")
    output.seek(0)
    return output.getvalue()


@four_seller_sales_bp.route("/4seller/sales/cost-map", methods=["GET", "POST"])
@login_required
def four_seller_sales_cost_map():
    overrides_mod = importlib.import_module("py.other.4seller_bag_cost_overrides")
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            use_filter = bool(payload.get("use_filter"))
            filters = _normalize_filters(payload.get("filters") or {}) if use_filter else None
        else:
            use_filter = request.args.get("use_filter", "").lower() in ("1", "true", "yes")
            filters = _normalize_filters(request.args.to_dict()) if use_filter else None

        rows = overrides_mod.build_cost_map(filters)
        return jsonify({"success": True, "rows": rows, "count": len(rows)})
    except Exception as exc:
        current_app.logger.error(f"4Seller cost map error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/cost-map/export", methods=["POST"])
@login_required
def four_seller_sales_cost_map_export():
    overrides_mod = importlib.import_module("py.other.4seller_bag_cost_overrides")
    payload = request.get_json(silent=True) or {}
    scope = str(payload.get("scope") or "filter").strip().lower()

    try:
        if scope == "all":
            rows = overrides_mod.build_cost_map_all_available()
            include_system_fields = False
            filename_prefix = "4seller_map_cost_all"
        else:
            use_filter = bool(payload.get("use_filter", True))
            filters = _normalize_filters(payload.get("filters") or {}) if use_filter else None
            rows = overrides_mod.build_cost_map(filters)
            include_system_fields = True
            filename_prefix = "4seller_map_cost"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        xlsx_bytes = _cost_map_to_xlsx_bytes(rows, include_system_fields=include_system_fields)
        output = BytesIO(xlsx_bytes)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{filename_prefix}_{timestamp}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller cost map export error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/cost-map/all", methods=["POST"])
@login_required
def four_seller_sales_cost_map_all():
    overrides_mod = importlib.import_module("py.other.4seller_bag_cost_overrides")
    try:
        rows = overrides_mod.build_cost_map_all_available()
        return jsonify({"success": True, "rows": rows, "count": len(rows)})
    except Exception as exc:
        current_app.logger.error(f"4Seller cost map all error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/cost-map/bulk", methods=["POST"])
@login_required
def four_seller_sales_cost_map_bulk():
    overrides_mod = importlib.import_module("py.other.4seller_bag_cost_overrides")
    payload = request.get_json(silent=True) or {}
    lines = payload.get("lines")
    if lines is None:
        lines = payload.get("text", "")
    if isinstance(lines, list):
        lines = "\n".join(str(line) for line in lines)

    try:
        entries = overrides_mod.parse_bulk_cost_lines(str(lines or ""))
        if not entries:
            return jsonify({"success": False, "message": "No valid style/cost lines found."}), 400
        result = overrides_mod.bulk_save_style_costs(entries)
        return jsonify({"success": True, **result})
    except Exception as exc:
        current_app.logger.error(f"4Seller cost map bulk save error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/bag-cost-override", methods=["POST", "DELETE"])
@login_required
def four_seller_sales_bag_cost_override():
    overrides_mod = importlib.import_module("py.other.4seller_bag_cost_overrides")
    payload = request.get_json(silent=True) or {}
    sku = str(payload.get("sku") or "").strip()

    try:
        if request.method == "DELETE":
            if not sku:
                return jsonify({"success": False, "message": "SKU is required."}), 400
            if str(payload.get("scope") or "").strip().lower() == "style":
                variant_skus = payload.get("variant_skus") or []
                if isinstance(variant_skus, str):
                    variant_skus = [part.strip() for part in variant_skus.split(",") if part.strip()]
                removed = overrides_mod.delete_style_bag_cost_override(sku, variant_skus)
            else:
                removed = overrides_mod.delete_sku_bag_cost_override(sku)
            return jsonify({"success": True, "removed": removed})

        if not sku:
            return jsonify({"success": False, "message": "SKU is required."}), 400
        if payload.get("bag_cost") is None and payload.get("line_cost") is not None:
            qty = int(payload.get("qty") or 1) or 1
            line_cost = float(payload.get("line_cost"))
            bag_cost = round(line_cost / qty, 4)
        else:
            bag_cost = payload.get("bag_cost")
        if str(payload.get("scope") or "").strip().lower() == "style":
            variant_skus = payload.get("variant_skus") or []
            if isinstance(variant_skus, str):
                variant_skus = [part.strip() for part in variant_skus.split(",") if part.strip()]
            result = overrides_mod.save_style_bag_cost_override_for_user(sku, bag_cost, variant_skus)
        else:
            result = overrides_mod.save_sku_bag_cost_override_for_user(sku, bag_cost)
        return jsonify({"success": True, **result})
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        current_app.logger.error(f"4Seller bag cost override error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_sales_bp.route("/4seller/sales/orders/<int:order_id>/finance", methods=["GET"])
@login_required
def four_seller_sales_order_finance(order_id: int):
    import importlib

    finance = importlib.import_module("py.other.4seller_finance")
    try:
        payload = finance.get_order_finance_detail(order_id)
        return jsonify({"success": True, **payload})
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    except Exception as exc:
        current_app.logger.error(f"4Seller order finance detail error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500
