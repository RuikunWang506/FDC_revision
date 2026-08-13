from __future__ import annotations

import importlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

import pandas as pd
import pyodbc

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config

TIKTOK_ORDER_DETAILS_SHEET = "Order details"
TIKTOK_ORDER_ID_COLUMN = "Order/adjustment ID"
TIKTOK_STATEMENT_DATE_COLUMN = "Statement date"

# (Excel column header, value kind: decimal | text)
TIKTOK_FINANCE_FIELDS: list[tuple[str, str]] = [
    ("Total settlement amount", "decimal"),
    ("Net sales", "decimal"),
    ("Gross sales", "decimal"),
    ("Gross sales refund", "decimal"),
    ("Seller discount", "decimal"),
    ("Shipping", "decimal"),
    ("TikTok Shop shipping fee", "decimal"),
    ("Customer-paid shipping fee", "decimal"),
    ("Customer-paid shipping fee refund", "decimal"),
    ("Return shipping fee", "decimal"),
    ("Return shipping fee reimbursement", "decimal"),
    ("Fees", "decimal"),
    ("Referral fee", "decimal"),
    ("Refund administration fee", "decimal"),
    ("Affiliate Commission", "decimal"),
    ("Affiliate partner commission", "decimal"),
    ("Affiliate Shop Ads commission", "decimal"),
    ("TikTok Shop Partner commission", "decimal"),
    ("Co-funded promotion (seller-funded)", "decimal"),
    ("Smart Promotion fee tax", "decimal"),
    ("Co-funded creator bonus", "decimal"),
    ("Affiliate commission deposit", "decimal"),
    ("Affiliate commission refund", "decimal"),
    ("Affiliate Partner shop ads commission", "decimal"),
    ("Campaign service fee", "decimal"),
    ("Campaign resource fee", "decimal"),
    ("Sales tax on referral fees", "decimal"),
    ("Smart Promotion fee", "decimal"),
    ("Managed service plan (Sales tax)", "decimal"),
    ("Managed service plan (Per order fee)", "decimal"),
    ("Co-funded Promotion campaign period fee", "decimal"),
    ("Smart Promotion campaign period fee", "decimal"),
    ("Co-funded Promotion campaign period fee sales tax", "decimal"),
    ("Smart Promotion campaign period fee sales tax", "decimal"),
    ("GMV Max coupon", "decimal"),
    ("GMV Max coupon sales tax", "decimal"),
    ("GMV Max ad fee", "decimal"),
    ("Category-led campaign fee", "decimal"),
    ("Category-led campaign fee tax", "decimal"),
    ("Brand Amplification Program commission fee", "decimal"),
    ("Brand Amplification Program fee tax", "decimal"),
    ("Brand campaign fee", "decimal"),
    ("Brand campaign fee tax", "decimal"),
    ("Adjustment amount", "decimal"),
    ("Adjustment reason", "text"),
    ("Related order ID", "text"),
    ("Customer payment", "decimal"),
    ("Customer refund", "decimal"),
    ("Seller co-funded voucher discount", "decimal"),
    ("Seller co-funded voucher discount refund", "decimal"),
    ("Platform discounts", "decimal"),
    ("Platform discounts refund", "decimal"),
    ("Platform co-funded voucher discounts", "decimal"),
    ("Platform co-funded voucher discounts refund", "decimal"),
    ("Ads referral fee discount", "decimal"),
    ("Sales tax payment", "decimal"),
    ("Sales tax refund", "decimal"),
    ("Retail delivery fee payment", "decimal"),
    ("Retail delivery fee refund", "decimal"),
    ("Customer-paid shipping fee before discounts", "decimal"),
    ("Seller shipping fee discount", "decimal"),
]

FINANCE_PLATFORM_PARSERS = {
    "tiktok": "tiktok",
}


def platform_prefix(platform: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(platform or "").strip().lower())
    return re.sub(r"_+", "_", slug).strip("_")


def finance_db_column(prefix: str, excel_header: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "_", excel_header.strip().lower())
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    return f"{prefix}_{suffix}"[: 64]


def _normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _build_header_lookup(columns: list[Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for column in columns:
        lookup[_normalize_header(column)] = str(column)
    return lookup


def _parse_decimal(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-"}:
        return None
    text = text.replace(",", "")
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _parse_text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _parse_statement_date(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime()
    except Exception:
        return None


def _tiktok_field_specs(prefix: str) -> list[tuple[str, str, str]]:
    return [
        (excel_header, kind, finance_db_column(prefix, excel_header))
        for excel_header, kind in TIKTOK_FINANCE_FIELDS
    ]


def ensure_finance_columns(platform: str) -> list[str]:
    prefix = platform_prefix(platform)
    specs = _tiktok_field_specs(prefix)
    import importlib

    sales = importlib.import_module("py.other.4seller_sales")
    sales._ensure_4seller_orders_table()
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = '4seller_orders'
            """
        )
        existing = {str(row[0]) for row in cursor.fetchall()}
        added: list[str] = []
        for _excel_header, kind, db_column in specs:
            if db_column in existing:
                continue
            sql_type = "DECIMAL(14, 4) NULL" if kind == "decimal" else "VARCHAR(500) NULL"
            cursor.execute(f"ALTER TABLE `4seller_orders` ADD COLUMN `{db_column}` {sql_type}")
            added.append(db_column)
            existing.add(db_column)
        if added:
            connection.commit()
            sales.invalidate_table_columns_cache()
        return added
    finally:
        cursor.close()
        connection.close()


def _statement_date_column(df: pd.DataFrame) -> str | None:
    for column in df.columns:
        if _normalize_header(column) == _normalize_header(TIKTOK_STATEMENT_DATE_COLUMN):
            return column
    return None


def _decimal_db_columns(prefix: str) -> set[str]:
    return {
        finance_db_column(prefix, excel_header)
        for excel_header, kind in TIKTOK_FINANCE_FIELDS
        if kind == "decimal"
    }


def _group_rows_by_order_id(df: pd.DataFrame) -> dict[str, list[pd.Series]]:
    grouped: dict[str, list[pd.Series]] = {}
    if TIKTOK_ORDER_ID_COLUMN not in df.columns:
        return grouped

    for _, row in df.iterrows():
        order_id = str(row.get(TIKTOK_ORDER_ID_COLUMN) or "").strip()
        if not order_id or order_id.lower() == "nan":
            continue
        grouped.setdefault(order_id, []).append(row)
    return grouped


def _latest_row_by_statement(rows: list[pd.Series], statement_col: str | None) -> pd.Series:
    if not rows:
        raise ValueError("rows required")
    if statement_col is None:
        return rows[-1]

    best = rows[0]
    best_dt = _parse_statement_date(best.get(statement_col))
    for row in rows[1:]:
        row_dt = _parse_statement_date(row.get(statement_col))
        if row_dt and (best_dt is None or row_dt >= best_dt):
            best = row
            best_dt = row_dt
    return best


def _decimal_headers_from_lookup(header_lookup: dict[str, str]) -> list[str]:
    headers: list[str] = []
    for excel_header, kind in TIKTOK_FINANCE_FIELDS:
        if kind != "decimal":
            continue
        actual_header = header_lookup.get(_normalize_header(excel_header))
        if actual_header:
            headers.append(actual_header)
    return headers


def _finance_row_fingerprint(row: pd.Series, decimal_headers: list[str]) -> tuple[float, ...]:
    return tuple(round(_parse_decimal(row.get(header)) or 0.0, 4) for header in decimal_headers)


def _dedupe_finance_rows(rows: list[pd.Series], decimal_headers: list[str]) -> list[pd.Series]:
    if len(rows) <= 1:
        return rows
    seen: set[tuple[float, ...]] = set()
    unique: list[pd.Series] = []
    for row in rows:
        fingerprint = _finance_row_fingerprint(row, decimal_headers)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(row)
    return unique


def _values_are_similar(values: list[float], *, tolerance: float = 0.05) -> bool:
    if len(values) < 2:
        return False
    minimum = min(values)
    maximum = max(values)
    if minimum <= 0:
        return False
    return maximum / minimum <= (1.0 + tolerance)


def _summed_values_look_multiplied(values: list[float]) -> bool:
    positives = [float(value) for value in values if value is not None and float(value) > 0]
    if len(positives) < 2:
        return False
    total = sum(positives)
    maximum = max(positives)
    if maximum <= 0:
        return False
    ratio = total / maximum
    nearest = round(ratio)
    return nearest >= 2 and abs(ratio - nearest) <= 0.06


def _collapse_similar_positive_settlement_rows(
    rows: list[pd.Series],
    *,
    statement_col: str | None,
    header_lookup: dict[str, str],
) -> list[pd.Series]:
    if len(rows) <= 1:
        return rows

    gross_header = header_lookup.get(_normalize_header("Gross sales"))
    settlement_header = header_lookup.get(_normalize_header("Total settlement amount"))
    customer_payment_header = header_lookup.get(_normalize_header("Customer payment"))

    positive_sale_rows: list[pd.Series] = []
    for row in rows:
        gross_sales = _parse_decimal(row.get(gross_header)) if gross_header else None
        settlement = _parse_decimal(row.get(settlement_header)) if settlement_header else None
        if gross_sales is not None and gross_sales > 0:
            positive_sale_rows.append(row)
        elif gross_sales is None and settlement is not None and settlement > 0:
            positive_sale_rows.append(row)

    if len(positive_sale_rows) <= 1:
        return rows

    gross_values = [
        float(value)
        for row in positive_sale_rows
        if gross_header and (value := _parse_decimal(row.get(gross_header))) is not None and value > 0
    ]
    settlement_values = [
        float(value)
        for row in positive_sale_rows
        if settlement_header and (value := _parse_decimal(row.get(settlement_header))) is not None and value > 0
    ]
    customer_payment_values = [
        float(value)
        for row in positive_sale_rows
        if customer_payment_header
        and (value := _parse_decimal(row.get(customer_payment_header))) is not None
        and value > 0
    ]

    should_collapse = (
        _values_are_similar(gross_values)
        or _values_are_similar(settlement_values)
        or _values_are_similar(customer_payment_values)
        or _summed_values_look_multiplied(settlement_values)
        or _summed_values_look_multiplied(customer_payment_values)
    )
    if not should_collapse:
        return rows

    other_rows = [row for row in rows if row not in positive_sale_rows]
    latest_sale = _latest_row_by_statement(positive_sale_rows, statement_col)
    return other_rows + [latest_sale]


def _prepare_finance_rows_for_order(
    rows: list[pd.Series],
    *,
    statement_col: str | None,
    header_lookup: dict[str, str],
) -> list[pd.Series]:
    if len(rows) <= 1:
        return rows

    decimal_headers = _decimal_headers_from_lookup(header_lookup)
    rows = _dedupe_finance_rows(rows, decimal_headers)
    if len(rows) <= 1:
        return rows

    if statement_col:
        from collections import defaultdict

        grouped_by_date: dict[str, list[pd.Series]] = defaultdict(list)
        for row in rows:
            statement_dt = _parse_statement_date(row.get(statement_col))
            date_key = statement_dt.date().isoformat() if statement_dt else "__unknown__"
            grouped_by_date[date_key].append(row)
        prepared: list[pd.Series] = []
        for date_rows in grouped_by_date.values():
            prepared.extend(
                _collapse_similar_positive_settlement_rows(
                    date_rows,
                    statement_col=statement_col,
                    header_lookup=header_lookup,
                )
            )
        rows = prepared
    else:
        rows = _collapse_similar_positive_settlement_rows(
            rows,
            statement_col=statement_col,
            header_lookup=header_lookup,
        )

    return rows


def _merge_finance_values(
    target: dict[str, Any],
    incoming: dict[str, Any],
    decimal_keys: set[str],
    *,
    overwrite_text: bool,
) -> None:
    for key, value in incoming.items():
        if key in decimal_keys:
            amount = _parse_decimal(value)
            if amount is None:
                continue
            existing = _parse_decimal(target.get(key))
            target[key] = round((existing or 0.0) + amount, 4)
            continue
        if not overwrite_text and key in target and target.get(key) not in (None, ""):
            continue
        if value is None or value == "":
            continue
        target[key] = value


def _rollup_related_order_records(
    records: dict[str, dict[str, Any]],
    *,
    platform: str,
) -> None:
    prefix = platform_prefix(platform)
    decimal_keys = _decimal_db_columns(prefix)
    related_key = f"{prefix}_related_order_id"

    for order_id, values in list(records.items()):
        parent_id = str(values.get(related_key) or "").strip()
        if not parent_id or parent_id == order_id:
            continue
        if parent_id not in records:
            records[parent_id] = {}
        _merge_finance_values(
            records[parent_id],
            values,
            decimal_keys,
            overwrite_text=False,
        )


def parse_tiktok_finance_file(file_bytes: bytes, *, platform: str) -> dict[str, dict[str, Any]]:
    prefix = platform_prefix(platform)
    specs = _tiktok_field_specs(prefix)
    workbook = pd.ExcelFile(BytesIO(file_bytes))
    sheet_name = None
    for name in workbook.sheet_names:
        if _normalize_header(name) == _normalize_header(TIKTOK_ORDER_DETAILS_SHEET):
            sheet_name = name
            break
    if not sheet_name:
        raise ValueError("TikTok finance file is missing the Order details sheet.")

    df = pd.read_excel(workbook, sheet_name=sheet_name)
    header_lookup = _build_header_lookup(list(df.columns))
    statement_col = _statement_date_column(df)
    grouped_rows = _group_rows_by_order_id(df)
    parsed: dict[str, dict[str, Any]] = {}

    for order_id, rows in grouped_rows.items():
        rows = _prepare_finance_rows_for_order(
            rows,
            statement_col=statement_col,
            header_lookup=header_lookup,
        )
        latest_row = _latest_row_by_statement(rows, statement_col)
        record: dict[str, Any] = {}
        for excel_header, kind, db_column in specs:
            actual_header = header_lookup.get(_normalize_header(excel_header))
            if not actual_header:
                continue
            if kind == "decimal":
                total: float | None = None
                for row in rows:
                    value = _parse_decimal(row.get(actual_header))
                    if value is None:
                        continue
                    total = (total or 0.0) + value
                if total is not None:
                    record[db_column] = round(total, 4)
            else:
                value = _parse_text(latest_row.get(actual_header))
                if value is not None:
                    record[db_column] = value
        if record:
            parsed[order_id] = record

    _rollup_related_order_records(parsed, platform=platform)
    return parsed


def _finance_record_fingerprint(record: dict[str, Any], decimal_keys: set[str]) -> tuple[float, ...]:
    values: list[float] = []
    for key in sorted(decimal_keys):
        values.append(round(_parse_decimal(record.get(key)) or 0.0, 4))
    return tuple(values)


def _finance_records_look_like_duplicates(
    left: dict[str, Any],
    right: dict[str, Any],
    decimal_keys: set[str],
) -> bool:
    if not left or not right:
        return False
    return _finance_record_fingerprint(left, decimal_keys) == _finance_record_fingerprint(
        right, decimal_keys
    )


def _should_additively_merge_finance(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    prefix: str,
) -> bool:
    settlement_key = f"{prefix}_total_settlement_amount"
    existing_settlement = _parse_decimal(existing.get(settlement_key)) or 0.0
    incoming_settlement = _parse_decimal(incoming.get(settlement_key)) or 0.0
    if existing_settlement == incoming_settlement:
        return False
    if existing_settlement * incoming_settlement <= 0:
        return True
    if abs(existing_settlement) <= 0:
        return True
    if abs(incoming_settlement) < abs(existing_settlement) * 0.75:
        return True
    return False


def _has_return_finance_data(record: dict[str, Any], *, prefix: str) -> bool:
    for field in (
        f"{prefix}_gross_sales_refund",
        f"{prefix}_customer_refund",
        f"{prefix}_platform_discounts_refund",
        f"{prefix}_platform_co_funded_voucher_discounts_refund",
        f"{prefix}_seller_co_funded_voucher_discount_refund",
    ):
        value = _parse_decimal(record.get(field))
        if value is not None and value != 0:
            return True
    return False


def _reimbursement_amount(record: dict[str, Any], *, prefix: str) -> float:
    value = _parse_decimal(record.get(f"{prefix}_return_shipping_fee_reimbursement"))
    return float(value or 0.0)


def _combine_finance_for_apply(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    prefix: str,
) -> dict[str, Any]:
    decimal_keys = _decimal_db_columns(prefix)
    if not existing:
        return dict(incoming)
    if not incoming:
        return dict(existing)
    if _finance_records_look_like_duplicates(existing, incoming, decimal_keys):
        return dict(incoming)

    settlement_key = f"{prefix}_total_settlement_amount"
    existing_settlement = _parse_decimal(existing.get(settlement_key)) or 0.0
    incoming_settlement = _parse_decimal(incoming.get(settlement_key)) or 0.0

    if _has_return_finance_data(incoming, prefix=prefix) and not _has_return_finance_data(
        existing, prefix=prefix
    ):
        return dict(incoming)

    if abs(incoming_settlement) > abs(existing_settlement) + 0.01 and (
        _has_return_finance_data(incoming, prefix=prefix)
        or incoming_settlement * existing_settlement <= 0
    ):
        return dict(incoming)

    if _should_additively_merge_finance(existing, incoming, prefix=prefix):
        incoming_reimb = _reimbursement_amount(incoming, prefix=prefix)
        existing_reimb = _reimbursement_amount(existing, prefix=prefix)
        if incoming_reimb > 0 and existing_reimb > 0 and abs(incoming_reimb - existing_reimb) < 0.01:
            return dict(existing)
        combined = dict(existing)
        _merge_finance_values(combined, incoming, decimal_keys, overwrite_text=False)
        return combined

    return dict(incoming)


def _combine_finance_records(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    prefix: str,
) -> dict[str, Any]:
    decimal_keys = _decimal_db_columns(prefix)
    if not existing:
        return dict(incoming)
    if not incoming:
        return dict(existing)
    if _finance_records_look_like_duplicates(existing, incoming, decimal_keys):
        return dict(incoming)
    if _should_additively_merge_finance(existing, incoming, prefix=prefix):
        combined = dict(existing)
        _merge_finance_values(combined, incoming, decimal_keys, overwrite_text=False)
        return combined
    return dict(incoming)


def merge_finance_records(
    target: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
    *,
    platform: str,
) -> None:
    prefix = platform_prefix(platform)
    decimal_keys = _decimal_db_columns(prefix)
    for order_id, values in incoming.items():
        if not values:
            continue
        if order_id not in target:
            target[order_id] = dict(values)
            continue
        target[order_id] = _combine_finance_records(
            target[order_id],
            values,
            prefix=prefix,
        )


def _fetch_existing_finance_records(
    platform_order_nos: list[str],
    *,
    platform: str,
) -> dict[str, dict[str, Any]]:
    if not platform_order_nos:
        return {}

    prefix = platform_prefix(platform)
    decimal_keys = sorted(_decimal_db_columns(prefix))
    text_keys = sorted(
        finance_db_column(prefix, excel_header)
        for excel_header, kind in TIKTOK_FINANCE_FIELDS
        if kind == "text"
    )
    columns = [*decimal_keys, *text_keys]
    if not columns:
        return {}

    platform_value = str(platform or "").strip().upper()
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    records: dict[str, dict[str, Any]] = {}
    cursor = connection.cursor()
    try:
        chunk_size = 200
        for start in range(0, len(platform_order_nos), chunk_size):
            chunk = platform_order_nos[start : start + chunk_size]
            placeholders = ", ".join(["%s"] * len(chunk))
            select_cols = ", ".join(f"MAX(`{column}`) AS `{column}`" for column in columns)
            cursor.execute(
                f"""
                SELECT platform_order_no, {select_cols}
                FROM `4seller_orders`
                WHERE platform_order_no IN ({placeholders})
                  AND UPPER(platform) = %s
                GROUP BY platform_order_no
                """,
                [*chunk, platform_value],
            )
            for row in cursor.fetchall():
                order_no = str(row[0] or "").strip()
                if not order_no:
                    continue
                values: dict[str, Any] = {}
                for idx, column in enumerate(columns, start=1):
                    value = row[idx]
                    if value is None or value == "":
                        continue
                    if column in decimal_keys:
                        parsed = _parse_decimal(value)
                        if parsed is None:
                            continue
                        values[column] = round(parsed, 4)
                    else:
                        values[column] = str(value).strip()
                if values:
                    records[order_no] = values
    finally:
        cursor.close()
        connection.close()
    return records


def apply_finance_records(platform: str, records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "orders_parsed": 0,
            "orders_matched": 0,
            "rows_updated": 0,
            "orders_not_found": [],
            "columns_added": [],
        }

    platform_value = str(platform or "").strip().upper()
    columns_added = ensure_finance_columns(platform_value)

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    orders_matched = 0
    rows_updated = 0
    not_found: list[str] = []

    try:
        existing_records = _fetch_existing_finance_records(list(records.keys()), platform=platform_value)
        for platform_order_no, values in records.items():
            if not values:
                continue
            merged_values = _combine_finance_for_apply(
                existing_records.get(platform_order_no, {}),
                values,
                prefix=platform_prefix(platform_value),
            )
            cursor.execute(
                """
                SELECT COUNT(*) FROM `4seller_orders`
                WHERE platform_order_no = %s AND UPPER(platform) = %s
                """,
                (platform_order_no, platform_value),
            )
            match_count = int(cursor.fetchone()[0] or 0)
            if match_count <= 0:
                not_found.append(platform_order_no)
                continue

            orders_matched += 1
            set_clause = ", ".join(f"`{column}` = %s" for column in merged_values)
            params = [*merged_values.values(), platform_order_no, platform_value]
            cursor.execute(
                f"""
                UPDATE `4seller_orders`
                SET {set_clause}
                WHERE platform_order_no = %s AND UPPER(platform) = %s
                """,
                params,
            )
            rows_updated += cursor.rowcount

        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return {
        "orders_parsed": len(records),
        "orders_matched": orders_matched,
        "rows_updated": rows_updated,
        "orders_not_found": not_found[:50],
        "orders_not_found_count": len(not_found),
        "columns_added": columns_added,
    }


def process_finance_upload(platform: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    parser_key = platform_prefix(platform)
    if parser_key not in FINANCE_PLATFORM_PARSERS:
        raise ValueError(f"Finance upload is not supported for platform '{platform}'.")

    merged: dict[str, dict[str, Any]] = {}
    file_summaries: list[dict[str, Any]] = []
    for filename, file_bytes in files:
        if not file_bytes:
            continue
        if parser_key == "tiktok":
            parsed = parse_tiktok_finance_file(file_bytes, platform=platform)
        else:
            raise ValueError(f"Finance upload is not supported for platform '{platform}'.")
        merge_finance_records(merged, parsed, platform=platform)
        file_summaries.append({"filename": filename, "orders_parsed": len(parsed)})

    result = apply_finance_records(platform, merged)
    result["files_processed"] = len(file_summaries)
    result["files"] = file_summaries
    return result


def finance_field_label_map(prefix: str) -> dict[str, str]:
    return {
        finance_db_column(prefix, excel_header): excel_header
        for excel_header, _kind in TIKTOK_FINANCE_FIELDS
    }


TIKTOK_PAYOUT_COMPONENT_KEYS = [
    "tiktok_net_sales",
    "tiktok_shipping",
    "tiktok_fees",
    "tiktok_adjustment_amount",
]

TIKTOK_NET_SALES_KEYS = [
    "tiktok_gross_sales",
    "tiktok_gross_sales_refund",
    "tiktok_seller_discount",
]

TIKTOK_SHIPPING_DETAIL_KEYS = [
    finance_db_column("tiktok", header)
    for header in (
        "TikTok Shop shipping fee",
        "Customer-paid shipping fee",
        "Customer-paid shipping fee refund",
        "Return shipping fee",
        "Return shipping fee reimbursement",
        "Customer-paid shipping fee before discounts",
        "Seller shipping fee discount",
    )
]

TIKTOK_FEE_DETAIL_KEYS = [
    finance_db_column("tiktok", header)
    for header, kind in TIKTOK_FINANCE_FIELDS
    if kind == "decimal"
    and header
    not in {
        "Total settlement amount",
        "Net sales",
        "Gross sales",
        "Gross sales refund",
        "Seller discount",
        "Shipping",
        "Fees",
        "Customer payment",
        "Customer refund",
        "Adjustment amount",
    }
    and not any(
        token in header.lower()
        for token in (
            "shipping fee",
            "shipping fee refund",
            "return shipping",
            "shipping fee before",
            "shipping fee discount",
        )
    )
]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _finance_line(key: str, label: str, value: Any) -> dict[str, Any] | None:
    amount = _to_float(value)
    if amount is None:
        return None
    return {"key": key, "label": label, "amount": round(amount, 4)}


def _build_tiktok_finance_detail(row: dict[str, Any]) -> dict[str, Any]:
    labels = finance_field_label_map("tiktok")
    currency = str(row.get("currency") or "USD")

    def line_for(key: str) -> dict[str, Any] | None:
        return _finance_line(key, labels.get(key, key), row.get(key))

    payout_lines = [line_for(key) for key in TIKTOK_PAYOUT_COMPONENT_KEYS]
    payout_lines = [line for line in payout_lines if line is not None]

    net_sales_lines = [line_for(key) for key in TIKTOK_NET_SALES_KEYS]
    net_sales_lines = [line for line in net_sales_lines if line is not None]

    shipping_lines = [line_for(key) for key in TIKTOK_SHIPPING_DETAIL_KEYS]
    shipping_lines = [line for line in shipping_lines if line and line["amount"] != 0]

    fee_lines = [line_for(key) for key in TIKTOK_FEE_DETAIL_KEYS]
    fee_lines = [line for line in fee_lines if line and line["amount"] != 0]

    finance_fields: list[dict[str, Any]] = []
    for key, label in labels.items():
        value = row.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, Decimal) or isinstance(value, (int, float)):
            amount = _to_float(value)
            if amount is None:
                continue
            finance_fields.append({"key": key, "label": label, "amount": round(amount, 4)})
        else:
            finance_fields.append({"key": key, "label": label, "value": str(value)})

    total_settlement = _to_float(row.get("tiktok_total_settlement_amount"))
    net_sales = _to_float(row.get("tiktok_net_sales"))
    shipping = _to_float(row.get("tiktok_shipping"))
    fees = _to_float(row.get("tiktok_fees"))
    adjustment = _to_float(row.get("tiktok_adjustment_amount")) or 0.0
    calculated_total = sum(
        value for value in (net_sales, shipping, fees, adjustment) if value is not None
    )
    matches = (
        total_settlement is not None
        and abs(calculated_total - total_settlement) < 0.02
    )

    has_finance = total_settlement is not None or bool(finance_fields)

    return {
        "has_finance": has_finance,
        "currency": currency,
        "summary": {
            "final_payout": total_settlement,
            "order_amount": _to_float(row.get("amount")),
            "customer_payment": _to_float(row.get("tiktok_customer_payment")),
            "shipping_label_price": _to_float(row.get("shipping_label_price")),
            "shipping_fee": _to_float(row.get("shipping_fee")),
            "net_sales": net_sales,
            "shipping_total": shipping,
            "fees_total": fees,
            "adjustment_total": adjustment if adjustment else None,
        },
        "payout_calculation": {
            "lines": payout_lines,
            "total": total_settlement,
            "calculated_total": round(calculated_total, 4) if total_settlement is not None else None,
            "matches": matches,
        },
        "net_sales_breakdown": {
            "lines": net_sales_lines,
            "subtotal": net_sales,
        },
        "shipping_breakdown": {
            "lines": shipping_lines,
            "subtotal": shipping,
        },
        "fee_breakdown": {
            "lines": fee_lines,
            "subtotal": fees,
        },
        "finance_fields": finance_fields,
        "adjustment_reason": row.get("tiktok_adjustment_reason"),
        "related_order_id": row.get("tiktok_related_order_id"),
    }


NOTE_DETAIL_SEPARATOR = "----------------------"

# Chinese labels for finance Excel cell notes (section titles + TikTok breakdown lines)
FINANCE_NOTE_LABEL_ZH: dict[str, str] = {
    "Net sales": "销售额",
    "Shipping": "运费",
    "Fees": "费用",
    "Adjustment": "平台审计",
    "Adjustment reason": "调整原因",
    "Related order ID": "相关订单号",
    "Final payout": "最终收入",
    "Bag cost": "包成本",
    "Returned order: bag cost = 0": "退货订单：包成本 = 0",
    "Other fee": "其他费用",
    "Shipping label price": "面单费用",
    "Xingxing payout": "星星收入",
    "Final payout - Bag cost": "最终收入 - 包成本",
    "Total settlement amount": "总结算金额",
    "Gross sales": "毛销售额",
    "Gross sales refund": "毛销售额退款",
    "Seller discount": "卖家折扣",
    "TikTok Shop shipping fee": "TikTok Shop 运费",
    "Customer-paid shipping fee": "顾客支付运费",
    "Customer-paid shipping fee refund": "顾客支付运费退款",
    "Return shipping fee": "退货运费",
    "Return shipping fee reimbursement": "退货运费报销",
    "Referral fee": "佣金",
    "Refund administration fee": "退款手续费",
    "Affiliate Commission": "联盟佣金",
    "Affiliate partner commission": "联盟伙伴佣金",
    "Affiliate Shop Ads commission": "联盟 Shop Ads 佣金",
    "TikTok Shop Partner commission": "TikTok Shop 合作方佣金",
    "Co-funded promotion (seller-funded)": "联合促销（卖家出资）",
    "Smart Promotion fee tax": "智能促销费用税",
    "Co-funded creator bonus": "联合创作者奖励",
    "Affiliate commission deposit": "联盟佣金保证金",
    "Affiliate commission refund": "联盟佣金退款",
    "Affiliate Partner shop ads commission": "联盟伙伴商店广告佣金",
    "Campaign service fee": "活动服务费",
    "Campaign resource fee": "活动资源费",
    "Sales tax on referral fees": "佣金销售税",
    "Smart Promotion fee": "智能促销费",
    "Managed service plan (Sales tax)": "托管服务计划（销售税）",
    "Managed service plan (Per order fee)": "托管服务计划（按单收费）",
    "Co-funded Promotion campaign period fee": "联合促销活动期费用",
    "Smart Promotion campaign period fee": "智能促销活动期费用",
    "Co-funded Promotion campaign period fee sales tax": "联合促销活动期费用销售税",
    "Smart Promotion campaign period fee sales tax": "智能促销活动期费用销售税",
    "GMV Max coupon": "GMV Max 优惠券",
    "GMV Max coupon sales tax": "GMV Max 优惠券销售税",
    "GMV Max ad fee": "GMV Max 广告费",
    "Category-led campaign fee": "类目活动费",
    "Category-led campaign fee tax": "类目活动费税",
    "Brand Amplification Program commission fee": "品牌放大计划佣金",
    "Brand Amplification Program fee tax": "品牌放大计划费用税",
    "Brand campaign fee": "品牌活动费",
    "Brand campaign fee tax": "品牌活动费税",
    "Adjustment amount": "调整金额",
    "Customer payment": "顾客付款",
    "Customer refund": "顾客退款",
    "Seller co-funded voucher discount": "卖家联合优惠券折扣",
    "Seller co-funded voucher discount refund": "卖家联合优惠券折扣退款",
    "Platform discounts": "平台折扣",
    "Platform discounts refund": "平台折扣退款",
    "Platform co-funded voucher discounts": "平台联合优惠券折扣",
    "Platform co-funded voucher discounts refund": "平台联合优惠券折扣退款",
    "Ads referral fee discount": "广告佣金折扣",
    "Sales tax payment": "销售税支付",
    "Sales tax refund": "销售税退款",
    "Retail delivery fee payment": "零售配送费支付",
    "Retail delivery fee refund": "零售配送费退款",
    "Customer-paid shipping fee before discounts": "折扣前顾客支付运费",
    "Seller shipping fee discount": "卖家运费折扣",
}


def _finance_note_label_zh(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    return FINANCE_NOTE_LABEL_ZH.get(text, text)


def _localize_note_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines or []:
        item = dict(line)
        item["label"] = _finance_note_label_zh(str(item.get("label") or ""))
        out.append(item)
    return out


def _format_note_amount(amount: float | None, currency: str) -> str:
    if amount is None:
        return ""
    value = float(amount)
    prefix = "+" if value > 0 else ""
    return f"{prefix}{currency} {value:.2f}"


def _format_breakdown_note(
    section_label: str,
    total: float | None,
    lines: list[dict[str, Any]],
    currency: str,
    *,
    extra_text_lines: list[tuple[str, str]] | None = None,
) -> str:
    parts: list[str] = [_finance_note_label_zh(section_label)]
    if total is not None:
        parts.append(_format_note_amount(total, currency))

    detail_parts: list[str] = []
    for line in _localize_note_lines(lines):
        amount = _to_float(line.get("amount"))
        if amount is not None and amount != 0:
            detail_parts.append(str(line.get("label") or ""))
            detail_parts.append(_format_note_amount(amount, currency))
    for label, value in extra_text_lines or []:
        text = str(value or "").strip()
        if not text:
            continue
        detail_parts.append(_finance_note_label_zh(label))
        detail_parts.append(text)

    if detail_parts:
        parts.append(NOTE_DETAIL_SEPARATOR)
        parts.extend(detail_parts)
    return "\n".join(part for part in parts if part)


def build_finance_export_cell_notes(order: dict[str, Any]) -> dict[str, str]:
    """Build Excel cell comment text for finance export columns (Chinese)."""
    prefix = platform_prefix(str(order.get("platform") or ""))
    notes: dict[str, str] = {}

    if prefix == "tiktok":
        detail = _build_tiktok_finance_detail(order)
        if not detail.get("has_finance"):
            return notes
        currency = str(detail.get("currency") or order.get("currency") or "USD")

        notes["finance_net_sales"] = _format_breakdown_note(
            "Net sales",
            _to_float(detail.get("summary", {}).get("net_sales")),
            [
                line
                for line in (detail.get("net_sales_breakdown") or {}).get("lines") or []
                if _to_float(line.get("amount")) not in (None, 0)
            ],
            currency,
        )
        notes["finance_shipping"] = _format_breakdown_note(
            "Shipping",
            _to_float(detail.get("summary", {}).get("shipping_total")),
            (detail.get("shipping_breakdown") or {}).get("lines") or [],
            currency,
        )
        notes["finance_fees"] = _format_breakdown_note(
            "Fees",
            _to_float(detail.get("summary", {}).get("fees_total")),
            (detail.get("fee_breakdown") or {}).get("lines") or [],
            currency,
        )
        adjustment_extra: list[tuple[str, str]] = []
        if detail.get("adjustment_reason"):
            adjustment_extra.append(("Adjustment reason", str(detail["adjustment_reason"])))
        if detail.get("related_order_id"):
            adjustment_extra.append(("Related order ID", str(detail["related_order_id"])))
        notes["finance_adjustment"] = _format_breakdown_note(
            "Adjustment",
            _to_float(detail.get("summary", {}).get("adjustment_total")),
            [],
            currency,
            extra_text_lines=adjustment_extra,
        )

        payout_parts = [_finance_note_label_zh("Final payout")]
        final_payout = _to_float(detail.get("summary", {}).get("final_payout"))
        if final_payout is not None:
            payout_parts.append(_format_note_amount(final_payout, currency))
        payout_detail_parts: list[str] = []
        for line in _localize_note_lines((detail.get("payout_calculation") or {}).get("lines") or []):
            payout_detail_parts.append(str(line.get("label") or ""))
            payout_detail_parts.append(_format_note_amount(_to_float(line.get("amount")), currency))
        if payout_detail_parts:
            payout_parts.append(NOTE_DETAIL_SEPARATOR)
            payout_parts.extend(payout_detail_parts)
        notes["finance_final_payout"] = "\n".join(part for part in payout_parts if part)

    bag_lines: list[str] = [_finance_note_label_zh("Bag cost")]
    bag_detail_lines: list[str] = []
    bag_total = 0.0
    found_bag = False
    for line in order.get("sku_lines") or []:
        sku = str(line.get("sku") or "").strip()
        if not sku:
            continue
        qty = int(line.get("item_qty") or 0) or 1
        unit_cost = _to_float(line.get("unit_cost"))
        line_cost = _to_float(line.get("line_cost"))
        if unit_cost is None and line_cost is None:
            continue
        found_bag = True
        if line_cost is not None:
            bag_total += line_cost
        elif unit_cost is not None:
            bag_total += unit_cost * qty
        if unit_cost is not None:
            bag_detail_lines.append(f"{sku} x{qty} @ {unit_cost:.2f}")
        else:
            bag_detail_lines.append(f"{sku} x{qty}")
    if order_is_returned(order):
        bag_detail_lines.append(_finance_note_label_zh("Returned order: bag cost = 0"))
    elif found_bag:
        currency = str(order.get("currency") or "USD")
        bag_lines.insert(1, _format_note_amount(round(bag_total, 2), currency))
    if bag_detail_lines:
        bag_lines.append(NOTE_DETAIL_SEPARATOR)
        bag_lines.extend(bag_detail_lines)
    if len(bag_lines) > 1:
        notes["finance_bag_cost"] = "\n".join(bag_lines)

    label_price = _to_float(order.get("shipping_label_price"))
    if label_price is not None:
        currency = str(order.get("currency") or "USD")
        notes["finance_other_fee"] = "\n".join(
            [
                _finance_note_label_zh("Other fee"),
                _format_note_amount(round(abs(label_price), 2), currency),
                NOTE_DETAIL_SEPARATOR,
                _finance_note_label_zh("Shipping label price"),
                _format_note_amount(label_price, currency),
            ]
        )

    payout = _finance_export_numeric_for_note(order, "finance_final_payout")
    bag = _finance_export_numeric_for_note(order, "finance_bag_cost")
    xingxing = _finance_export_numeric_for_note(order, "finance_xingxing_payout")
    if payout is not None:
        currency = str(order.get("currency") or "USD")
        xingxing_parts = [
            _finance_note_label_zh("Xingxing payout"),
            _format_note_amount(xingxing, currency),
            NOTE_DETAIL_SEPARATOR,
            _finance_note_label_zh("Final payout - Bag cost"),
            f"{_format_note_amount(payout, currency)} - "
            f"{_format_note_amount(bag or 0, currency)}",
        ]
        notes["finance_xingxing_payout"] = "\n".join(xingxing_parts)

    return {key: value for key, value in notes.items() if value and value.strip()}


def _finance_export_numeric_for_note(order: dict[str, Any], field: str) -> float | None:
    if field == "finance_bag_cost":
        if order_is_returned(order):
            return 0.0
        return _to_float(order.get("finance_bag_cost"))
    if field == "finance_xingxing_payout":
        value = _to_float(order.get("finance_xingxing_payout"))
        if value is not None:
            return value
        payout = _to_float(order.get("finance_final_payout"))
        if payout is None:
            return None
        bag = _finance_export_numeric_for_note(order, "finance_bag_cost") or 0.0
        return round(payout - bag, 4)
    return _to_float(order.get(field))


def order_row_has_finance(row: dict[str, Any]) -> bool:
    prefix = platform_prefix(str(row.get("platform") or ""))
    if not prefix:
        return False
    needle = f"{prefix}_"
    for key, value in row.items():
        if not str(key).startswith(needle):
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def attach_finance_summary_fields(row: dict[str, Any]) -> None:
    prefix = platform_prefix(str(row.get("platform") or ""))
    mapping = {
        "finance_net_sales": "net_sales",
        "finance_shipping": "shipping",
        "finance_fees": "fees",
        "finance_adjustment": "adjustment_amount",
        "finance_final_payout": "total_settlement_amount",
    }
    for out_key, suffix in mapping.items():
        if not prefix:
            row[out_key] = None
            continue
        value = row.get(f"{prefix}_{suffix}")
        row[out_key] = _to_float(value)


def _finance_indicates_return(row: dict[str, Any]) -> bool:
    prefix = platform_prefix(str(row.get("platform") or ""))
    if not prefix or not order_row_has_finance(row):
        return False

    refund_fields = (
        f"{prefix}_gross_sales_refund",
        f"{prefix}_customer_refund",
        f"{prefix}_platform_discounts_refund",
        f"{prefix}_platform_co_funded_voucher_discounts_refund",
        f"{prefix}_seller_co_funded_voucher_discount_refund",
        f"{prefix}_sales_tax_refund",
        f"{prefix}_retail_delivery_fee_refund",
        f"{prefix}_return_shipping_fee",
        f"{prefix}_affiliate_commission_refund",
    )
    for field in refund_fields:
        value = _to_float(row.get(field))
        if value is not None and value > 0:
            return True

    reimbursement = _to_float(row.get(f"{prefix}_return_shipping_fee_reimbursement"))
    if reimbursement is not None and reimbursement > 0:
        net_sales = _to_float(row.get(f"{prefix}_net_sales"))
        gross_sales = _to_float(row.get(f"{prefix}_gross_sales"))
        if net_sales == 0 or gross_sales == 0:
            return True

    return False


def order_is_returned(row: dict[str, Any]) -> bool:
    order_status = str(row.get("order_status") or "").lower()
    platform_status = str(row.get("platform_order_status") or "").lower()
    if "return" in order_status or "return" in platform_status:
        return True
    if "refund" in platform_status:
        return True

    payout = _to_float(row.get("finance_final_payout"))
    if payout is not None and payout < 0:
        return True

    prefix = platform_prefix(str(row.get("platform") or ""))
    if prefix:
        raw_payout = _to_float(row.get(f"{prefix}_total_settlement_amount"))
        if raw_payout is not None and raw_payout < 0:
            return True

    if _finance_indicates_return(row):
        return True

    return False


def attach_returned_flags(orders: list[dict[str, Any]]) -> None:
    for order in orders:
        order["is_returned"] = order_is_returned(order)


def _normalize_sku_key(sku: str) -> str:
    return re.sub(r"\s+", " ", str(sku or "").strip()).upper()


def _sku_lookup_prefix(sku: str) -> str:
    parts = _normalize_sku_key(sku).split()
    return parts[0] if parts else _normalize_sku_key(sku)


_INV_AMOUNT_COLUMNS = frozenset({"SALES_COST", "RETAIL_PRS"})


def _fetch_inv_amounts_from_sql_db(
    database_name: str,
    skus: list[str],
    column: str,
) -> dict[str, float]:
    if not skus:
        return {}
    field = str(column or "").strip().upper()
    if field not in _INV_AMOUNT_COLUMNS:
        raise ValueError(f"Unsupported inv amount column: {column}")

    config = Config.SQL_SERVER_CONFIG
    found: dict[str, float] = {}
    remaining = list(dict.fromkeys(_normalize_sku_key(sku) for sku in skus if _normalize_sku_key(sku)))
    if not remaining:
        return found

    try:
        conn = pyodbc.connect(
            f'DRIVER={config["driver"]};'
            f'SERVER={config["server"]};'
            f'DATABASE={database_name};'
            f'UID={config["username"]};'
            f'PWD={config["password"]}'
        )
        cursor = conn.cursor()

        chunk_size = 200
        for start in range(0, len(remaining), chunk_size):
            chunk = remaining[start : start + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            cursor.execute(
                f"""
                SELECT LTRIM(RTRIM(PROD_CD)) AS sku, {field}
                FROM dbo.inv
                WHERE LTRIM(RTRIM(PROD_CD)) IN ({placeholders})
                """,
                chunk,
            )
            for row in cursor.fetchall():
                sku_key = _normalize_sku_key(row[0])
                if sku_key and sku_key not in found:
                    found[sku_key] = round(float(row[1] or 0), 4)

        still_missing = [sku for sku in remaining if sku not in found]
        for sku in still_missing:
            prefix = _sku_lookup_prefix(sku)
            if not prefix:
                continue
            cursor.execute(
                f"""
                SELECT TOP 1 LTRIM(RTRIM(PROD_CD)) AS sku, {field}
                FROM dbo.inv
                WHERE LTRIM(RTRIM(PROD_CD)) LIKE ?
                   OR LTRIM(RTRIM(ALT_CD)) LIKE ?
                ORDER BY
                    CASE WHEN LTRIM(RTRIM(PROD_CD)) = ? THEN 0 ELSE 1 END,
                    LEN(LTRIM(RTRIM(PROD_CD)))
                """,
                (f"{prefix}%", f"{prefix}%", sku),
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                found[sku] = round(float(row[1] or 0), 4)

        conn.close()
    except Exception:
        return found

    return found


def _fetch_costs_from_sql_db(database_name: str, skus: list[str]) -> dict[str, float]:
    return _fetch_inv_amounts_from_sql_db(database_name, skus, "SALES_COST")


def fetch_sku_unit_costs(skus: list[str]) -> dict[str, float]:
    """Return normalized SKU -> unit SALES_COST from FDC then E_FAS dbo.inv."""
    config = Config.SQL_SERVER_CONFIG
    costs = _fetch_costs_from_sql_db(config["database_fdc"], skus)
    missing = [_normalize_sku_key(sku) for sku in skus if _normalize_sku_key(sku) not in costs]
    if missing:
        costs.update(_fetch_costs_from_sql_db(config["database_oms_ef"], missing))
    return costs


def fetch_sku_msrps(skus: list[str]) -> dict[str, float]:
    """Return normalized SKU -> MSRP (RETAIL_PRS) from FDC then E_FAS dbo.inv."""
    config = Config.SQL_SERVER_CONFIG
    msrps = _fetch_inv_amounts_from_sql_db(config["database_fdc"], skus, "RETAIL_PRS")
    missing = [_normalize_sku_key(sku) for sku in skus if _normalize_sku_key(sku) not in msrps]
    if missing:
        msrps.update(_fetch_inv_amounts_from_sql_db(config["database_oms_ef"], missing, "RETAIL_PRS"))
    return msrps


def _resolve_sku_unit_cost(sku: str, cost_map: dict[str, float]) -> float | None:
    sku_key = _normalize_sku_key(sku)
    if not sku_key:
        return None
    return cost_map.get(sku_key)


def attach_bag_cost_fields(orders: list[dict[str, Any]]) -> None:
    skus: list[str] = []
    for order in orders:
        for line in order.get("sku_lines") or []:
            sku = str(line.get("sku") or "").strip()
            if sku:
                skus.append(sku)
    if not skus:
        for order in orders:
            order["finance_bag_cost"] = None
        return

    cost_map = fetch_sku_unit_costs(skus)
    sku_keys = sorted(
        {
            _normalize_sku_key(str(line.get("sku") or "").strip())
            for order in orders
            for line in order.get("sku_lines") or []
            if str(line.get("sku") or "").strip()
        }
    )
    style_keys = sorted({_sku_lookup_prefix(sku) for sku in skus if _sku_lookup_prefix(sku)})
    overrides_mod = importlib.import_module("py.other.4seller_bag_cost_overrides")
    override_map = overrides_mod.fetch_sku_bag_cost_overrides([*sku_keys, *style_keys])

    for order in orders:
        if order_is_returned(order):
            order["finance_bag_cost"] = 0.0
            order["finance_bag_cost_adjusted"] = False
            for line in order.get("sku_lines") or []:
                line["unit_cost"] = 0.0
                line["line_cost"] = 0.0
                line["bag_cost_user_adjusted"] = False
            continue

        total_cost = 0.0
        found_any = False
        for line in order.get("sku_lines") or []:
            sku = str(line.get("sku") or "").strip()
            if not sku:
                continue
            sku_key = _normalize_sku_key(sku)
            style_key = _sku_lookup_prefix(sku)
            if sku_key in override_map:
                unit_cost = override_map[sku_key]
                line["bag_cost_user_adjusted"] = True
            elif style_key in override_map:
                unit_cost = override_map[style_key]
                line["bag_cost_user_adjusted"] = True
            else:
                unit_cost = _resolve_sku_unit_cost(sku, cost_map)
                line["bag_cost_user_adjusted"] = False
            qty = int(line.get("item_qty") or 0) or 1
            if unit_cost is None:
                line["unit_cost"] = None
                line["line_cost"] = None
                continue
            found_any = True
            line_cost = round(unit_cost * qty, 4)
            line["unit_cost"] = unit_cost
            line["line_cost"] = line_cost
            total_cost += line_cost
        order["finance_bag_cost"] = round(total_cost, 4) if found_any else None
        order["finance_bag_cost_adjusted"] = any(
            bool(line.get("bag_cost_user_adjusted"))
            for line in order.get("sku_lines") or []
        )


def attach_xingxing_finance_fields(orders: list[dict[str, Any]]) -> None:
    for order in orders:
        label_price = _to_float(order.get("shipping_label_price"))
        order["finance_other_fee"] = (
            round(abs(label_price), 4) if label_price is not None else None
        )

        payout = _to_float(order.get("finance_final_payout"))
        if order_is_returned(order):
            bag = 0.0
        else:
            bag = _to_float(order.get("finance_bag_cost")) or 0.0

        if payout is not None:
            order["finance_xingxing_payout"] = round(payout - bag, 4)
        else:
            order["finance_xingxing_payout"] = None


def get_order_finance_detail(order_id: int) -> dict[str, Any]:
    import importlib

    sales = importlib.import_module("py.other.4seller_sales")
    sales._ensure_4seller_orders_table()
    columns = sales._get_select_columns()
    select_sql = ", ".join(f"`{column}`" for column in columns)

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {select_sql} FROM `4seller_orders` WHERE order_id = %s LIMIT 1",
            (int(order_id),),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("Order not found.")

        sku_lines: list[dict[str, Any]] = []
        cursor.execute(
            """
            SELECT sku, item_qty, item_price, item_title, order_item_id
            FROM `4seller_orders`
            WHERE order_id = %s
            ORDER BY order_item_id
            """,
            (int(order_id),),
        )
        for line in cursor.fetchall():
            sku_lines.append(
                {
                    "sku": line.get("sku"),
                    "item_qty": int(line.get("item_qty") or 0),
                    "item_price": _to_float(line.get("item_price")),
                    "item_title": line.get("item_title"),
                    "order_item_id": line.get("order_item_id"),
                }
            )
    finally:
        cursor.close()
        connection.close()

    prefix = platform_prefix(str(row.get("platform") or ""))
    finance_detail: dict[str, Any] = {"has_finance": False}
    if prefix == "tiktok":
        finance_detail = _build_tiktok_finance_detail(row)

    return {
        "order": {
            "order_id": row.get("order_id"),
            "platform_order_no": row.get("platform_order_no"),
            "platform": row.get("platform"),
            "order_status": row.get("order_status"),
            "platform_order_status": row.get("platform_order_status"),
            "amount": _to_float(row.get("amount")),
            "currency": row.get("currency"),
            "paid_time": row.get("paid_time"),
            "ship_time": row.get("ship_time"),
            "tracking_no": row.get("tracking_no"),
            "sku_lines": sku_lines,
        },
        "finance": finance_detail,
    }
