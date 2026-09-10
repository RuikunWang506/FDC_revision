import pandas as pd
from flask import Blueprint, render_template, session, redirect, url_for, current_app, jsonify, send_file, request
import pyodbc
import os
import math
import json
import tempfile
from datetime import datetime
from urllib.parse import quote
import logging
import re
import glob
from io import StringIO
import gspread
from google.oauth2.service_account import Credentials
import requests
from datetime import timezone, timedelta
import imaplib
import email
from email.header import decode_header

from py.search.container import (
    _filter_system_file_line_items,
    _trim_blank_export_rows,
    extract_packing_list_case_dimensions,
    lookup_case_dimensions,
)
from py.utilities.color_abbrev import normalize_color_to_abbrev
from py.utilities.boat_sheet_cache import (
    build_boat_data_cache_key,
    invalidate_boat_data_response_cache,
    load_cached_boat_data,
    load_latest_cached_boat_payload,
    load_sheet_dataframe,
    save_cached_boat_data,
)
from py.utilities.shipment_tracking_import import (
    has_saved_tracking_file,
    load_saved_tracking_rows,
    load_shipment_tracking_lookup,
    load_tracking_meta,
    process_shipment_tracking_upload,
    reprocess_saved_shipment_tracking,
    save_tracking_meta,
)
from py.utilities.container_po_import import (
    delete_container_po_from_fdc,
    lookup_container_vendor_id,
    lookup_vendor_info,
    receive_container_po_at_827,
    revert_container_to_ocean,
    transfer_container_po_to_fdc,
    PO_LINE_PUR_CD,
    RECEIVE_ALLOC_PUR_CD,
    VENDOR_FALLBACK,
)
from py.utilities.style_efas_transfer import transfer_container_styles_to_oms, update_retail_prices_dual
from py.utilities.setting import Config, login_required
TERMINAL49_API_URL = "https://api.terminal49.com/v2/tracking_requests"
TERMINAL49_API_KEY = "8zoazZdxmyQS1zuNSKXDuGnm"
TERMINAL49_CUSTOMER_ID = "f7cb530a-9e60-412c-a5bc-205a2f34ba54"  # Default customer ID

boat_service_bp = Blueprint('boat_service', __name__, template_folder='../templates')

# Google Sheets configuration
GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE/export?format=csv&gid=0"
GOOGLE_SHEET_ID = "14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE"
GOOGLE_SHEET_EDIT_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?gid=0#gid=0"
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'credentials', 'service-account.json')
DROPBOX_CONTAINER_PATH = r"C:\Users\stream\Dropbox\Pictures-China (1)\container catalog\Container\container INFO"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'output', 'container_csvs')


def get_db_connection():
    """Get database connection"""
    from py.utilities.database_utils import get_sql_server_connection_fdc

    return get_sql_server_connection_fdc()


_FDC_CONTAINER_SORT_RE = re.compile(r'^([A-Za-z]+)(\d+)\s*$')
_FT_CONTAINER_RE = re.compile(r'^FT(\d+)$', re.IGNORECASE)


def normalize_container_id(container_val) -> str:
    """Canonical container id: FT25141 -> F25141."""
    text = str(container_val or "").strip().upper()
    if not text:
        return text
    match = _FT_CONTAINER_RE.match(text)
    if match:
        return f"F{match.group(1)}"
    return text


def _safe_float(val, default: float = 0.0) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _sanitize_for_json(value):
    """Ensure preview payloads are valid JSON (no NaN/Inf)."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    return value


def _fdc_container_sort_number(container_val):
    """Numeric part of IDs like F261107 so sort order matches humans (261107 before 26199)."""
    if pd.isna(container_val):
        return -1
    m = _FDC_CONTAINER_SORT_RE.match(str(container_val).strip())
    if not m:
        return -1
    try:
        return int(m.group(2))
    except ValueError:
        return -1


_HUB_STATUS_LABELS = {
    "upload": "New",
    "ocean": "Ocean",
    "landed": "Landed",
    "port": "Port",
    "minimal": "Waiting",
}


def _hub_action_mode(row) -> str:
    if bool(row.get("can_process", False)):
        return "new"
    status_key = str(row.get("status_key") or "minimal").strip()
    if status_key in {"ocean", "landed", "port"}:
        return status_key
    return "waiting"


_HUB_ACTION_SORT_ORDER = {
    "new": 0,
    "waiting": 1,
    "ocean": 2,
    "port": 3,
    "landed": 4,
}


def _received_sort_ts(row: dict) -> float:
    iso = str(row.get("received_sort") or "").strip()
    if not iso:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(iso, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _hub_row_sort_key(row: dict) -> tuple:
    """All view: New first, then Ocean, then Landed; landed by receive time new→old."""
    action_mode = str(row.get("action_mode") or "waiting")
    status_rank = _HUB_ACTION_SORT_ORDER.get(action_mode, 99)
    landed_rec = -_received_sort_ts(row) if action_mode == "landed" else 0.0
    return (status_rank, landed_rec, -_fdc_container_sort_number(row.get("container")))


def _written_time_column(df: pd.DataFrame) -> str | None:
    for candidate in ("written time", "Written time", "WRITTEN TIME"):
        if candidate in df.columns:
            return candidate
    if len(df.columns) >= 15:
        return str(df.columns[14])
    return None


def _written_by_column(df: pd.DataFrame, written_col: str | None) -> str | None:
    named = _find_sheet_column(df, ("written by", "written_by", "wrote by"))
    if named:
        return named
    if written_col and written_col in df.columns:
        cols = list(df.columns)
        idx = cols.index(written_col)
        if idx > 0:
            return str(cols[idx - 1])
    if len(df.columns) >= 14:
        return str(df.columns[13])
    return None


_MSRP_STATUS_CACHE = os.path.join(
    os.path.dirname(OUTPUT_DIR), "boat_cache", "msrp_status_cache.json"
)
_MSRP_OK_LOCKED = os.path.join(
    os.path.dirname(OUTPUT_DIR), "boat_cache", "msrp_ok_locked.json"
)


def _load_msrp_ok_locked() -> dict[str, dict]:
    if not os.path.isfile(_MSRP_OK_LOCKED):
        return {}
    try:
        with open(_MSRP_OK_LOCKED, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_msrp_ok_locked(payload: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(_MSRP_OK_LOCKED), exist_ok=True)
    with open(_MSRP_OK_LOCKED, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _msrp_ok_locked_entry(stat: dict) -> dict:
    return {
        "status": "ok",
        "missing": 0,
        "total": int(stat.get("total") or 0),
        "locked": True,
    }


def _lock_msrp_ok(container: str, stat: dict) -> None:
    key = normalize_container_id(container)
    locked = _load_msrp_ok_locked()
    locked[key] = _msrp_ok_locked_entry(stat)
    _save_msrp_ok_locked(locked)


def _promote_cached_msrp_ok_to_locked(cache: dict[str, dict]) -> None:
    """One-time promotion: previously scanned OK containers skip future file reads."""
    locked = _load_msrp_ok_locked()
    changed = False
    for key, stat in cache.items():
        if stat.get("status") != "ok":
            continue
        norm_key = normalize_container_id(key)
        if norm_key in locked:
            continue
        locked[norm_key] = _msrp_ok_locked_entry(stat)
        changed = True
    if changed:
        _save_msrp_ok_locked(locked)


def _load_msrp_status_cache() -> dict[str, dict]:
    if not os.path.isfile(_MSRP_STATUS_CACHE):
        return {}
    try:
        with open(_MSRP_STATUS_CACHE, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _save_msrp_status_cache(payload: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(_MSRP_STATUS_CACHE), exist_ok=True)
    with open(_MSRP_STATUS_CACHE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _scan_original_msrp_status(file_path: str) -> dict:
    """Count Price1 gaps in the container source Excel System File tab."""
    df = pd.read_excel(file_path, sheet_name="System File", header=2)
    df.columns = df.columns.str.replace("\n", " ").str.strip()
    df = _filter_system_file_line_items(df)
    total = len(df)
    if total == 0 or "Price1" not in df.columns:
        return {"status": "missing", "missing": total, "total": total}

    prices = pd.to_numeric(df["Price1"], errors="coerce")
    missing = int((prices.isna() | (prices <= 0)).sum())
    if missing == 0:
        status = "ok"
    elif missing >= total:
        status = "missing"
    else:
        status = "partial"
    return {"status": status, "missing": missing, "total": total}


def _build_msrp_status_lookup(container_files: dict[str, str]) -> dict[str, dict]:
    cache = _load_msrp_status_cache()
    _promote_cached_msrp_ok_to_locked(cache)
    locked = _load_msrp_ok_locked()
    updated_cache: dict[str, dict] = {}
    lookup: dict[str, dict] = dict(locked)

    for container, file_path in container_files.items():
        key = normalize_container_id(container)
        if key in locked:
            continue

        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            lookup[key] = {"status": "unknown", "missing": 0, "total": 0}
            continue

        cached = cache.get(key)
        if cached and cached.get("mtime") == mtime and cached.get("status") == "ok":
            _lock_msrp_ok(key, cached)
            entry = _msrp_ok_locked_entry(cached)
            lookup[key] = entry
            updated_cache[key] = {**cached, "locked": True}
            continue
        if cached and cached.get("mtime") == mtime:
            lookup[key] = cached
            updated_cache[key] = cached
            continue

        try:
            stat = _scan_original_msrp_status(file_path)
        except Exception as exc:
            current_app.logger.warning("MSRP scan failed for %s: %s", key, exc)
            stat = {"status": "unknown", "missing": 0, "total": 0}
        stat["mtime"] = mtime
        if stat.get("status") == "ok":
            _lock_msrp_ok(key, stat)
            stat = _msrp_ok_locked_entry(stat)
        lookup[key] = stat
        updated_cache[key] = stat

    for key, entry in locked.items():
        lookup[key] = entry
        if key not in updated_cache:
            updated_cache[key] = entry

    _save_msrp_status_cache(updated_cache)
    return lookup


def _msrp_price_map_from_original(file_path: str) -> dict[str, float]:
    """Return SKU -> Price1 from the original container file (Price1 > 0 only)."""
    df = pd.read_excel(file_path, sheet_name="System File", header=2)
    df.columns = df.columns.str.replace("\n", " ").str.strip()
    df = _filter_system_file_line_items(df)
    mapping: dict[str, float] = {}
    for _, row in df.iterrows():
        style = str(row.get("STYLE NO", "")).strip()
        if not style:
            continue
        color_raw = str(row.get("COLOR", "")).strip()
        color = normalize_color_to_abbrev(color_raw)
        sku = f"{style} {color}".strip().upper()
        price = _safe_float(row.get("Price1"))
        if sku and price > 0:
            mapping[sku] = price
    return mapping


def _container_po_skus(cursor, container_number: str) -> set[str]:
    cursor.execute(
        """
        SELECT DISTINCT RTRIM(prod_cd)
        FROM dbo.plog
        WHERE RTRIM(pur_num) = ?
          AND ISNULL(log_qty, 0) > 0
        """,
        (container_number.strip().upper(),),
    )
    return {str(row[0]).strip().upper() for row in cursor.fetchall() if row[0]}


def _fetch_retail_has_zero_lookup(container_list: list[str]) -> dict[str, bool]:
    """
    Return container -> True when any PO SKU still has RETAIL_PRS <= 0 in FDC inv.
    Paired with Excel MSRP OK to decide when Update MSRP should appear.
    """
    if not container_list:
        return {}

    conn = get_db_connection()
    cursor = conn.cursor()
    lookup: dict[str, bool] = {}
    try:
        placeholders = ",".join(["?"] * len(container_list))
        cursor.execute(
            f"""
            SELECT RTRIM(p.pur_num) AS pur_num,
                   COUNT(DISTINCT RTRIM(p.prod_cd)) AS sku_count,
                   MIN(ISNULL(i.RETAIL_PRS, 0)) AS min_retail
            FROM dbo.plog p
            LEFT JOIN dbo.inv i ON RTRIM(i.PROD_CD) = RTRIM(p.prod_cd)
            WHERE RTRIM(p.pur_num) IN ({placeholders})
              AND ISNULL(p.log_qty, 0) > 0
            GROUP BY RTRIM(p.pur_num)
            """,
            container_list,
        )
        for row in cursor.fetchall():
            pur_num = str(row[0]).strip()
            sku_count = int(row[1] or 0)
            min_retail = float(row[2] or 0)
            lookup[pur_num] = sku_count > 0 and min_retail <= 0
    finally:
        conn.close()
    return lookup


def _sync_styles_csv_retail_prices(container_number: str, price_updates: dict[str, float]) -> int:
    normalized = normalize_container_id(container_number)
    csv_path = os.path.join(OUTPUT_DIR, f"{normalized}-styles.csv")
    if not os.path.exists(csv_path) or not price_updates:
        return 0

    df = pd.read_csv(csv_path)
    if "Item#" not in df.columns or "Price1" not in df.columns:
        return 0

    changed = 0
    for idx, record in df.iterrows():
        sku = str(record.get("Item#", "")).strip().upper()
        if sku in price_updates:
            df.at[idx, "Price1"] = price_updates[sku]
            changed += 1
    if changed:
        df.to_csv(csv_path, index=False)
    return changed


def _run_container_msrp_update(container_number: str, current_user: str) -> dict:
    normalized = normalize_container_id(container_number)
    container_files = scan_dropbox_containers()
    if normalized not in container_files:
        return {
            "success": False,
            "error": f"No Dropbox source file found for {normalized}",
        }

    original_prices = _msrp_price_map_from_original(container_files[normalized])
    if not original_prices:
        return {
            "success": False,
            "error": (
                f"No Price1 values found in the original file for {normalized}. "
                "Add MSRP to the Excel file or use Reprocess to enter retail manually."
            ),
        }

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        po_skus = _container_po_skus(cursor, normalized)
    finally:
        conn.close()

    if not po_skus:
        return {
            "success": False,
            "error": f"No OMS PO lines found for {normalized}",
        }

    price_updates = {
        sku: original_prices[sku]
        for sku in po_skus
        if sku in original_prices and original_prices[sku] > 0
    }
    if not price_updates:
        return {
            "success": False,
            "error": (
                f"Original file has no usable Price1 for SKUs on PO {normalized}. "
                "Fix the Excel file or use Reprocess to enter retail manually."
            ),
        }

    from decimal import Decimal

    decimal_updates = {sku: Decimal(str(price)) for sku, price in price_updates.items()}
    result = update_retail_prices_dual(decimal_updates, adjusted_by=current_user)
    csv_changed = _sync_styles_csv_retail_prices(normalized, price_updates)

    msrp_stat = _scan_original_msrp_status(container_files[normalized])
    still_missing = msrp_stat.get("missing", 0)
    message = (
        f"Updated MSRP on {len(result.get('updated_fdc', []))} SKU(s) in FDC and "
        f"{len(result.get('updated_efas', []))} SKU(s) in EFAS."
    )
    if csv_changed:
        message += f" Styles CSV updated ({csv_changed} row(s))."
    if still_missing:
        message += (
            f" {still_missing} line(s) still have no Price1 in the original file."
        )

    return {
        "success": result.get("success", False),
        "message": message,
        "updated_count": len(price_updates),
        "still_missing_msrp": still_missing,
        "result": result,
    }


def _erp_log_time_to_display(log_time) -> str:
    """Decode OMS plog LOG_TIME to HH:MM:SS."""
    if log_time is None:
        return ""
    try:
        total = int(log_time)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    seconds = total // 100
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _fetch_po_created_lookup(container_list: list[str]) -> dict[str, tuple[str, str]]:
    """Return container -> (po_date, po_time) from pur_ord + plog."""
    if not container_list:
        return {}

    conn = get_db_connection()
    cursor = conn.cursor()
    lookup: dict[str, tuple[str, str]] = {}
    try:
        placeholders = ",".join(["?"] * len(container_list))
        cursor.execute(
            f"""
            SELECT RTRIM(p.pur_num) AS pur_num,
                   RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(p.pur_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                   RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(p.pur_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                   CAST(YEAR(DATEADD(DAY, CAST(p.pur_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS po_date,
                   MIN(pl.log_time) AS log_time
            FROM dbo.pur_ord p
            LEFT JOIN dbo.plog pl
                ON RTRIM(pl.pur_num) = RTRIM(p.pur_num)
               AND pl.pur_cd = ?
            WHERE RTRIM(p.pur_num) IN ({placeholders})
            GROUP BY RTRIM(p.pur_num), p.pur_dt
            """,
            (PO_LINE_PUR_CD, *container_list),
        )
        for pur_num, po_date, log_time in cursor.fetchall():
            key = str(pur_num or "").strip().upper()
            date_part = str(po_date or "").strip()
            time_part = _erp_log_time_to_display(log_time)
            if key and date_part:
                lookup[key] = (date_part, time_part)
    finally:
        conn.close()
    return lookup


def _fetch_receive_time_lookup(container_list: list[str]) -> dict[str, tuple[str, str]]:
    """Container -> (receive_date, receive_time) from OMS receive plog (PUR_CD=2).

    Falls back to inv_data.LASTRCV_DT (date only) when a landed PO has no receive plog yet.
    """
    if not container_list:
        return {}

    conn = get_db_connection()
    cursor = conn.cursor()
    lookup: dict[str, tuple[str, str]] = {}
    try:
        placeholders = ",".join(["?"] * len(container_list))
        cursor.execute(
            f"""
            SELECT RTRIM(ranked.pur_num) AS pur_num,
                   RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(ranked.log_date AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                   RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(ranked.log_date AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                   CAST(YEAR(DATEADD(DAY, CAST(ranked.log_date AS INT) - 81088, '20230101')) AS VARCHAR) AS rec_date,
                   ranked.log_time
            FROM (
                SELECT RTRIM(pl.pur_num) AS pur_num,
                       pl.log_date,
                       pl.log_time,
                       ROW_NUMBER() OVER (
                           PARTITION BY RTRIM(pl.pur_num)
                           ORDER BY pl.log_date ASC, pl.log_time ASC
                       ) AS rn
                FROM dbo.plog pl
                WHERE RTRIM(pl.pur_num) IN ({placeholders})
                  AND pl.pur_cd = ?
                  AND ISNULL(pl.log_date, 0) > 0
            ) ranked
            WHERE ranked.rn = 1
            """,
            (*container_list, RECEIVE_ALLOC_PUR_CD),
        )
        for pur_num, rec_date, log_time in cursor.fetchall():
            key = str(pur_num or "").strip().upper()
            date_part = str(rec_date or "").strip()
            if key and date_part:
                lookup[key] = (date_part, _erp_log_time_to_display(log_time))

        missing = [
            str(c).strip().upper()
            for c in container_list
            if str(c).strip().upper() and str(c).strip().upper() not in lookup
        ]
        if missing:
            miss_ph = ",".join(["?"] * len(missing))
            cursor.execute(
                f"""
                SELECT RTRIM(bp.pur_num) AS pur_num,
                       RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(MIN(id.lastrcv_dt) AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                       RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(MIN(id.lastrcv_dt) AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                       CAST(YEAR(DATEADD(DAY, CAST(MIN(id.lastrcv_dt) AS INT) - 81088, '20230101')) AS VARCHAR) AS rec_date
                FROM dbo.bin_pur bp
                INNER JOIN dbo.inv_data id
                    ON RTRIM(id.prod_cd) = RTRIM(bp.prod_cd)
                   AND RTRIM(id.whs_num) = '01'
                WHERE RTRIM(bp.pur_num) IN ({miss_ph})
                  AND bp.pur_cd = ?
                  AND ISNULL(bp.rcvd_qty, 0) > 0
                  AND id.lastrcv_dt IS NOT NULL
                  AND CAST(id.lastrcv_dt AS INT) > 0
                GROUP BY RTRIM(bp.pur_num)
                """,
                (*missing, PO_LINE_PUR_CD),
            )
            for pur_num, rec_date in cursor.fetchall():
                key = str(pur_num or "").strip().upper()
                date_part = str(rec_date or "").strip()
                if key and date_part and key not in lookup:
                    lookup[key] = (date_part, "")
    finally:
        conn.close()
    return lookup


def _hub_received_sort_iso(date_part: str, time_part: str) -> str:
    """ISO timestamp for sorting receive time new → old."""
    date_part = str(date_part or "").strip()
    time_part = str(time_part or "").strip()
    if not date_part:
        return ""
    stamp = f"{date_part} {time_part}".strip() if time_part else date_part
    parsed_date, parsed_time = _split_datetime_display(stamp)
    if not parsed_date:
        return ""
    clock = parsed_time or "00:00:00"
    if clock.count(":") == 1:
        clock = f"{clock}:00"
    try:
        return datetime.strptime(f"{parsed_date} {clock}", "%m/%d/%Y %H:%M:%S").strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    except ValueError:
        return ""


_RECEIVE_TIMES_PATH = os.path.join(os.path.dirname(OUTPUT_DIR), "boat_cache", "receive_times.json")
_RECEIVE_TIMES_VERSION = 2
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_PACKSLIP_CONTAINER_RE = re.compile(r"FDC_container=([A-Za-z0-9]+)", re.IGNORECASE)
_MARK_LANDED_CONTAINER_RE = re.compile(r"marked container ([A-Za-z0-9]+) landed", re.IGNORECASE)


def _receive_dt_to_pair(dt: datetime) -> tuple[str, str]:
    return dt.strftime("%m/%d/%Y"), dt.strftime("%H:%M:%S")


def _parse_log_timestamp(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19),):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def _receive_log_files() -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    candidates = [
        getattr(Config, "USER_LOG_FILE", ""),
        getattr(Config, "SYSTEM_LOG_FILE", ""),
        getattr(Config, "LOG_FILE", ""),
    ]
    for path in candidates:
        if not path:
            continue
        variants = [path] + [f"{path}.{i}" for i in range(1, 4)]
        for candidate in variants:
            key = os.path.normcase(os.path.abspath(candidate))
            if key in seen or not os.path.isfile(candidate):
                continue
            seen.add(key)
            paths.append(candidate)
    return paths


def _load_receive_times_store() -> dict:
    empty = {"version": _RECEIVE_TIMES_VERSION, "files": {}, "containers": {}}
    if not os.path.isfile(_RECEIVE_TIMES_PATH):
        return empty
    try:
        with open(_RECEIVE_TIMES_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("version") != _RECEIVE_TIMES_VERSION:
            return empty
        data.setdefault("files", {})
        data.setdefault("containers", {})
        if not isinstance(data["files"], dict):
            data["files"] = {}
        if not isinstance(data["containers"], dict):
            data["containers"] = {}
        data["version"] = _RECEIVE_TIMES_VERSION
        return data
    except Exception:
        return empty


def _save_receive_times_store(store: dict) -> None:
    folder = os.path.dirname(_RECEIVE_TIMES_PATH)
    os.makedirs(folder, exist_ok=True)
    store["version"] = _RECEIVE_TIMES_VERSION
    tmp_path = _RECEIVE_TIMES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2)
    os.replace(tmp_path, _RECEIVE_TIMES_PATH)


def _merge_receive_event(containers: dict, container: str, dt: datetime, source: str) -> None:
    key = normalize_container_id(container)
    if not key or dt is None:
        return
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
    existing = containers.get(key) or {}
    existing_source = str(existing.get("source") or "")
    existing_iso = str(existing.get("sort") or "")
    keep = False
    if source == "packing_slip":
        if existing_source == "mark_landed" and existing_iso and iso > existing_iso:
            return
        if existing_source == "packing_slip" and existing_iso and iso >= existing_iso:
            return
        keep = True
    elif source == "mark_landed":
        if existing_source == "packing_slip":
            return
        if existing_iso and iso <= existing_iso:
            return
        keep = True
    else:
        keep = not existing_iso
    if not keep:
        return
    date_part, time_part = _receive_dt_to_pair(dt)
    containers[key] = {
        "date": date_part,
        "time": time_part,
        "sort": iso,
        "source": source,
    }


def _scan_log_receive_events(text: str, containers: dict) -> None:
    current_ts = None
    pending_kind = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ts_match = _LOG_TS_RE.match(line)
        if ts_match:
            current_ts = _parse_log_timestamp(ts_match.group(1))
            pending_kind = None
            low = line.lower()
            if "downloaded packing slip" in low:
                found = _PACKSLIP_CONTAINER_RE.search(line)
                if found and current_ts:
                    _merge_receive_event(containers, found.group(1), current_ts, "packing_slip")
                else:
                    pending_kind = "packing_slip"
            elif "marked container" in low and "landed" in low:
                found = _MARK_LANDED_CONTAINER_RE.search(line)
                if found and current_ts:
                    _merge_receive_event(containers, found.group(1), current_ts, "mark_landed")
            elif "@@@marked landed@@@" in low:
                found = _PACKSLIP_CONTAINER_RE.search(line)
                if found and current_ts:
                    _merge_receive_event(containers, found.group(1), current_ts, "mark_landed")
                else:
                    pending_kind = "mark_landed"
            continue
        if pending_kind and current_ts:
            found = _PACKSLIP_CONTAINER_RE.search(line)
            if found:
                _merge_receive_event(containers, found.group(1), current_ts, pending_kind)
                pending_kind = None


def _load_receive_time_log_lookup() -> dict[str, tuple[str, str]]:
    """Container -> (date, time) from packing-slip / mark-landed app logs."""
    store = _load_receive_times_store()
    files_meta = store["files"]
    containers = store["containers"]
    changed = False
    for path in _receive_log_files():
        try:
            stat = os.stat(path)
        except OSError:
            continue
        key = os.path.normcase(os.path.abspath(path))
        meta = files_meta.get(key) or {}
        if int(meta.get("mtime") or 0) == int(stat.st_mtime) and int(meta.get("size") or 0) == int(stat.st_size):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        _scan_log_receive_events(text, containers)
        files_meta[key] = {"mtime": int(stat.st_mtime), "size": int(stat.st_size)}
        changed = True
    if changed:
        store["files"] = files_meta
        store["containers"] = containers
        store["version"] = _RECEIVE_TIMES_VERSION
        try:
            _save_receive_times_store(store)
        except Exception:
            pass
    lookup: dict[str, tuple[str, str]] = {}
    for container, info in containers.items():
        date_part = str((info or {}).get("date") or "").strip()
        time_part = str((info or {}).get("time") or "").strip()
        if date_part:
            lookup[str(container).strip().upper()] = (date_part, time_part)
    return lookup


def _stamp_container_receive_time(container: str, when: datetime, source: str = "mark_landed") -> None:
    store = _load_receive_times_store()
    _merge_receive_event(store["containers"], container, when, source)
    _save_receive_times_store(store)


def _clear_container_receive_time(container: str) -> None:
    key = normalize_container_id(container)
    if not key:
        return
    store = _load_receive_times_store()
    if key in store["containers"]:
        store["containers"].pop(key, None)
        _save_receive_times_store(store)


def _hub_receive_fields(
    *,
    action_mode: str,
    log_receive: tuple[str, str],
    oms_receive: tuple[str, str],
    processed_date: str = "",
    processed_time: str = "",
) -> dict[str, str]:
    """Landed receive time: OMS receive plog clock first, else packing-slip logs, else date-only."""
    received_date = ""
    received_time = ""
    if action_mode == "landed":
        log_date, log_time = log_receive or ("", "")
        oms_date, oms_time = oms_receive or ("", "")
        if oms_date and oms_time:
            received_date, received_time = oms_date, oms_time
        elif log_date:
            received_date, received_time = log_date, log_time
        elif oms_date:
            received_date, received_time = oms_date, oms_time
    return {
        "received_date": received_date,
        "received_time": received_time,
        "received_sort": _hub_received_sort_iso(received_date, received_time),
    }


def _split_datetime_display(raw) -> tuple[str, str]:
    """Return (date, time) strings for hub display."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "", ""
    if hasattr(raw, "to_pydatetime"):
        try:
            raw = raw.to_pydatetime()
        except Exception:
            pass
    if isinstance(raw, datetime):
        date_part = raw.strftime("%m/%d/%Y")
        time_part = raw.strftime("%H:%M:%S") if (raw.hour or raw.minute or raw.second) else ""
        return date_part, time_part
    text = str(raw or "").strip()
    if not text or text.lower() in {"written time", "nan", "none", "nat"}:
        return "", ""
    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            date_part = dt.strftime("%m/%d/%Y")
            time_part = dt.strftime("%H:%M:%S") if "%H" in fmt else ""
            if time_part in {"00:00:00", "00:00"}:
                time_part = ""
            return date_part, time_part
        except ValueError:
            continue
    if " " in text:
        date_part, time_part = text.split(None, 1)
        clock = time_part.strip()
        if clock in {"00:00:00", "00:00"}:
            clock = ""
        return date_part.strip(), clock
    return text, ""


def _msrp_status_label(status: str, missing: int, total: int) -> str:
    """Excel-only MSRP badge: OK or Need Ken."""
    if status == "ok":
        return "OK"
    if status == "need_ken":
        return "Need Ken"
    return "—"


def _excel_msrp_display_status(raw_status: str, dropbox_available: bool) -> str:
    """Normalize Excel scan result to ok | need_ken | unknown for hub display."""
    if not dropbox_available:
        return "unknown"
    if raw_status == "ok":
        return "ok"
    return "need_ken"


def _find_sheet_column(df: pd.DataFrame, needles: tuple[str, ...]) -> str | None:
    for col in df.columns:
        label = str(col).strip().lower()
        for needle in needles:
            if needle in label:
                return col
    return None


def _find_sheet_eta_column(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        if str(col).strip().lower() == "eta":
            return col
    return None


def _tracking_date_display(raw) -> str:
    date_part, _time_part = _split_datetime_display(raw)
    if date_part:
        return date_part
    return _clean_hub_text(raw)


def _clean_hub_text(raw) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = str(raw).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


def _build_container_hub_rows(
    df: pd.DataFrame,
    container_files: dict[str, str],
) -> list[dict]:
    """Build simplified container rows for the hub page."""
    sheet = df.copy()
    if "FDC container" not in sheet.columns:
        return []

    msrp_lookup = _build_msrp_status_lookup(container_files)

    if "BOL NUM" in sheet.columns:
        sheet = sheet.rename(columns={"FDC container": "container", "BOL NUM": "bol"})
    else:
        sheet = sheet.rename(columns={"FDC container": "container"})
        sheet["bol"] = ""

    sheet["container"] = sheet["container"].astype(str).str.strip().str.upper().map(normalize_container_id)
    sheet = sheet[
        sheet["container"].notna()
        & (sheet["container"] != "")
        & (~sheet["container"].str.lower().isin({"nan", "none"}))
    ].copy()
    sheet["bol"] = sheet["bol"].fillna("").astype(str).str.strip()
    sheet = sheet.drop_duplicates(subset=["container"], keep="last")
    sheet["_fdc_sort_num"] = sheet["container"].map(_fdc_container_sort_number)
    sheet = sheet.sort_values(by="_fdc_sort_num", ascending=False).drop(columns=["_fdc_sort_num"])
    written_col = _written_time_column(sheet)
    written_by_col = _written_by_column(sheet, written_col)
    voyage_col = _find_sheet_column(sheet, ("voyage", "vessel no", "vesso"))
    etd_col = _find_sheet_column(sheet, ("eta1", "etd"))
    eta_col = _find_sheet_eta_column(sheet)
    rcd_col = _find_sheet_column(sheet, ("rcd",))

    container_list = sheet["container"].tolist()
    rcvd_lookup: dict[str, float] = {}
    po_created_lookup: dict[str, tuple[str, str]] = {}
    receive_time_lookup: dict[str, tuple[str, str]] = {}
    log_receive_lookup: dict[str, tuple[str, str]] = {}
    retail_has_zero_lookup: dict[str, bool] = {}
    tracking_lookup: dict[str, dict[str, str]] = {}

    if container_list:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            placeholders = ",".join(["?"] * len(container_list))
            cursor.execute(
                f"""
                SELECT PUR_NUM, SUM(RCVD_QTY) AS total_rcvd
                FROM dbo.bin_pur
                WHERE PUR_NUM IN ({placeholders})
                  AND pur_cd = 1
                GROUP BY PUR_NUM
                """,
                container_list,
            )
            rcvd_lookup = {str(row[0]).strip(): row[1] for row in cursor.fetchall()}
        finally:
            conn.close()
        po_created_lookup = _fetch_po_created_lookup(container_list)
        try:
            receive_time_lookup = _fetch_receive_time_lookup(container_list)
        except Exception as e:
            current_app.logger.warning(f"Could not load receive-time lookup: {e}")
        try:
            log_receive_lookup = _load_receive_time_log_lookup()
        except Exception as e:
            current_app.logger.warning(f"Could not load receive-time logs: {e}")
        retail_has_zero_lookup = _fetch_retail_has_zero_lookup(container_list)
        try:
            tracking_lookup = load_shipment_tracking_lookup(container_list)
        except Exception as e:
            current_app.logger.warning(f"Could not load shipment tracking lookup: {e}")

    rows: list[dict] = []
    for _, record in sheet.iterrows():
        container_num = str(record["container"]).strip()
        rcvd_qty = rcvd_lookup.get(container_num)
        dropbox_available = container_num in container_files
        in_oms = rcvd_qty is not None
        can_process = dropbox_available and not in_oms

        if rcvd_qty is not None and rcvd_qty > 0:
            status_key = "landed"
        elif rcvd_qty is not None and rcvd_qty == 0:
            status_key = "ocean"
        else:
            status_key = "upload"

        styles_file = os.path.join(OUTPUT_DIR, f"{container_num}-styles.csv")
        po_file = os.path.join(OUTPUT_DIR, f"{container_num}-PO.csv")
        written_raw = record.get(written_col) if written_col else ""
        written_date, written_clock = _split_datetime_display(written_raw)
        written_by = _clean_hub_text(record.get(written_by_col)) if written_by_col else ""
        processed_date, processed_clock = po_created_lookup.get(container_num, ("", ""))
        msrp_info = msrp_lookup.get(container_num, {})
        raw_msrp_status = str(msrp_info.get("status") or "unknown")
        msrp_missing = int(msrp_info.get("missing") or 0)
        msrp_total = int(msrp_info.get("total") or 0)
        msrp_status = _excel_msrp_display_status(raw_msrp_status, dropbox_available)
        retail_has_zero = retail_has_zero_lookup.get(container_num, False)
        show_update_msrp = (
            in_oms
            and dropbox_available
            and msrp_status == "ok"
            and retail_has_zero
        )

        tracking = tracking_lookup.get(container_num, {})
        bol = _clean_hub_text(record.get("bol")) or _clean_hub_text(tracking.get("container_no"))
        vessel_no = _clean_hub_text(record.get(voyage_col)) if voyage_col else ""
        if not vessel_no:
            vessel_no = _clean_hub_text(tracking.get("vessel_no"))

        etd = _tracking_date_display(record.get(etd_col)) if etd_col else ""
        if not etd:
            etd = _clean_hub_text(tracking.get("etd"))

        eta = _tracking_date_display(record.get(eta_col)) if eta_col else ""
        if not eta:
            eta = _clean_hub_text(tracking.get("eta"))

        rcd_raw = record.get(rcd_col) if rcd_col else ""
        rcd_date, rcd_clock = _split_datetime_display(rcd_raw)
        rcd = rcd_date or _tracking_date_display(rcd_raw)
        if not rcd:
            deliver_raw = tracking.get("deliver_date")
            deliver_date, deliver_clock = _split_datetime_display(deliver_raw)
            rcd = deliver_date or _clean_hub_text(deliver_raw)
            if not rcd_clock:
                rcd_clock = deliver_clock

        action_mode = _hub_action_mode(
            {"can_process": can_process, "status_key": status_key}
        )
        receive_fields = _hub_receive_fields(
            action_mode=action_mode,
            log_receive=log_receive_lookup.get(container_num, ("", "")),
            oms_receive=receive_time_lookup.get(container_num, ("", "")),
            processed_date=processed_date,
            processed_time=processed_clock,
        )

        row = {
            "container": container_num,
            "bol": bol,
            "vessel_no": vessel_no,
            "etd": etd,
            "eta": eta,
            "rcd": rcd,
            "written_date": written_date,
            "written_time": written_clock,
            "written_by": written_by,
            "processed_date": processed_date,
            "processed_time": processed_clock,
            **receive_fields,
            "msrp_status": msrp_status,
            "msrp_missing_count": msrp_missing,
            "msrp_total_count": msrp_total,
            "msrp_label": _msrp_status_label(msrp_status, msrp_missing, msrp_total),
            "retail_has_zero": retail_has_zero,
            "show_update_msrp": show_update_msrp,
            "status_key": status_key,
            "status_label": _HUB_STATUS_LABELS.get(status_key, status_key.title()),
            "action_mode": action_mode,
            "dropbox_available": dropbox_available,
            "in_oms": in_oms,
            "can_process": can_process,
            "has_styles_csv": os.path.exists(styles_file),
            "has_po_csv": os.path.exists(po_file),
            "is_special": container_num.startswith("L"),
        }
        rows.append(row)

    rows.sort(key=_hub_row_sort_key)
    return rows


def lookup_boat_hub_row(container_id: str) -> dict | None:
    """Find one container row from the cached /boat/data payload."""
    normalized = normalize_container_id(container_id)
    if not normalized:
        return None
    payload = load_latest_cached_boat_payload() or {}
    for row in payload.get("data") or []:
        if normalize_container_id(row.get("container")) == normalized:
            if not str(row.get("rcd") or "").strip():
                row["rcd"] = _sheet_rcd_map().get(normalized, "")
            return row
    return None


def _refresh_cached_hub_row(container_number: str, rcd_override: str | None = None) -> dict | None:
    """Re-read OMS status for one container and patch that row in the hub cache."""
    normalized = normalize_container_id(container_number)
    if not normalized:
        return None
    payload = load_latest_cached_boat_payload()
    if not payload:
        return None
    rows = payload.get("data") or []
    idx = None
    target = None
    for i, row in enumerate(rows):
        if normalize_container_id(row.get("container")) == normalized:
            idx = i
            target = dict(row)
            break
    if target is None:
        return None

    rcvd_qty = None
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT SUM(RCVD_QTY) AS total_rcvd
            FROM dbo.bin_pur
            WHERE RTRIM(PUR_NUM) = ?
              AND pur_cd = 1
            """,
            (normalized,),
        )
        rec = cursor.fetchone()
        if rec and rec[0] is not None:
            rcvd_qty = rec[0]
        else:
            cursor.execute(
                "SELECT TOP 1 1 FROM dbo.pur_ord WHERE RTRIM(pur_num) = ?",
                (normalized,),
            )
            if cursor.fetchone():
                rcvd_qty = 0
    finally:
        conn.close()

    po_created_lookup = _fetch_po_created_lookup([normalized])
    receive_time_lookup: dict[str, tuple[str, str]] = {}
    log_receive_lookup: dict[str, tuple[str, str]] = {}
    try:
        receive_time_lookup = _fetch_receive_time_lookup([normalized])
    except Exception as e:
        current_app.logger.warning(f"Could not load receive-time lookup for {normalized}: {e}")
    try:
        log_receive_lookup = _load_receive_time_log_lookup()
    except Exception as e:
        current_app.logger.warning(f"Could not load receive-time logs for {normalized}: {e}")
    retail_has_zero_lookup = _fetch_retail_has_zero_lookup([normalized])
    in_oms = rcvd_qty is not None
    dropbox_available = bool(target.get("dropbox_available"))
    can_process = dropbox_available and not in_oms
    if rcvd_qty is not None and float(rcvd_qty) > 0:
        status_key = "landed"
    elif in_oms:
        status_key = "ocean"
    else:
        status_key = "upload"
    processed_date, processed_clock = po_created_lookup.get(normalized, ("", ""))
    msrp_status = str(target.get("msrp_status") or "unknown")
    retail_has_zero = bool(retail_has_zero_lookup.get(normalized, False))
    action_mode = _hub_action_mode(
        {"can_process": can_process, "status_key": status_key}
    )
    rcd = str(rcd_override if rcd_override is not None else target.get("rcd") or "").strip()
    rcd_date, rcd_clock = _split_datetime_display(rcd)
    if rcd_date:
        target["rcd"] = rcd_date
    elif rcd_override is not None:
        target["rcd"] = rcd
    receive_fields = _hub_receive_fields(
        action_mode=action_mode,
        log_receive=log_receive_lookup.get(normalized, ("", "")),
        oms_receive=receive_time_lookup.get(normalized, ("", "")),
        processed_date=processed_date,
        processed_time=processed_clock,
    )
    target.update(
        {
            "in_oms": in_oms,
            "can_process": can_process,
            "status_key": status_key,
            "status_label": _HUB_STATUS_LABELS.get(status_key, status_key.title()),
            "action_mode": action_mode,
            "processed_date": processed_date,
            "processed_time": processed_clock,
            **receive_fields,
            "retail_has_zero": retail_has_zero,
            "show_update_msrp": (
                in_oms
                and dropbox_available
                and msrp_status == "ok"
                and retail_has_zero
            ),
            "has_styles_csv": os.path.exists(
                os.path.join(OUTPUT_DIR, f"{normalized}-styles.csv")
            ),
            "has_po_csv": os.path.exists(
                os.path.join(OUTPUT_DIR, f"{normalized}-PO.csv")
            ),
        }
    )
    rows[idx] = target
    payload["data"] = rows
    payload["counts"] = {
        "total": len(rows),
        "new": sum(1 for r in rows if r.get("action_mode") == "new"),
        "ocean": sum(1 for r in rows if r.get("action_mode") == "ocean"),
        "landed": sum(1 for r in rows if r.get("action_mode") == "landed"),
        "special": sum(1 for r in rows if r.get("is_special")),
    }
    save_cached_boat_data(str(payload.get("cache_key") or ""), payload)
    return target


def _sheet_rcd_map() -> dict[str, str]:
    """FDC container -> RCD date from the local Google-sheet cache."""
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "output", "boat_cache", "container_import_sheet.csv")
    )
    if not os.path.isfile(path):
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    if "FDC container" not in df.columns:
        return {}
    rcd_col = _find_sheet_column(df, ("rcd",))
    if not rcd_col:
        return {}
    out: dict[str, str] = {}
    for _, rec in df.iterrows():
        cid = normalize_container_id(rec.get("FDC container"))
        if not cid:
            continue
        rcd = _tracking_date_display(rec.get(rcd_col))
        if rcd:
            out[cid] = rcd
    return out


def _fill_missing_rcd(rows: list[dict]) -> None:
    rcd_map = _sheet_rcd_map()
    landed_ids: list[str] = []
    for row in rows or []:
        if not row:
            continue
        cid = normalize_container_id(row.get("container"))
        if cid and not str(row.get("rcd") or "").strip() and rcd_map.get(cid):
            row["rcd"] = rcd_map[cid]
        if str(row.get("action_mode") or "") == "landed" and cid:
            landed_ids.append(cid)
    if not landed_ids:
        return

    oms_lookup: dict[str, tuple[str, str]] = {}
    try:
        oms_lookup = _fetch_receive_time_lookup(list(dict.fromkeys(landed_ids)))
    except Exception:
        oms_lookup = {}
    log_lookup: dict[str, tuple[str, str]] = {}
    try:
        log_lookup = _load_receive_time_log_lookup()
    except Exception:
        log_lookup = {}
    for row in rows or []:
        if not row or str(row.get("action_mode") or "") != "landed":
            continue
        cid = normalize_container_id(row.get("container"))
        row.update(
            _hub_receive_fields(
                action_mode="landed",
                log_receive=log_lookup.get(cid, ("", "")),
                oms_receive=oms_lookup.get(cid, ("", "")),
            )
        )


def sidebar_container_lists(*, arrived_limit: int = 5) -> tuple[list[dict], list[dict]]:
    """Ocean (all, newest first) and latest arrived containers for the search sidebar."""
    payload = load_latest_cached_boat_payload() or {}
    rcd_map = _sheet_rcd_map()
    ocean: list[dict] = []
    arrived: list[dict] = []
    seen_ocean: set[str] = set()
    seen_arrived: set[str] = set()
    for row in payload.get("data") or []:
        cid = normalize_container_id(row.get("container"))
        if not cid or cid.startswith("L"):
            continue
        mode = str(row.get("action_mode") or "").strip().lower()
        item = {
            "id": cid,
            "eta": str(row.get("eta") or "").strip(),
            "rcd": str(row.get("rcd") or "").strip() or rcd_map.get(cid, ""),
        }
        if mode in {"ocean", "port"}:
            if cid not in seen_ocean:
                seen_ocean.add(cid)
                ocean.append(item)
        elif mode == "landed":
            if cid not in seen_arrived:
                seen_arrived.add(cid)
                arrived.append(item)
    return ocean, arrived[:arrived_limit]


FDCEF_CONTAINER_SEARCH_URL = "https://www.fdcef.com/container_search"


def _arrival_date_mmdd(raw) -> str:
    """Format Receive Time (or similar date) as MM/DD with no year."""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.split()[0].replace("-", "/")
    parts = [p for p in text.split("/") if p]
    try:
        if len(parts) >= 3 and len(parts[0]) == 4:
            return f"{int(parts[1]):02d}/{int(parts[2]):02d}"
        if len(parts) >= 2:
            return f"{int(parts[0]):02d}/{int(parts[1]):02d}"
    except (TypeError, ValueError):
        return ""
    return ""


def recent_arrivals_for_dashboard(*, limit: int = 8) -> list[dict]:
    """Latest landed containers from the Container Hub cache for the homepage sidebar.

    Reads the same cached /boat/data payload as ``sidebar_container_lists``
    (Container Hub / container_search) without re-parsing the sheet CSV.
    Display date is boat landed ``received_date`` (Receive Time / 收货时间), MM/DD.
    Returns dicts with id, arrival_date (MM/DD), and an fdcef.com container_search URL.
    """
    try:
        cap = max(1, min(int(limit or 8), 12))
    except (TypeError, ValueError):
        cap = 8
    payload = load_latest_cached_boat_payload() or {}
    out: list[dict] = []
    seen: set[str] = set()
    for row in payload.get("data") or []:
        cid = normalize_container_id(row.get("container"))
        if not cid or cid.startswith("L") or cid in seen:
            continue
        if str(row.get("action_mode") or "").strip().lower() != "landed":
            continue
        date_raw = str(row.get("received_date") or "").strip()
        arrival_date = _arrival_date_mmdd(date_raw)
        if not arrival_date:
            continue
        seen.add(cid)
        out.append(
            {
                "id": cid,
                "arrival_date": arrival_date,
                "url": f"{FDCEF_CONTAINER_SEARCH_URL}?container_id={quote(cid, safe='')}",
            }
        )
        if len(out) >= cap:
            break
    return out


_PACKSLIP_BIG_CUSTOMER_SQL = """
    UPPER(c.cus_nm) LIKE '%ROSS%'
    OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACTORY%'
    OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACOTORY%'
"""


def _is_packslip_big_customer(name: str) -> bool:
    upper = str(name or "").upper()
    return (
        "ROSS" in upper
        or "BURLINGTON COAT FACTORY" in upper
        or "BURLINGTON COAT FACOTORY" in upper
    )


from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles.colors import Color


_PACKSLIP_NUM_FONT = InlineFont(b=True, sz=14, rFont="Calibri")
_PACKSLIP_LABEL_FONT = InlineFont(b=False, sz=10, rFont="Calibri")
_PACKSLIP_ORDER_COLORS = (
    "0070C0",
    "C65911",
    "548235",
    "7030A0",
    "008080",
    "C00000",
    "1F4E79",
    "833C0C",
    "C00080",
    "7F6000",
    "2E75B6",
    "BF8F00",
)
_PACKSLIP_COLOR_FONTS: dict[str, tuple[InlineFont, InlineFont]] = {}


def _packslip_colored_fonts(hex_color: str) -> tuple[InlineFont, InlineFont]:
    key = str(hex_color or "").strip().upper().replace("#", "")
    if len(key) == 6:
        key = "FF" + key
    cached = _PACKSLIP_COLOR_FONTS.get(key)
    if cached:
        return cached
    color = Color(rgb=key)
    fonts = (
        InlineFont(b=True, sz=14, rFont="Calibri", color=color),
        InlineFont(b=False, sz=10, rFont="Calibri", color=color),
    )
    _PACKSLIP_COLOR_FONTS[key] = fonts
    return fonts


def _customer_first_word(name: str) -> str:
    text = str(name or "").strip()
    return text.split()[0] if text else ""


def _ship_date_mmdd(ship_date: str) -> str:
    text = str(ship_date or "").strip().replace("-", "/")
    if not text:
        return ""
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0].zfill(2)}/{parts[1].zfill(2)}"
    return text


def _style_from_prod_cd(prod_cd: str) -> str:
    return str(prod_cd or "").strip().split()[0].upper() if str(prod_cd or "").strip() else ""


def _rich_join(parts: list) -> CellRichText | str:
    blocks: list[TextBlock] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, TextBlock):
            if part.text:
                blocks.append(part)
            continue
        text = str(part)
        if text:
            blocks.append(TextBlock(_PACKSLIP_LABEL_FONT, text))
    if not blocks:
        return ""
    return CellRichText(*blocks)


def _num_block(value, *, font: InlineFont | None = None) -> TextBlock:
    return TextBlock(font or _PACKSLIP_NUM_FONT, str(value))


def _label_block(value, *, font: InlineFont | None = None) -> TextBlock:
    return TextBlock(font or _PACKSLIP_LABEL_FONT, str(value))


def _format_packslip_order_line_rich(order: dict, *, highlight: str | None = None) -> list[TextBlock]:
    customer = _customer_first_word(
        order.get("abbrev") or order.get("customer_name") or ""
    )
    ship = _ship_date_mmdd(order.get("ship_date") or "")
    num_font, label_font = (
        _packslip_colored_fonts(highlight) if highlight else (_PACKSLIP_NUM_FONT, _PACKSLIP_LABEL_FONT)
    )
    parts = [
        _num_block(order.get("order_id") or "", font=num_font),
        _label_block(" - ", font=label_font),
        _label_block(customer, font=label_font),
        _label_block(" - ", font=label_font),
        _num_block(order.get("qty") or "", font=num_font),
    ]
    if ship:
        parts.extend([_label_block(" - ", font=label_font), _num_block(ship, font=num_font)])
    return parts


def _format_packslip_order_cell(orders: list[dict], highlights: dict[str, str] | None = None) -> CellRichText | str:
    blocks: list[TextBlock] = []
    highlights = highlights or {}
    for idx, order in enumerate(orders or []):
        if not order:
            continue
        if blocks:
            blocks.append(_label_block("\n"))
        oid = str(order.get("order_id") or "").strip()
        blocks.extend(_format_packslip_order_line_rich(order, highlight=highlights.get(oid)))
    return _rich_join(blocks)


def _packslip_cell_line_count(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, CellRichText):
        text = "".join(str(getattr(block, "text", "") or "") for block in value)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return 0
    return text.count("\n") + 1


def _packslip_row_height(line_count: int) -> float:
    if line_count <= 1:
        return 16
    return min(409, 16 * int(line_count) + 6)


def _format_packslip_qty_cell(item: dict) -> CellRichText | str:
    case_qty = int(item.get("Case Qty") or 0)
    sku_boxes = int(item.get("SKU Boxes") or 0)
    quantity = int(item.get("Quantity") or 0)
    box_label = " box = " if sku_boxes == 1 else " boxes = "
    return _rich_join(
        [
            _num_block(case_qty),
            _label_block("/box x "),
            _num_block(sku_boxes),
            _label_block(box_label),
            _num_block(quantity),
            _label_block(" pc"),
        ]
    )


def _format_packslip_pcs_boxes(pcs, boxes) -> CellRichText | str:
    pcs = int(pcs or 0)
    boxes = int(boxes or 0)
    return _rich_join(
        [
            _num_block(pcs),
            _label_block(" ("),
            _num_block(boxes),
            _label_block(" box)"),
        ]
    )


def _packslip_row_sold_stay(need, quantity, case_qty, sku_boxes):
    """Allocate order pcs against this case-pack row.

    If orders exceed this row's qty, sold is ALL and stay is 0.
    Otherwise pull whole boxes: 48 pcs / 12 = 4 box → sold 48 (4 box).
    """
    need = int(need or 0)
    quantity = int(quantity or 0)
    case_qty = int(case_qty or 0)
    sku_boxes = int(sku_boxes or 0)
    if quantity <= 0:
        return "", 0, 0
    if need <= 0:
        return "", _format_packslip_pcs_boxes(quantity, sku_boxes), 0
    if need > quantity:
        return "ALL", 0, quantity
    if case_qty <= 0:
        taken = min(need, quantity)
        stay_pcs = quantity - taken
        if stay_pcs <= 0:
            return "ALL", 0, quantity
        return taken, stay_pcs, taken

    boxes_needed = math.ceil(need / case_qty)
    boxes_take = min(int(boxes_needed), sku_boxes)
    pcs_take = boxes_take * case_qty
    if pcs_take >= quantity:
        return "ALL", 0, quantity
    stay_boxes = sku_boxes - boxes_take
    stay_pcs = quantity - pcs_take
    return (
        _format_packslip_pcs_boxes(pcs_take, boxes_take),
        _format_packslip_pcs_boxes(stay_pcs, stay_boxes),
        pcs_take,
    )


def _merge_packslip_orders(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    keys_order: list[tuple[str, str]] = []
    for row in rows:
        key = (str(row.get("order_id") or ""), str(row.get("customer_name") or ""))
        if not key[0]:
            continue
        if key not in merged:
            merged[key] = {
                "order_id": key[0],
                "customer_name": key[1],
                "abbrev": row.get("abbrev") or _customer_first_word(key[1]),
                "qty": int(row.get("qty") or 0),
                "ship_date": str(row.get("ship_date") or ""),
            }
            keys_order.append(key)
        else:
            merged[key]["qty"] += int(row.get("qty") or 0)
            if not merged[key]["ship_date"] and row.get("ship_date"):
                merged[key]["ship_date"] = str(row.get("ship_date") or "")
    return [merged[key] for key in keys_order]


def _packslip_order_row(customer_name, order_id, order_qty, ship_date) -> dict | None:
    original = str(customer_name or "").strip()
    customer = original
    if original.upper() in {"BURLINGTON COAT FACTORY", "BURLINGTON COAT FACOTORY"}:
        customer = "burlington"
    oid = str(order_id or "").strip()
    qty = int(order_qty or 0)
    if not oid or qty <= 0:
        return None
    return {
        "order_id": oid,
        "customer_name": customer,
        "abbrev": _customer_first_word(original) or _customer_first_word(customer),
        "qty": qty,
        "ship_date": str(ship_date or "").strip(),
    }


def get_packslip_order_groups(full_sku_list, styles=None):
    """Split open orders into Ross/Burlington (big) vs other customers (small).

    Both lists are SKU-level so qty matches the packslip row. Small orders exclude
    Ross/Burlington so a big-order customer never appears in small.
    """
    sku_big: dict[str, list[dict]] = {}
    sku_small: dict[str, list[dict]] = {}
    normalized_skus = sorted({str(sku).strip().upper() for sku in full_sku_list if str(sku).strip()})
    if not normalized_skus:
        return sku_big, sku_small

    conn = None
    cursor = None
    ship_sql = """
        CASE
            WHEN o.ship_dt IS NULL OR o.ship_dt = 0 THEN ''
            ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                 RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                 CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR)
        END
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sku_placeholders = ",".join("?" for _ in normalized_skus)

        cursor.execute(
            f"""
            SELECT
                UPPER(LTRIM(RTRIM(ol.prod_cd))) AS sku,
                LTRIM(RTRIM(ISNULL(c.cus_nm, ''))) AS customer_name,
                ol.ord_num AS order_id,
                CAST(ISNULL(ol.order_qty, 0) AS INT) AS order_qty,
                {ship_sql} AS ship_date
            FROM dbo.ord_log ol
            JOIN dbo.customer c ON ol.cus_id = c.cus_id
            JOIN dbo.orders o ON ol.ord_num = o.ord_num
            WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) IN ({sku_placeholders})
              AND ({_PACKSLIP_BIG_CUSTOMER_SQL})
              AND (
                    (o.INVS_NUM IS NULL OR CAST(o.INVS_NUM AS INT) = 0)
                    OR (o.can_dt IS NULL OR CAST(o.can_dt AS INT) = 0)
              )
              AND ISNULL(ol.order_qty, 0) > 0
            ORDER BY ol.ord_num
            """,
            normalized_skus,
        )
        big_rows: dict[str, list[dict]] = {}
        for row in cursor.fetchall():
            sku = str(row[0] or "").strip().upper()
            parsed = _packslip_order_row(row[1], row[2], row[3], row[4])
            if not sku or not parsed:
                continue
            big_rows.setdefault(sku, []).append(parsed)
        for sku, rows in big_rows.items():
            sku_big[sku] = _merge_packslip_orders(rows)

        cursor.execute(
            f"""
            SELECT
                UPPER(LTRIM(RTRIM(ol.prod_cd))) AS sku,
                LTRIM(RTRIM(ISNULL(c.cus_nm, ''))) AS customer_name,
                ol.ord_num AS order_id,
                CAST(ISNULL(ol.order_qty, 0) AS INT) AS order_qty,
                {ship_sql} AS ship_date
            FROM dbo.ord_log ol
            JOIN dbo.customer c ON ol.cus_id = c.cus_id
            JOIN dbo.orders o ON ol.ord_num = o.ord_num
            WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) IN ({sku_placeholders})
              AND NOT ({_PACKSLIP_BIG_CUSTOMER_SQL})
              AND (o.INVS_NUM IS NULL OR CAST(o.INVS_NUM AS INT) = 0)
              AND (o.can_dt IS NULL OR CAST(o.can_dt AS INT) = 0)
              AND ISNULL(ol.order_qty, 0) > 0
            ORDER BY ol.ord_num
            """,
            normalized_skus,
        )
        small_rows: dict[str, list[dict]] = {}
        for row in cursor.fetchall():
            sku = str(row[0] or "").strip().upper()
            parsed = _packslip_order_row(row[1], row[2], row[3], row[4])
            if not sku or not parsed or _is_packslip_big_customer(parsed["customer_name"]):
                continue
            small_rows.setdefault(sku, []).append(parsed)
        for sku, rows in small_rows.items():
            sku_small[sku] = _merge_packslip_orders(rows)
    except Exception as e:
        current_app.logger.warning(f"Failed to fetch packslip order groups: {e}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return sku_big, sku_small


def get_packslip_open_orders(full_sku_list):
    """All open orders per SKU: not invoiced and not cancelled.

    Same rule as the old small-order list, but Ross/Burlington are included.
    """
    sku_orders: dict[str, list[dict]] = {}
    normalized_skus = sorted({str(sku).strip().upper() for sku in full_sku_list if str(sku).strip()})
    if not normalized_skus:
        return sku_orders

    conn = None
    cursor = None
    ship_sql = """
        CASE
            WHEN o.ship_dt IS NULL OR o.ship_dt = 0 THEN ''
            ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                 RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                 CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR)
        END
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sku_placeholders = ",".join("?" for _ in normalized_skus)
        cursor.execute(
            f"""
            SELECT
                UPPER(LTRIM(RTRIM(ol.prod_cd))) AS sku,
                LTRIM(RTRIM(ISNULL(c.cus_nm, ''))) AS customer_name,
                ol.ord_num AS order_id,
                CAST(ISNULL(ol.order_qty, 0) AS INT) AS order_qty,
                {ship_sql} AS ship_date
            FROM dbo.ord_log ol
            JOIN dbo.customer c ON ol.cus_id = c.cus_id
            JOIN dbo.orders o ON ol.ord_num = o.ord_num
            WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) IN ({sku_placeholders})
              AND (o.INVS_NUM IS NULL OR CAST(o.INVS_NUM AS INT) = 0)
              AND (o.can_dt IS NULL OR CAST(o.can_dt AS INT) = 0)
              AND ISNULL(ol.order_qty, 0) > 0
            ORDER BY ol.ord_num
            """,
            normalized_skus,
        )
        grouped: dict[str, list[dict]] = {}
        for row in cursor.fetchall():
            sku = str(row[0] or "").strip().upper()
            parsed = _packslip_order_row(row[1], row[2], row[3], row[4])
            if not sku or not parsed:
                continue
            grouped.setdefault(sku, []).append(parsed)
        for sku, rows in grouped.items():
            sku_orders[sku] = _merge_packslip_orders(rows)
    except Exception as e:
        current_app.logger.warning(f"Failed to fetch packslip open orders: {e}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return sku_orders


def get_filtered_order_summary_by_skus(full_sku_list):
    """Return pending/active ROSS/BURLINGTON order summary keyed by full SKU."""
    sku_big, _ = get_packslip_order_groups(
        full_sku_list,
        [_style_from_prod_cd(sku) for sku in full_sku_list],
    )
    sku_summary_map = {}
    for sku, orders in sku_big.items():
        sku_summary_map[sku] = {
            "qty_in_order": sum(int(order.get("qty") or 0) for order in orders),
            "customer_name": ", ".join(
                dict.fromkeys(str(order.get("customer_name") or "") for order in orders if order.get("customer_name"))
            ),
            "order_id": ", ".join(str(order.get("order_id") or "") for order in orders if order.get("order_id")),
            "order_qty": ", ".join(str(order.get("qty") or "") for order in orders),
            "ship_date": ", ".join(
                dict.fromkeys(str(order.get("ship_date") or "") for order in orders if order.get("ship_date"))
            ),
        }
    return sku_summary_map

def get_los_angeles_time(dt_str):
    """Convert ISO datetime string to Los Angeles time"""
    if not dt_str:
        return ''
    try:
        # Parse the ISO datetime string (assuming UTC)
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        
        # Los Angeles is UTC-8 (PST) or UTC-7 (PDT)
        # For simplicity, we'll use UTC-8
        la_tz = timezone(timedelta(hours=-8))
        la_time = dt.astimezone(la_tz)
        
        return la_time.strftime('%m/%d/%Y %H:%M')
    except Exception as e:
        # Log error if in Flask context, otherwise just return original
        try:
            current_app.logger.error(f"Error converting time {dt_str}: {e}")
        except RuntimeError:
            # Outside Flask context, just print to console
            print(f"Error converting time {dt_str}: {e}")
        return dt_str

def get_sheets_client():
    """Initialize Google Sheets client with service account"""
    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        current_app.logger.error(f"Error initializing Google Sheets client: {e}")
        return None

def batch_update_column_o(container_numbers):
    """Update column O for multiple containers in batch"""
    try:
        client = get_sheets_client()
        if not client:
            return False
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all data at once (single read)
        all_data = sheet.get_all_values()
        
        # Find rows to update (Container # is in Column A now)
        updates = []
        target_ids = {normalize_container_id(c) for c in container_numbers}
        for row_idx, row in enumerate(all_data):
            if len(row) >= 1:  # Make sure row has at least column A
                container = normalize_container_id(row[0])  # Column A = Container #
                if container in target_ids:
                    # Row index in sheet (1-based, +1 because enumerate is 0-based)
                    sheet_row = row_idx + 1
                    # Update column O with timestamp
                    updates.append({
                        'range': f'O{sheet_row}',  # Column O
                        'values': [[datetime.now().strftime('%m/%d/%Y %H:%M:%S')]]
                    })
        
        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error batch updating Google Sheets: {e}")
        return False

def update_received_dates_in_sheets(container_dates_dict):
    """Update RCD (Received Date) in Google Sheets Column F for landed containers"""
    try:
        client = get_sheets_client()
        if not client:
            return False
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all data at once (single read)
        all_data = sheet.get_all_values()
        
        # Find rows to update (Container # is in Column A)
        updates = []
        for row_idx, row in enumerate(all_data):
            if len(row) >= 1:  # Make sure row has at least column A
                container = row[0]  # Column A (index 0) = Container #
                container_key = normalize_container_id(container)
                if container_key not in container_dates_dict:
                    continue
                existing_rcd = row[5].strip() if len(row) > 5 else ''
                if not existing_rcd:
                    sheet_row = row_idx + 1
                    received_date = container_dates_dict[container_key]
                    updates.append({
                        'range': f'F{sheet_row}',  # Column F = RCD
                        'values': [[received_date]]
                    })
        
        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error updating received dates in Google Sheets: {e}")
        return False

def clear_received_dates_in_sheets(container_numbers: list[str]) -> bool:
    """Clear RCD (column F) for containers reverted to ocean."""
    try:
        client = get_sheets_client()
        if not client:
            return False

        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        all_data = sheet.get_all_values()
        target_keys = {normalize_container_id(c) for c in container_numbers if c}
        updates = []
        for row_idx, row in enumerate(all_data):
            if not row or not row[0]:
                continue
            container_key = normalize_container_id(row[0])
            if container_key not in target_keys:
                continue
            sheet_row = row_idx + 1
            updates.append({
                'range': f'F{sheet_row}',
                'values': [['']],
            })

        if updates:
            sheet.batch_update(updates)
            return True
        return False
    except Exception as e:
        current_app.logger.error(f"Error clearing received dates in Google Sheets: {e}")
        return False

def update_last_updated_for_all_containers(container_list, fetch_timestamp):
    """Update the Last Updated column (T) for all containers with the fetch timestamp"""
    try:
        client = get_sheets_client()
        if not client:
            return False
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all data at once (single read)
        all_data = sheet.get_all_values()
        
        # Find rows to update (Container # is in Column A)
        updates = []
        for row_idx, row in enumerate(all_data):
            if len(row) >= 1:  # Make sure row has at least column A
                container = row[0]  # Column A (index 0) = Container #
                if container in container_list:
                    # Row index in sheet (1-based, +1 because enumerate is 0-based)
                    sheet_row = row_idx + 1
                    
                    # Update column T = Last Updated
                    updates.append({
                        'range': f'T{sheet_row}',  # Column T = Last Updated
                        'values': [[fetch_timestamp]]
                    })
        
        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error updating Last Updated for all containers: {e}")
        return False

def _sheet_col_letter(col_idx_1based: int) -> str:
    letters = ""
    while col_idx_1based > 0:
        col_idx_1based, rem = divmod(col_idx_1based - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def update_shipment_tracking_in_sheets(records: list[dict]) -> int:
    """Write weekly tracking fields onto the container import Google Sheet."""
    if not records:
        return 0

    by_container = {
        normalize_container_id(rec.get("container_id")): rec
        for rec in records
        if normalize_container_id(rec.get("container_id"))
    }
    if not by_container:
        return 0

    try:
        client = get_sheets_client()
        if not client:
            return 0

        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        all_data = sheet.get_all_values()
        if not all_data:
            return 0

        header_map = {
            str(h).strip().lower(): idx
            for idx, h in enumerate(all_data[0])
            if h is not None
        }

        def find_idx(candidates: list[str], fallback: int) -> int:
            for candidate in candidates:
                if candidate in header_map:
                    return header_map[candidate]
            return fallback

        container_idx = find_idx(["fdc container", "container"], 0)
        bol_idx = find_idx(["bol num", "bol"], 2)
        etd_idx = find_idx(["eta1", "etd"], 4)
        rcd_idx = find_idx(["rcd", "deliver date", "delivery date"], 5)
        eta_idx = find_idx(["eta", "auto eta", "dennis eta"], 16)
        voyage_idx = find_idx(["voyage", "vessel no", "vesso no", "vessel"], 18)
        updated_idx = find_idx(["last updated", "last_updated"], 19)

        updates = []
        matched = 0
        stamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")

        for row_idx, row in enumerate(all_data[1:], start=2):
            container_raw = row[container_idx] if len(row) > container_idx else ""
            container_key = normalize_container_id(container_raw)
            if container_key not in by_container:
                continue

            rec = by_container[container_key]
            matched += 1

            def queue_update(col_idx: int, value: str) -> None:
                if not value:
                    return
                updates.append(
                    {
                        "range": f"{_sheet_col_letter(col_idx + 1)}{row_idx}",
                        "values": [[value]],
                    }
                )

            queue_update(bol_idx, str(rec.get("container_no") or "").strip())
            queue_update(etd_idx, str(rec.get("etd") or "").strip())
            queue_update(rcd_idx, str(rec.get("deliver_date") or "").strip())
            queue_update(eta_idx, str(rec.get("eta") or "").strip())
            queue_update(voyage_idx, str(rec.get("vessel_no") or "").strip())
            queue_update(updated_idx, stamp)

        if updates:
            sheet.batch_update(updates)
        return matched
    except Exception as e:
        current_app.logger.error(f"Error updating shipment tracking in Google Sheets: {e}")
        return 0


def update_tracking_data_in_sheets(container_tracking_dict):
    """Update tracking data in Google Sheets for containers with shipment data.
    The function will try to locate header columns by name (case-insensitive)
    and write the fields into the matching columns. If headers cannot be
    found, it falls back to the legacy columns P-T.
    """
    try:
        client = get_sheets_client()
        if not client:
            return False

        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

        # Get all data at once (single read)
        all_data = sheet.get_all_values()

        if not all_data:
            return False

        # Build header lookup from the first row (case-insensitive)
        header_row = all_data[0]
        header_map = {str(h).strip().lower(): idx for idx, h in enumerate(header_row) if h is not None}

        # Helper: convert 1-based column index to A1 letter (1 -> A)
        def idx_to_col(col_idx_1based):
            letters = ''
            while col_idx_1based > 0:
                col_idx_1based, rem = divmod(col_idx_1based - 1, 26)
                letters = chr(65 + rem) + letters
            return letters

        # Helper: find header index by several candidate names, fallback to default 0-based index
        def find_idx(candidates, fallback_0based):
            for c in candidates:
                if c in header_map:
                    return header_map[c]
            return fallback_0based

        # Preferred header keys (lowercase)
        shipping_line_idx = find_idx(['shipping line', 'shipping_line', 'shippingline', 'shipping line name'], 15)  # default P (0-based 15)
        eta_idx = find_idx(['auto eta', 'eta', 'dennis eta'], 16)  # default Q
        ata_idx = find_idx(['ata', 'pod_ata_at'], 17)  # default R
        voyage_idx = find_idx(['voyage', 'voyage number'], 18)  # default S
        last_updated_idx = find_idx(['last updated', 'last_updated', 'written time'], 19)  # default T

        updates = []

        # Iterate all rows and match container (Column A)
        for row_idx, row in enumerate(all_data):
            if not row or len(row) < 1:
                continue
            container = str(row[0]).strip()
            if not container:
                continue

            if container in container_tracking_dict:
                sheet_row = row_idx + 1
                tracking_data = container_tracking_dict.get(container, {})

                shipping_line = tracking_data.get('shipping_line', '')
                if shipping_line == 'Orient Overseas Container Line':
                    shipping_line = 'OOCL'

                # Prepare updates mapping to discovered columns (convert to 1-based for idx_to_col)
                updates.append({
                    'range': f"{idx_to_col(shipping_line_idx+1)}{sheet_row}",
                    'values': [[shipping_line]]
                })

                updates.append({
                    'range': f"{idx_to_col(eta_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('real_eta', '')]]
                })

                updates.append({
                    'range': f"{idx_to_col(ata_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('ata', '')]]
                })

                updates.append({
                    'range': f"{idx_to_col(voyage_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('voyage', '')]]
                })

                updates.append({
                    'range': f"{idx_to_col(last_updated_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('last_updated', '')]]
                })

        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True

        return False

    except Exception as e:
        current_app.logger.error(f"Error updating tracking data in Google Sheets: {e}")
        return False

def add_multiple_containers_to_sheets(container_list):
    """Add multiple new containers to Google Sheets as new rows"""
    try:
        client = get_sheets_client()
        if not client:
            return []
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all existing containers to avoid duplicates
        all_data = sheet.get_all_values()
        existing_containers = set()
        for row in all_data:
            if row and row[0]:  # Column A has container number
                existing_containers.add(normalize_container_id(row[0]))
        
        containers_to_add = []
        for container_num in container_list:
            container_num = normalize_container_id(container_num)
            if not container_num:
                continue
            
            # Check if container already exists
            if container_num in existing_containers:
                continue
            
            containers_to_add.append(container_num)
        
        if not containers_to_add:
            return []
        
        # Find the last row with data to append after
        last_row_with_data = 0
        for i, row in enumerate(all_data):
            if any(cell.strip() for cell in row if cell):
                last_row_with_data = i + 1
        
        
        # Get container files for reading Invoice data and tracking info
        container_files = scan_dropbox_containers()
        
        # Prepare new rows - each container gets a new row with full data
        new_rows = []
        for container_num in containers_to_add:
            # Create row with basic structure
            new_row = [container_num, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '']  # Extend to at least column T
            
            # Extract invoice data if file exists
            if container_num in container_files:
                try:
                    file_path = container_files[container_num]
                    # Read Invoice tab
                    df_invoice = pd.read_excel(file_path, sheet_name='Invoice', header=None)
                    
                    # Check A5 for T-FASHION
                    if len(df_invoice) > 4:  # A5 is row index 4
                        a5_value = str(df_invoice.iloc[4, 0]) if pd.notna(df_invoice.iloc[4, 0]) else ''
                        if 'T-FASHION' in a5_value:
                            new_row[1] = 'TF'  # Column B (index 1) = Company Code
                    
                    # Get container number from A6
                    if len(df_invoice) > 5:  # A6 is row index 5
                        a6_value = str(df_invoice.iloc[5, 0]) if pd.notna(df_invoice.iloc[5, 0]) else ''
                        # Extract container number after "CONTAINER #："
                        match = re.search(r'CONTAINER\s*#[：:]\s*([A-Z0-9]+)', a6_value, re.IGNORECASE)
                        if match:
                            actual_container_number = normalize_container_id(match.group(1))
                            new_row[2] = actual_container_number  # Column C (index 2) = Invoice Container #
                except Exception as e:
                    current_app.logger.error(f"Error reading Invoice for {container_num}: {e}")
                    actual_container_number = None
            
            # Set timestamp in column O and current user in column N (written by)
            new_row[14] = datetime.now().strftime('%m/%d/%Y %H:%M:%S')  # Column O (index 14)
            new_row[13] = str(session.get("username") or "").strip()
            
            new_rows.append(new_row)
        
        # Add all new rows at once using explicit ranges
        if new_rows:
            # Find the next available row (after the last row with data)
            next_row = last_row_with_data + 1
            
            # Write all rows in a single batch update to avoid API quota issues
            if len(new_rows) == 1:
                range_str = f'A{next_row}:T{next_row}'
                sheet.update(range_str, new_rows)
            else:
                # Multiple rows - use range spanning all rows
                range_str = f'A{next_row}:T{next_row + len(new_rows) - 1}'
                sheet.update(range_str, new_rows)
            
            current_app.logger.info(f"Added {len(new_rows)} containers to Google Sheets in a single batch")
            return containers_to_add
            
        return []
        
    except Exception as e:
        current_app.logger.error(f"Error adding containers to Google Sheets: {e}")
        return []

def _dropbox_scan_locations(*, all_years: bool = False):
    """Dropbox folders to scan for container Excels.

    Default: catalog root + latest *年 folder and its month subfolders (boat hub).
    all_years=True: every *年 folder and nested month folders (backfill / historical).
    """
    locations = [DROPBOX_CONTAINER_PATH]
    year_folders = [
        path for path in glob.glob(os.path.join(DROPBOX_CONTAINER_PATH, '*'))
        if os.path.isdir(path) and re.search(r'\d+年', os.path.basename(path))
    ]
    if not year_folders:
        return locations

    def _year_sort_key(path: str) -> int:
        match = re.search(r'(\d+)', os.path.basename(path))
        return int(match.group(1)) if match else 0

    years = (
        sorted(year_folders, key=_year_sort_key)
        if all_years
        else [max(year_folders, key=_year_sort_key)]
    )
    for year_folder in years:
        locations.append(year_folder)
        month_folders = glob.glob(os.path.join(year_folder, '*'))
        locations.extend(path for path in month_folders if os.path.isdir(path))
    return locations


def scan_dropbox_containers(*, all_years: bool = False):
    """Scan Dropbox folder for container Excel files (F, FT, and L prefixes).

    When all_years=True, includes older year/month folders. Newer paths overwrite
    older ones when the same container id appears more than once.
    """
    container_files = {}
    
    try:
        for location in _dropbox_scan_locations(all_years=all_years):
            excel_files = glob.glob(os.path.join(location, '*.xlsx'))
            
            for file_path in excel_files:
                filename = os.path.basename(file_path)
                
                # FT25141.xlsx -> F25141 (never store FT prefix)
                match = re.match(r'^FT(\d+)', filename, re.IGNORECASE)
                if match:
                    f_number = 'F' + match.group(1)
                    container_files[f_number] = file_path
                    continue
                
                # F26119
                match = re.match(r'^F(\d+)', filename, re.IGNORECASE)
                if match:
                    container_files[match.group(0).upper()] = file_path
                    continue

                # L202601 (special / EFAS containers)
                match = re.match(r'^(L\d+)', filename, re.IGNORECASE)
                if match:
                    container_files[match.group(1).upper()] = file_path
        
        return container_files
    
    except Exception as e:
        current_app.logger.error(f"Error scanning Dropbox containers: {e}")
        return {}

def list_terminal49_shipments():
    """List all shipments from Terminal49 API"""
    try:
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get("https://api.terminal49.com/v2/shipments", headers=headers)
        
        # Log the raw response regardless of status code

        
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            current_app.logger.error(f"Terminal49 API error listing shipments: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        current_app.logger.error(f"Error listing Terminal49 shipments: {e}")
        return None

def find_existing_tracking_request(container_number):
    """Find existing tracking request for a container number by checking shipments"""
    try:
        shipments_data = get_terminal49_shipments()
        if not shipments_data or 'data' not in shipments_data:
            return None

        shipments = shipments_data['data']

        for shipment in shipments:
            attributes = shipment.get('attributes', {})
            bill_of_lading = attributes.get('bill_of_lading_number', '')

            # Check if this shipment matches our container number
            if bill_of_lading == container_number:
                # Get the tracking request relationship if available
                relationships = shipment.get('relationships', {})
                tracking_request = relationships.get('tracking_request', {})
                tracking_request_data = tracking_request.get('data', {})

                if tracking_request_data:
                    tracking_id = tracking_request_data.get('id')
                    return tracking_id

        return None

    except Exception as e:
        current_app.logger.error(f"Error finding existing tracking request for {container_number}: {e}")
        return None

def create_terminal49_tracking_request(container_number, scac=None):
    """Create a tracking request with Terminal49 and return the tracking ID"""
    try:
        import threading
        import queue
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        # Send two concurrent requests - one for OOCL and one for EGLV
        scac_codes = ["OOLU", "EGLV"]
        results_queue = queue.Queue()
        
        def send_request(scac_code):
            """Send a single tracking request for a SCAC code"""
            try:
                payload = {
                    "data": {
                        "attributes": {
                            "request_type": "container",
                            "request_number": container_number,
                            "ref_numbers": [],  # Can be populated with PO/BL numbers if available
                            "shipment_tags": [],
                            "scac": scac_code
                        },
                        "type": "tracking_request"
                    }
                }

                # Retry logic for 500 errors
                max_retries = 2
                for retry in range(max_retries + 1):
                    try:
                        response = requests.post(TERMINAL49_API_URL, headers=headers, json=payload, timeout=30)

                        if response.status_code == 201:
                            data = response.json()
                            tracking_id = data.get('data', {}).get('id')
                            results_queue.put(('success', scac_code, tracking_id))
                            return
                        elif response.status_code == 422:
                            # Check if it's a duplicate error with existing tracking request
                            error_data = response.json()
                            errors = error_data.get('errors', [])

                            for error in errors:
                                if error.get('code') == 'duplicate':
                                    # Extract the existing tracking request ID from meta
                                    meta = error.get('meta', {})
                                    existing_tracking_id = meta.get('tracking_request_id')
                                    if existing_tracking_id:
                                        results_queue.put(('duplicate', scac_code, existing_tracking_id))
                                        return

                            # Check if it's a SCAC-related error
                            scac_error = any('scac' in str(error.get('source', {}).get('pointer', '')) for error in errors)
                            container_error = any('container' in str(error.get('detail', '')).lower() or 'number' in str(error.get('detail', '')).lower() for error in errors)

                            if scac_error or container_error:
                                results_queue.put(('scac_error', scac_code, None))
                                return
                            else:
                                # Other validation error
                                results_queue.put(('validation_error', scac_code, None))
                                return
                        elif response.status_code == 404:
                            results_queue.put(('not_found', scac_code, None))
                            return
                        elif response.status_code == 500:
                            # Server errors may be temporary - use more aggressive retry
                            server_max_retries = 3
                            if retry < server_max_retries:
                                current_app.logger.warning(f"Terminal49 server error (500) for {container_number} with SCAC {scac_code}, retrying ({retry + 1}/{server_max_retries})")
                                import time
                                time.sleep(3)  # Longer wait for server errors
                                continue
                            else:
                                results_queue.put(('server_error', scac_code, None))
                                return
                        elif response.status_code == 429:
                            # Rate limited
                            current_app.logger.warning(f"Rate limited by Terminal49 for {container_number}, waiting before retry")
                            import time
                            time.sleep(5)
                            if retry < max_retries:
                                continue
                            else:
                                results_queue.put(('rate_limited', scac_code, None))
                                return
                        else:
                            results_queue.put(('api_error', scac_code, response.status_code))
                            return

                    except requests.exceptions.Timeout:
                        if retry < max_retries:
                            current_app.logger.warning(f"Timeout error for {container_number} with SCAC {scac_code}, retrying ({retry + 1}/{max_retries})")
                            continue
                        else:
                            results_queue.put(('timeout', scac_code, None))
                            return
                    except requests.exceptions.RequestException as e:
                        results_queue.put(('request_error', scac_code, str(e)))
                        return
                        
            except Exception as e:
                results_queue.put(('exception', scac_code, str(e)))
        
        # Start both requests concurrently
        threads = []
        for scac_code in scac_codes:
            thread = threading.Thread(target=send_request, args=(scac_code,))
            threads.append(thread)
            thread.start()
        
        # Wait for both threads to complete
        for thread in threads:
            thread.join()
        
        # Process results
        results = []
        while not results_queue.empty():
            results.append(results_queue.get())
        
        # Look for successful results first
        for result_type, scac_code, data in results:
            if result_type == 'success':
                return data
            elif result_type == 'duplicate':
                return data
        
        # If no success, log the failures
        for result_type, scac_code, data in results:
            if result_type == 'scac_error':
                current_app.logger.warning(f"SCAC {scac_code} not recognized for {container_number}")
            elif result_type == 'not_found':
                current_app.logger.warning(f"Container {container_number} not found for SCAC {scac_code}")
            elif result_type == 'validation_error':
                current_app.logger.error(f"Terminal49 validation error for {container_number} with SCAC {scac_code}")
            elif result_type == 'server_error':
                current_app.logger.error(f"Terminal49 server error for {container_number} with SCAC {scac_code}")
            elif result_type == 'rate_limited':
                current_app.logger.warning(f"Rate limited by Terminal49 for {container_number}")
            elif result_type == 'timeout':
                current_app.logger.error(f"Timeout error for {container_number} with SCAC {scac_code}")
            elif result_type == 'request_error':
                current_app.logger.error(f"Request error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'api_error':
                current_app.logger.error(f"Terminal49 API error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'exception':
                current_app.logger.error(f"Exception for {container_number} with SCAC {scac_code}: {data}")
        
        # If we get here, all requests failed
        current_app.logger.error(f"All SCAC options failed for container {container_number}")
        return None

        # If we get here, all SCACs failed
        current_app.logger.error(f"All SCAC options failed for container {container_number}")
        return None
            
    except Exception as e:
        current_app.logger.error(f"Error creating Terminal49 tracking request for {container_number}: {e}")
        return None


def create_terminal49_tracking_request(container_number, scac=None):
    """Create a tracking request with Terminal49 and return the tracking ID"""
    try:
        import queue
        import threading
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        # Send two concurrent requests - one for OOCL and one for EGLV
        scac_codes = ["OOLU", "EGLV"]
        results_queue = queue.Queue()
        
        def send_request(scac_code):
            """Send a single tracking request for a SCAC code"""
            try:
                payload = {
                    "data": {
                        "attributes": {
                            "request_type": "container",
                            "request_number": container_number,
                            "ref_numbers": [],
                            "shipment_tags": [],
                            "scac": scac_code
                        },
                        "type": "tracking_request"
                    }
                }

                # Retry logic for 500 errors
                max_retries = 2
                for retry in range(max_retries + 1):
                    try:
                        response = requests.post(TERMINAL49_API_URL, headers=headers, json=payload, timeout=30)

                        if response.status_code == 201:
                            data = response.json()
                            tracking_id = data.get('data', {}).get('id')
                            results_queue.put(('success', scac_code, tracking_id))
                            return
                        elif response.status_code == 422:
                            error_data = response.json()
                            errors = error_data.get('errors', [])
                            for error in errors:
                                if 'duplicate' in str(error).lower():
                                    tracking_id = error.get('detail', '').split('tracking_request/')[-1].split(' ')[0] if 'tracking_request/' in str(error) else None
                                    if tracking_id:
                                        results_queue.put(('duplicate', scac_code, tracking_id))
                                        return
                            results_queue.put(('validation_error', scac_code, str(errors)))
                        elif response.status_code == 404:
                            results_queue.put(('not_found', scac_code, f'Container {container_number} not found for SCAC {scac_code}'))
                        elif response.status_code == 500:
                            if retry < max_retries:
                                import time
                                time.sleep(2 ** retry)
                                continue
                            results_queue.put(('server_error', scac_code, f'Server error (attempt {retry + 1}/{max_retries + 1})'))
                        elif response.status_code == 429:
                            wait_time = int(response.headers.get('Retry-After', 60))
                            if retry < max_retries:
                                import time
                                time.sleep(min(wait_time, 5))
                                continue
                            results_queue.put(('rate_limited', scac_code, f'Rate limited, retry after {wait_time}s'))
                        else:
                            results_queue.put(('api_error', scac_code, f'API error: {response.status_code} - {response.text}'))

                    except requests.exceptions.Timeout:
                        if retry < max_retries:
                            import time
                            time.sleep(2 ** retry)
                            continue
                        results_queue.put(('timeout', scac_code, 'Request timeout'))
                    except requests.exceptions.RequestException as e:
                        results_queue.put(('request_error', scac_code, str(e)))
                        
            except Exception as e:
                results_queue.put(('exception', scac_code, str(e)))
        
        # Start both requests concurrently
        threads = []
        for scac_code in scac_codes:
            thread = threading.Thread(target=send_request, args=(scac_code,))
            threads.append(thread)
            thread.start()
        
        # Wait for both threads to complete
        for thread in threads:
            thread.join()
        
        # Process results
        results = []
        while not results_queue.empty():
            results.append(results_queue.get())
        
        # Look for successful results first
        for result_type, scac_code, data in results:
            if result_type == 'success':
                return data
            elif result_type == 'duplicate':
                return data
        
        # If no success, log the failures
        for result_type, scac_code, data in results:
            if result_type == 'scac_error':
                current_app.logger.warning(f"SCAC {scac_code} not recognized for {container_number}")
            elif result_type == 'not_found':
                current_app.logger.warning(f"Container {container_number} not found for SCAC {scac_code}")
            elif result_type == 'validation_error':
                current_app.logger.error(f"Terminal49 validation error for {container_number} with SCAC {scac_code}")
            elif result_type == 'server_error':
                current_app.logger.error(f"Terminal49 server error for {container_number} with SCAC {scac_code}")
            elif result_type == 'rate_limited':
                current_app.logger.warning(f"Rate limited by Terminal49 for {container_number}")
            elif result_type == 'timeout':
                current_app.logger.error(f"Timeout error for {container_number} with SCAC {scac_code}")
            elif result_type == 'request_error':
                current_app.logger.error(f"Request error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'api_error':
                current_app.logger.error(f"Terminal49 API error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'exception':
                current_app.logger.error(f"Exception for {container_number} with SCAC {scac_code}: {data}")
        
        # If we get here, all requests failed
        current_app.logger.error(f"All SCAC options failed for container {container_number}")
        return None
            
    except Exception as e:
        current_app.logger.error(f"Error creating Terminal49 tracking request for {container_number}: {e}")
        return None

def _container_step3_columns():
    return [
        'Item#', 'WHS#', 'Descrip', 'Unit', 'OrdSize', 'MinStk', 'Price Type', 'Price1', 'Price2', 'Price3',
        'Price4', 'Price5', 'QtyPrice1', 'QtyPrice2', 'QtyPrice3', 'QtyPrice4', 'QtyPrice5', 'QtyPrice6',
        'Range1', 'Range2', 'Range3', 'Range4', 'Range5', 'Range6', 'Range7', 'CreateDate', 'Note1', 'Note2',
        'Item Def. Bin Location', 'Warehouse Bin Location', 'Product Year', 'Comp', 'Class#', 'Vend#',
        'Vend Item#', 'Image Name', 'Unit/Case', 'Box/Case', 'Case Vol', 'Unit Vol', 'Sales Cost', 'Discount',
        'CommRt', 'Taxable', 'Stkable', 'UPC Code', 'Updt Date', 'Updt By', 'Dept#', 'Case Wt', 'Unit Wt',
        'OEM#', 'Alt.Item#', 'Lead Date', 'Color', 'Size', 'In Stock', 'Last Rcv Q', 'Last Rcv Dat',
        'Base Cost', 'Duty', 'Handling fee', 'Ship Fee', 'Avg.Cost', 'Last Sale Da', 'Last Order', 'Order Date',
        'So Qty', 'PO Qty', 'l2yrsqty', 'lyrsqty', 'ThisYrSQty', 'Descrip2', 'Descrip3', 'Notes Line 1',
        'Notes Line 2', 'Notes Line 3', 'Notes Line 4', 'Notes Line 5', 'Notes Line 6', 'Notes Line 7',
        'Notes Line 8', 'Case Length', 'Case Height', 'Case Width', 'Unit Length', 'Unit Height', 'Unit Width',
        'Division', 'Case/Pallet', 'Catalog Page', 'Status', 'Default Unit', 'Release', 'Last Sale Qty',
        'Allocate Qty', 'More Notes 1', 'More Notes 2', 'More Notes 3', 'More Notes 4', 'More Notes 5',
        'More Notes 6', 'More Notes 7', 'More Notes 8',
    ]


def _preview_row_from_line(row, container_number: str, packing_dims: dict | None = None) -> dict:
    style = str(row['STYLE NO']).strip()
    color_raw = str(row['COLOR_RAW']).strip()
    color = normalize_color_to_abbrev(color_raw)
    case_len, case_wi, case_hi = lookup_case_dimensions(packing_dims or {}, style, color)
    return {
        'style': style,
        'color': color,
        'color_original': color_raw,
        'color_changed': color_raw.upper() != color.upper(),
        'item_num': f"{style} {color}",
        'descrip': container_number,
        'sales_cost': _safe_float(row['OMS']),
        'retail_prs': _safe_float(row['Price1']),
        'po_qty': _safe_float(row['QUANTITY']),
        'class_cd': 'L1',
        'unit_case': _safe_float(row['PCS /CTN']),
        'box_case': 1.0,
        'case_wt': _safe_float(row['WT/CTN (LBS)']),
        'unit_wt': _safe_float(row['WT/PCS (LBS)']),
        'unit_len': _safe_float(row['SIZE   (L)IN'], default=None) if pd.notna(row['SIZE   (L)IN']) else None,
        'unit_wi': _safe_float(row['SIZE   (W)IN'], default=None) if pd.notna(row['SIZE   (W)IN']) else None,
        'unit_hi': _safe_float(row['SIZE   (H)IN'], default=None) if pd.notna(row['SIZE   (H)IN']) else None,
        'case_len': case_len,
        'case_wi': case_wi,
        'case_hi': case_hi,
    }


def _build_container_preview(
    df_f24213, container_number: str, packing_dims: dict | None = None
) -> list[dict]:
    return [
        _preview_row_from_line(row, container_number, packing_dims)
        for _, row in df_f24213.iterrows()
    ]


def save_container_csvs(container_number: str, preview_rows: list[dict]) -> dict:
    """Write styles + PO CSV files from confirmed preview rows."""
    step3_columns = _container_step3_columns()
    step3_data = {
        'Item#': [r['item_num'] for r in preview_rows],
        'WHS#': ['01'] * len(preview_rows),
        'Descrip': [r['descrip'] for r in preview_rows],
        'Alt.Item#': [r['style'] for r in preview_rows],
        'Color': [r['color'] for r in preview_rows],
        'Class#': [r.get('class_cd', 'L1') for r in preview_rows],
        'Unit/Case': [r['unit_case'] for r in preview_rows],
        'Box/Case': [r.get('box_case', 1) for r in preview_rows],
        'Sales Cost': [r['sales_cost'] for r in preview_rows],
        'Case Wt': [r['case_wt'] for r in preview_rows],
        'Unit Wt': [r['unit_wt'] for r in preview_rows],
        'Unit Length': [r.get('unit_len') or '' for r in preview_rows],
        'Unit Width': [r.get('unit_wi') or '' for r in preview_rows],
        'Unit Height': [r.get('unit_hi') or '' for r in preview_rows],
        'Case Length': [r.get('case_len') if r.get('case_len') is not None else '' for r in preview_rows],
        'Case Width': [r.get('case_wi') if r.get('case_wi') is not None else '' for r in preview_rows],
        'Case Height': [r.get('case_hi') if r.get('case_hi') is not None else '' for r in preview_rows],
        'Price1': [r['retail_prs'] for r in preview_rows],
        'Base Cost': [r['sales_cost'] for r in preview_rows],
        'Avg.Cost': [r['sales_cost'] for r in preview_rows],
    }
    df_step3 = pd.DataFrame(step3_data)
    for col in step3_columns:
        if col not in df_step3.columns:
            df_step3[col] = ''
    df_step3 = df_step3[step3_columns]

    step4_columns = ['P/O#', 'Item#', 'W#', 'P/O Qty', 'Price', 'Receive Qty', 'Receive Price', 'UPC']
    step4_data = {
        'P/O#': [container_number] * len(preview_rows),
        'Item#': [r['item_num'] for r in preview_rows],
        'W#': ['01'] * len(preview_rows),
        'P/O Qty': [r['po_qty'] for r in preview_rows],
        'Price': [r['sales_cost'] for r in preview_rows],
    }
    df_step4 = pd.DataFrame(step4_data)
    for col in step4_columns:
        if col not in df_step4.columns:
            df_step4[col] = ''
    df_step4 = df_step4[step4_columns]

    df_step3, df_step4 = _trim_blank_export_rows(df_step3, df_step4)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    step3_filename = f"{container_number}-styles.csv"
    step4_filename = f"{container_number}-PO.csv"
    df_step3.to_csv(os.path.join(OUTPUT_DIR, step3_filename), index=False)
    df_step4.to_csv(os.path.join(OUTPUT_DIR, step4_filename), index=False)
    return {'styles_file': step3_filename, 'po_file': step4_filename}


def process_container_file(file_path, container_number):
    """Process a container Excel file and generate CSV files + preview rows."""
    try:
        df_header = pd.read_excel(file_path, sheet_name='System File', nrows=2, header=None)
        df_f24213 = pd.read_excel(file_path, sheet_name='System File', header=2)

        df_f24213.columns = df_f24213.columns.str.replace('\n', ' ').str.strip()
        df_f24213 = _filter_system_file_line_items(df_f24213)
        df_f24213['WT/PCS (LBS)'] = pd.to_numeric(df_f24213['WT/PCS (LBS)'], errors='coerce').round(2)
        df_f24213 = df_f24213.applymap(
            lambda x: re.sub(r'\s+', ' ', str(x)) if isinstance(x, str) else x
        )
        df_f24213['COLOR_RAW'] = df_f24213['COLOR'].astype(str).str.strip()
        df_f24213['COLOR'] = df_f24213['COLOR_RAW'].apply(normalize_color_to_abbrev)

        packing_dims = extract_packing_list_case_dimensions(file_path) or {}
        if packing_dims:
            current_app.logger.info(
                "Loaded masterbox dims for %s SKU(s) from packing list in %s",
                len(packing_dims),
                container_number,
            )
        else:
            current_app.logger.warning(
                "No packing-list masterbox dims found for %s; Case Length/Width/Height will be blank",
                container_number,
            )

        preview = _build_container_preview(df_f24213, container_number, packing_dims)
        if not preview:
            return {
                'success': False,
                'error': (
                    f'No line items found in System File for {container_number}. '
                    'Check STYLE NO / OMS / RMB / QUANTITY columns.'
                ),
            }
        file_info = save_container_csvs(container_number, preview)

        return {
            'success': True,
            'preview': preview,
            'styles_file': file_info['styles_file'],
            'po_file': file_info['po_file'],
        }

    except Exception as e:
        current_app.logger.error(f"Error processing container {container_number}: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def _clean_preview_import_rows(rows: list[dict], container_number: str) -> list[dict]:
    normalized = normalize_container_id(container_number)
    cleaned_rows = []
    for row in rows:
        style = str(row.get('style', '')).strip().upper()
        color = normalize_color_to_abbrev(str(row.get('color', '')).strip())
        item_num = str(row.get('item_num', '')).strip().upper()
        if not item_num:
            item_num = f"{style} {color}".strip()
        if not style and item_num:
            parts = item_num.split(' ', 1)
            style = parts[0]
            if len(parts) > 1 and not color:
                color = normalize_color_to_abbrev(parts[1])
        cleaned_rows.append({
            **row,
            'style': style,
            'color': color,
            'item_num': item_num,
            'descrip': str(row.get('descrip', normalized)).strip().upper() or normalized,
            'sales_cost': float(row.get('sales_cost', 0) or 0),
            'retail_prs': float(row.get('retail_prs', 0) or 0),
            'po_qty': float(row.get('po_qty', 0) or 0),
            'class_cd': str(row.get('class_cd', 'L1')).strip() or 'L1',
            'unit_case': float(row.get('unit_case', 0) or 0),
            'box_case': float(row.get('box_case', 1) or 1),
            'case_wt': float(row.get('case_wt', 0) or 0),
            'unit_wt': float(row.get('unit_wt', 0) or 0),
            'unit_len': row.get('unit_len'),
            'unit_wi': row.get('unit_wi'),
            'unit_hi': row.get('unit_hi'),
            'case_len': row.get('case_len'),
            'case_wi': row.get('case_wi'),
            'case_hi': row.get('case_hi'),
        })
    return cleaned_rows


def _run_container_db_import(
    container_number: str,
    preview_rows: list[dict],
    current_user: str,
    *,
    vendor_id: str | None = None,
) -> dict:
    """Import styles to FDC+EFAS, then PO to FDC only."""
    normalized = normalize_container_id(container_number)
    cleaned_rows = _clean_preview_import_rows(preview_rows, normalized)
    file_info = save_container_csvs(normalized, cleaned_rows)
    batch_update_column_o([normalized])
    styles_result = transfer_container_styles_to_oms(normalized, adjusted_by=current_user)
    po_result = transfer_container_po_to_fdc(normalized, vendor_id=vendor_id)
    success = styles_result.success and po_result.success
    if po_result.skipped:
        po_note = "PO already in FDC (skipped)"
    elif po_result.repaired:
        po_note = f"PO synced {po_result.repaired_lines} row(s) in FDC"
    elif po_result.inserted_lines:
        po_note = f"PO {len(po_result.inserted_lines)} line(s) → FDC (vendor {po_result.vendor_id})"
    else:
        po_note = "PO not imported"
    message = (
        f"Synced {styles_result.total_skus} SKU(s): "
        f"FDC {len(styles_result.fdc.inserted)} inserted / {len(styles_result.fdc.updated)} updated, "
        f"EFAS {len(styles_result.efas.inserted)} inserted / {len(styles_result.efas.updated)} updated. "
        f"{po_note}."
    )
    return {
        'success': success,
        'message': message,
        'files': file_info,
        'styles': styles_result.to_dict(),
        'po': po_result.to_dict(),
    }


@boat_service_bp.route('/boat', methods=['GET'])
@login_required
def boat_service():
    current_user = session['username']
    current_app.logger.info(f"***{current_user}***__@@@checked container hub@@@")
    return render_template(
        'boat.html',
        current_user=current_user,
        page_title='Container Hub',
        google_sheet_url=GOOGLE_SHEET_EDIT_URL,
    )


@boat_service_bp.route('/boat/data', methods=['GET'])
def boat_data():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        force_refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        container_files = scan_dropbox_containers()

        try:
            df, sheet_info = load_sheet_dataframe(force_refresh=force_refresh)
        except Exception as e:
            current_app.logger.error(f"Error fetching sheet for boat: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

        cache_key = build_boat_data_cache_key(
            sheet_hash=sheet_info['content_hash'],
            dropbox_keys=list(container_files.keys()),
        )
        if not force_refresh:
            cached_payload = load_cached_boat_data(cache_key)
            if cached_payload:
                _fill_missing_rcd(cached_payload.get("data") or [])
                return jsonify(cached_payload)

        try:
            current_containers = {
                normalize_container_id(c)
                for c in df['FDC container'].dropna().astype(str).str.strip()
            }
            new_containers = {
                normalize_container_id(c)
                for c in container_files.keys()
            } - current_containers
            if new_containers:
                add_multiple_containers_to_sheets(sorted(new_containers))
                df, sheet_info = load_sheet_dataframe(force_refresh=True)
                cache_key = build_boat_data_cache_key(
                    sheet_hash=sheet_info['content_hash'],
                    dropbox_keys=list(container_files.keys()),
                )
        except Exception as e:
            current_app.logger.error(f"Error syncing containers to sheet: {e}")

        data_rows = _build_container_hub_rows(df, container_files)
        sheet_ts = sheet_info.get('sheet_timestamp') or sheet_info.get('cached_at') or ''
        if sheet_info.get('source') == 'cache':
            last_modified = f"{sheet_ts} (local cache)"
        else:
            last_modified = f"{sheet_ts} (updated)"

        payload = {
            'success': True,
            'data': data_rows,
            'last_modified': last_modified,
            'from_cache': False,
            'counts': {
                'total': len(data_rows),
                'new': sum(1 for r in data_rows if r['action_mode'] == 'new'),
                'ocean': sum(1 for r in data_rows if r['action_mode'] == 'ocean'),
                'landed': sum(1 for r in data_rows if r['action_mode'] == 'landed'),
                'special': sum(1 for r in data_rows if r.get('is_special')),
            },
        }
        save_cached_boat_data(cache_key, payload)
        return jsonify(payload)
    except Exception as e:
        current_app.logger.error(f"Error building container data: {e}")
        current_app.logger.exception("Full traceback:")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/update_msrp/<container_number>', methods=['POST'])
def update_container_msrp(container_number):
    """Push Price1 from the original Dropbox file to FDC + EFAS (RETAIL_PRS only)."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized = normalize_container_id(container_number)
    current_user = session.get('username', 'unknown')
    try:
        result = _run_container_msrp_update(normalized, current_user)
        status = 200 if result.get('success') else 400
        if result.get('success'):
            current_app.logger.info(
                "User %s updated MSRP for %s: %s",
                current_user,
                normalized,
                result.get('message'),
            )
        return jsonify(result), status
    except Exception as e:
        current_app.logger.error(f"Error updating MSRP for {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/shipment_tracking/upload', methods=['POST'])
def upload_shipment_tracking():
    """Upload weekly shipment tracking workbook (2026 sheet) to MySQL + Google Sheet."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'success': False, 'error': 'Choose a tracking Excel file to upload.'}), 400

    filename = upload.filename
    if not filename.lower().endswith(('.xls', '.xlsx', '.xlsm')):
        return jsonify({'success': False, 'error': 'Only .xls, .xlsx, and .xlsm files are supported.'}), 400

    current_user = session.get('username', 'unknown')
    temp_path = ""
    try:
        suffix = os.path.splitext(filename)[1] or ".xlsx"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            upload.save(tmp.name)
            temp_path = tmp.name

        result = process_shipment_tracking_upload(
            temp_path,
            original_filename=filename,
            uploaded_by=current_user,
        )
        matched_rows = update_shipment_tracking_in_sheets(result["rows"])
        meta = save_tracking_meta(
            original_filename=filename,
            sheet_name=result["sheet_name"],
            row_count=len(result["rows"]),
            uploaded_by=current_user,
            matched_sheet_rows=matched_rows,
        )
        invalidate_boat_data_response_cache()
        load_sheet_dataframe(force_refresh=True)

        message = (
            f"Imported {result['mysql_saved']} container(s) from sheet "
            f"'{result['sheet_name']}' and updated {matched_rows} Google Sheet row(s)."
        )
        current_app.logger.info("User %s uploaded shipment tracking %s: %s", current_user, filename, message)
        return jsonify(
            {
                "success": True,
                "message": message,
                "meta": meta,
                "sheet_name": result["sheet_name"],
                "row_count": len(result["rows"]),
                "matched_sheet_rows": matched_rows,
            }
        )
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error uploading shipment tracking file: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if temp_path and os.path.isfile(temp_path):
            os.remove(temp_path)


@boat_service_bp.route('/boat/shipment_tracking/meta', methods=['GET'])
def shipment_tracking_meta():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    meta = load_tracking_meta()
    meta["has_file"] = has_saved_tracking_file()
    return jsonify({'success': True, 'meta': meta})


@boat_service_bp.route('/boat/shipment_tracking/view', methods=['GET'])
def shipment_tracking_view():
    """Return parsed 2026-sheet rows from the last uploaded tracking file."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        sheet_name, rows = load_saved_tracking_rows()
        if not rows:
            return jsonify({'success': False, 'error': 'No uploaded tracking file found.'}), 404
        meta = load_tracking_meta()
        return jsonify(
            {
                'success': True,
                'sheet_name': sheet_name,
                'rows': rows,
                'meta': meta,
            }
        )
    except Exception as e:
        current_app.logger.error(f"Error loading shipment tracking view: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/shipment_tracking/resync', methods=['POST'])
def resync_shipment_tracking():
    """Re-parse the last uploaded tracking file and sync MySQL + Google Sheet."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    current_user = session.get('username', 'unknown')
    try:
        meta = load_tracking_meta()
        result = reprocess_saved_shipment_tracking(uploaded_by=current_user)
        matched_rows = update_shipment_tracking_in_sheets(result["rows"])
        meta = save_tracking_meta(
            original_filename=meta.get("original_filename") or "",
            sheet_name=result["sheet_name"],
            row_count=len(result["rows"]),
            uploaded_by=current_user,
            matched_sheet_rows=matched_rows,
        )
        invalidate_boat_data_response_cache()
        load_sheet_dataframe(force_refresh=True)
        message = (
            f"Re-synced {result['mysql_saved']} container(s) from cached file "
            f"and updated {matched_rows} Google Sheet row(s)."
        )
        return jsonify(
            {
                "success": True,
                "message": message,
                "meta": meta,
                "row_count": len(result["rows"]),
                "matched_sheet_rows": matched_rows,
            }
        )
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error re-syncing shipment tracking: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/delete_po/<container_number>', methods=['POST'])
def delete_container_po(container_number):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized = normalize_container_id(container_number)
    current_user = session.get('username', 'unknown')
    try:
        result = delete_container_po_from_fdc(normalized)
        if result.get('skipped'):
            message = f"No PO found in OMS for {normalized}"
        else:
            message = (
                f"Deleted PO {normalized} from OMS "
                f"(bin_pur {result.get('bin_pur_deleted', 0)}, "
                f"plog {result.get('plog_deleted', 0)}, "
                f"pur_ord {result.get('pur_ord_deleted', 0)})"
            )
        current_app.logger.info("User %s deleted PO %s: %s", current_user, normalized, message)
        status = 200 if result.get('success') else 400
        return jsonify({
            'success': bool(result.get('success')),
            'message': message,
            'result': result,
        }), status
    except Exception as e:
        current_app.logger.error(f"Error deleting PO for {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/mark_landed/<container_number>', methods=['POST'])
def mark_container_landed(container_number):
    """Receive container PO at 827 and sync Google Sheet RCD."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized = normalize_container_id(container_number)
    current_user = session.get('username', 'unknown')
    try:
        result = receive_container_po_at_827(normalized)
        if not result.get('success'):
            message = result.get('errors', ['Failed to mark landed'])[0]
            return jsonify({'success': False, 'error': message, 'result': result}), 400

        received_stamp = datetime.now()
        received_date = received_stamp.strftime("%m/%d/%Y")
        if not result.get('already_landed'):
            try:
                update_received_dates_in_sheets({normalized: received_date})
            except Exception as sheet_exc:
                current_app.logger.warning(
                    "Marked %s landed but sheet RCD update failed: %s",
                    normalized,
                    sheet_exc,
                )
            try:
                _stamp_container_receive_time(normalized, received_stamp, "mark_landed")
            except Exception:
                pass

        if result.get('already_landed'):
            message = f"{normalized} is already landed in OMS."
        else:
            message = (
                f"{normalized} marked landed at 827 — "
                f"{result.get('lines_received', 0)} line(s), "
                f"{int(result.get('qty_received', 0)):,} unit(s) received."
            )
        current_app.logger.info(
            f"***{current_user}***__@@@marked landed@@@ "
            f"https://www.fdcef.com/boat FDC_container={normalized} {message}"
        )
        try:
            _refresh_cached_hub_row(
                normalized,
                rcd_override=None if result.get('already_landed') else received_date,
            )
        except Exception as row_exc:
            current_app.logger.warning(
                "Marked %s landed but hub row refresh failed: %s",
                normalized,
                row_exc,
            )
        return jsonify({'success': True, 'message': message, 'result': result})
    except Exception as e:
        current_app.logger.error(f"Error marking {normalized} landed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/unland/<container_number>', methods=['POST'])
def unland_container(container_number):
    """Revert landed container to ocean (zero RCVD_QTY, restore on-order stock)."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized = normalize_container_id(container_number)
    current_user = session.get('username', 'unknown')
    try:
        result = revert_container_to_ocean(normalized)
        if not result.get('success'):
            message = result.get('errors', ['Failed to un-land'])[0]
            return jsonify({'success': False, 'error': message, 'result': result}), 400

        if not result.get('already_ocean'):
            try:
                clear_received_dates_in_sheets([normalized])
            except Exception as sheet_exc:
                current_app.logger.warning(
                    "Un-landed %s but sheet RCD clear failed: %s",
                    normalized,
                    sheet_exc,
                )
            try:
                _clear_container_receive_time(normalized)
            except Exception:
                pass

        if result.get('already_ocean'):
            message = f"{normalized} is already ocean in OMS."
        else:
            message = (
                f"{normalized} moved back to ocean — "
                f"{result.get('lines_reverted', 0)} line(s), "
                f"{int(result.get('qty_reverted', 0)):,} unit(s) unreceived."
            )
        current_app.logger.info("User %s un-landed container %s: %s", current_user, normalized, message)
        return jsonify({'success': True, 'message': message, 'result': result})
    except Exception as e:
        current_app.logger.error(f"Error un-landing {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/process/<container_number>', methods=['POST'])
def process_container(container_number):
    """Process a specific container and generate CSV files"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    current_user = session.get('username', 'unknown')
    
    try:
        container_files = scan_dropbox_containers()
        normalized = normalize_container_id(container_number)

        if normalized not in container_files:
            return jsonify({'error': f'Container {normalized} not found in Dropbox'}), 404

        result = process_container_file(container_files[normalized], normalized)
        
        if result['success']:
            preview = _sanitize_for_json(result.get('preview') or [])
            return jsonify({
                'success': True,
                'message': (
                    f'Processed {normalized} — review styles, then confirm import '
                    '(styles → FDC + EFAS, PO → FDC only)'
                ),
                'container': normalized,
                'files': {
                    'styles': result['styles_file'],
                    'po': result['po_file'],
                },
                'preview': preview,
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            }), 500
    
    except Exception as e:
        current_app.logger.error(f"Error processing container {container_number}: {e}")
        return jsonify({'error': str(e)}), 500


def _preview_from_styles_csv(container_number: str) -> list[dict]:
    normalized = normalize_container_id(container_number)
    csv_path = os.path.join(OUTPUT_DIR, f"{normalized}-styles.csv")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Styles CSV not found for {normalized}")

    df = pd.read_csv(csv_path)
    preview = []
    for _, record in df.iterrows():
        style = str(record.get('Alt.Item#', '')).strip()
        color_raw = str(record.get('Color', '')).strip()
        color = normalize_color_to_abbrev(color_raw)
        item_num = str(record.get('Item#', '')).strip() or f"{style} {color}"
        preview.append({
            'style': style,
            'color': color,
            'color_original': color_raw,
            'color_changed': color_raw.upper() != color.upper(),
            'item_num': item_num,
            'descrip': str(record.get('Descrip', container_number)).strip(),
            'sales_cost': _safe_float(record.get('Sales Cost', 0)),
            'retail_prs': _safe_float(record.get('Price1', 0)),
            'po_qty': _safe_float(record.get('PO Qty', 0)) if 'PO Qty' in record else 0,
            'class_cd': str(record.get('Class#', 'L1')).strip() or 'L1',
            'unit_case': _safe_float(record.get('Unit/Case', 0)),
            'box_case': _safe_float(record.get('Box/Case', 1), default=1.0),
            'case_wt': _safe_float(record.get('Case Wt', 0)),
            'unit_wt': _safe_float(record.get('Unit Wt', 0)),
            'unit_len': _safe_float(record.get('Unit Length'), default=None) if pd.notna(record.get('Unit Length')) else None,
            'unit_wi': _safe_float(record.get('Unit Width'), default=None) if pd.notna(record.get('Unit Width')) else None,
            'unit_hi': _safe_float(record.get('Unit Height'), default=None) if pd.notna(record.get('Unit Height')) else None,
            'case_len': _safe_float(record.get('Case Length'), default=None) if pd.notna(record.get('Case Length')) else None,
            'case_wi': _safe_float(record.get('Case Width'), default=None) if pd.notna(record.get('Case Width')) else None,
            'case_hi': _safe_float(record.get('Case Height'), default=None) if pd.notna(record.get('Case Height')) else None,
        })

    po_path = os.path.join(OUTPUT_DIR, f"{normalized}-PO.csv")
    if os.path.exists(po_path) and preview:
        df_po = pd.read_csv(po_path)
        po_by_item = {
            str(r.get('Item#', '')).strip(): float(r.get('P/O Qty', 0) or 0)
            for _, r in df_po.iterrows()
        }
        for row in preview:
            if row['item_num'] in po_by_item:
                row['po_qty'] = _safe_float(po_by_item[row['item_num']])

    return _sanitize_for_json(preview)


@boat_service_bp.route('/boat/preview/<container_number>', methods=['GET'])
def container_style_preview(container_number):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized = normalize_container_id(container_number)
    try:
        preview = _preview_from_styles_csv(normalized)
        return jsonify({'success': True, 'container': normalized, 'preview': preview})
    except FileNotFoundError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except Exception as e:
        current_app.logger.error(f"Error loading preview for {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/vendor/<container_number>', methods=['GET'])
def container_vendor_defaults(container_number):
    """Return pre-mapped vendor ID/name for a container PO import."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized = normalize_container_id(container_number)
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            vendor_id = lookup_container_vendor_id(cursor, normalized)
            info = lookup_vendor_info(cursor, vendor_id)
            prefix = "L" if normalized.startswith("L") else "F"
            return jsonify({
                'success': True,
                'container': normalized,
                'vendor_id': info['vendor_id'],
                'vendor_name': info['vendor_name'],
                'is_standard': info['is_standard'],
                'fallback_vendor_id': VENDOR_FALLBACK[prefix],
            })
        finally:
            conn.close()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error loading vendor defaults for {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/vendor_lookup', methods=['GET'])
def container_vendor_lookup():
    """Resolve a vendor ID to OMS vendor name (for import confirmation)."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    raw_vendor_id = (request.args.get('vendor_id') or '').strip()
    if not raw_vendor_id:
        return jsonify({'success': False, 'error': 'vendor_id is required'}), 400

    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            info = lookup_vendor_info(cursor, raw_vendor_id)
            return jsonify({'success': True, **info})
        finally:
            conn.close()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error looking up vendor {raw_vendor_id!r}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/confirm_import/<container_number>', methods=['POST'])
def confirm_container_import(container_number):
    """Save user-confirmed preview rows, then import styles into FDC and EFAS."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    current_user = session.get('username', 'unknown')
    normalized = normalize_container_id(container_number)
    data = request.get_json(force=True) or {}
    rows = data.get('rows') or []
    vendor_id = (data.get('vendor_id') or '').strip()

    if not rows:
        return jsonify({'success': False, 'error': 'No style rows provided'}), 400
    if not vendor_id:
        return jsonify({'success': False, 'error': 'Vendor ID is required'}), 400

    try:
        import_result = _run_container_db_import(
            normalized,
            rows,
            current_user,
            vendor_id=vendor_id,
        )

        current_app.logger.info(
            "User '%s' confirmed import %s; %s",
            current_user,
            normalized,
            import_result.get('message'),
        )

        payload = {
            'success': import_result['success'],
            'message': import_result['message'],
            'styles': import_result['styles'],
            'po': import_result['po'],
            'files': import_result['files'],
        }
        if import_result['success']:
            try:
                payload['row'] = _refresh_cached_hub_row(normalized)
            except Exception as row_exc:
                current_app.logger.warning(
                    "Could not refresh hub row after import %s: %s",
                    normalized,
                    row_exc,
                )
                payload['row'] = None
        status = 200 if import_result['success'] else 400
        return jsonify(payload), status
    except Exception as e:
        current_app.logger.error(f"Error confirming import for {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/row/<container_number>', methods=['GET'])
def boat_container_row(container_number):
    """Return one hub row after a live OMS status check (no full table rebuild)."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    normalized = normalize_container_id(container_number)
    try:
        row = _refresh_cached_hub_row(normalized)
        if not row:
            return jsonify({'success': False, 'error': 'Container not on hub'}), 404
        return jsonify({'success': True, 'row': row})
    except Exception as e:
        current_app.logger.error(f"Error refreshing hub row for {normalized}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@boat_service_bp.route('/boat/download/<container_number>/<file_type>', methods=['GET'])
def download_boat_file(container_number, file_type):
    """Download original Excel or generated CSVs for a container.

    file_type: 'original' | 'styles' | 'po'
    """
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        normalized = normalize_container_id(container_number)
        # Styles and PO files are stored in OUTPUT_DIR
        if file_type == 'styles':
            filename = f"{normalized}-styles.csv"
            file_path = os.path.join(OUTPUT_DIR, filename)
            if os.path.exists(file_path):
                return send_file(file_path, as_attachment=True, download_name=filename, mimetype='text/csv')
            else:
                return (f"Styles CSV for {normalized} not found", 404)

        elif file_type == 'po':
            filename = f"{normalized}-PO.csv"
            file_path = os.path.join(OUTPUT_DIR, filename)
            if os.path.exists(file_path):
                return send_file(file_path, as_attachment=True, download_name=filename, mimetype='text/csv')
            else:
                return (f"PO CSV for {normalized} not found", 404)

        elif file_type == 'original':
            container_files = scan_dropbox_containers()
            if normalized in container_files:
                orig_path = container_files[normalized]
                if os.path.exists(orig_path):
                    filename = os.path.basename(orig_path)
                    return send_file(orig_path, as_attachment=True, download_name=filename)
                else:
                    return (f"Original file for {normalized} not found on disk", 404)
            else:
                return (f"Original file for {normalized} not found in Dropbox scan", 404)

        else:
            return ("Invalid file type", 400)

    except Exception as e:
        current_app.logger.error(f"Error downloading file for {container_number} type {file_type}: {e}")
        return jsonify({'error': str(e)}), 500

@boat_service_bp.route('/boat/emails/<bol_num>')
def get_bol_emails(bol_num):
    """Get emails related to BOL number from the last month"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # Email credentials
        EMAIL_USER = 'yihao@fdcus.com'
        EMAIL_PASS = 'Fdc2640$$'
        IMAP_SERVER = 'imap.fatcow.com'
        IMAP_PORT = 993

        # Connect to IMAP server
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_USER, EMAIL_PASS)

        # Select inbox
        mail.select('inbox')

        # Calculate date one month ago
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%d-%b-%Y')

        # Search for emails containing the BOL number in subject or body
        search_criteria = f'(OR SUBJECT "{bol_num}" BODY "{bol_num}" SINCE {one_month_ago})'
        status, messages = mail.search(None, search_criteria)

        email_list = []

        if status == 'OK':
            message_nums = messages[0].split()

            # Get last 20 emails to avoid too many results
            for num in message_nums[-20:]:
                # Fetch email
                status, msg_data = mail.fetch(num, '(RFC822)')
                if status != 'OK':
                    continue

                # Parse email
                email_body = msg_data[0][1]
                email_message = email.message_from_bytes(email_body)

                # Decode subject
                subject, encoding = decode_header(email_message['Subject'])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or 'utf-8')

                # Get sender
                sender = email_message.get('From', '')

                # Get date
                date_str = email_message.get('Date', '')
                try:
                    # Parse email date
                    email_date = email.utils.parsedate_to_datetime(date_str)
                    formatted_date = email_date.strftime('%Y-%m-%d %H:%M')
                except:
                    formatted_date = date_str

                # Get body content
                body_content = ""
                if email_message.is_multipart():
                    for part in email_message.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                            except:
                                continue
                else:
                    try:
                        body_content = email_message.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except:
                        body_content = "Unable to decode email content"

                # Truncate body if too long
                if len(body_content) > 1000:
                    body_content = body_content[:1000] + "..."

                email_list.append({
                    'subject': subject,
                    'sender': sender,
                    'date': formatted_date,
                    'body': body_content,
                    'message_id': num.decode()
                })

        mail.logout()

        # Sort by date descending
        email_list.sort(key=lambda x: x['date'], reverse=True)

        return render_template('boat_yihao_email.html',
                             bol_num=bol_num,
                             emails=email_list)

    except Exception as e:
        current_app.logger.error(f"Error fetching emails for BOL {bol_num}: {e}")
        return render_template('boat_yihao_email.html',
                             bol_num=bol_num,
                             emails=[],
                             error=str(e))

@boat_service_bp.route('/boat/emails/<bol_num>/count')
def get_bol_emails_count(bol_num):
    """Get email count for BOL number from the last month"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # Email credentials
        EMAIL_USER = 'yihao@fdcus.com'
        EMAIL_PASS = 'Fdc2640$$'
        IMAP_SERVER = 'imap.fatcow.com'
        IMAP_PORT = 993

        # Connect to IMAP server
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_USER, EMAIL_PASS)

        # Select inbox
        mail.select('inbox')

        # Calculate date one month ago
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%d-%b-%Y')

        # Search for emails containing the BOL number in subject or body
        search_criteria = f'(OR SUBJECT "{bol_num}" BODY "{bol_num}" SINCE {one_month_ago})'
        status, messages = mail.search(None, search_criteria)

        email_count = 0
        if status == 'OK' and messages[0]:
            message_nums = messages[0].split()
            email_count = len(message_nums)

        mail.logout()

        return jsonify({'count': email_count})

    except Exception as e:
        current_app.logger.error(f"Error fetching email count for BOL {bol_num}: {e}")
        return jsonify({'error': str(e), 'count': 0}), 500

@boat_service_bp.route('/boat/packslip/<container_number>', methods=['GET'])
def boat_packslip(container_number):
    """Generate and download packslip Excel for a container"""
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    try:
        normalized = normalize_container_id(container_number)
        bol_num = ''
        try:
            df_sheets = pd.read_csv(GOOGLE_SHEETS_URL)
            df_sheets['_norm_container'] = (
                df_sheets['FDC container'].astype(str).str.strip().map(normalize_container_id)
            )
            matching_row = df_sheets[df_sheets['_norm_container'] == normalized]
            if not matching_row.empty:
                bol_num = str(matching_row['BOL NUM'].iloc[0]).strip() if pd.notna(matching_row['BOL NUM'].iloc[0]) else ''
        except Exception as e:
            current_app.logger.warning(f"Could not fetch BOL NUM from Google Sheets: {e}")
        
        container_files = scan_dropbox_containers()
        
        if normalized not in container_files:
            return jsonify({'error': f'Container {normalized} not found in Dropbox'}), 404
        
        file_path = container_files[normalized]
        
        # Try different sheet names and header positions for packing list (similar to container.py)
        df_packing = None
        packing_sheet = None
        
        try:
            excel_file = pd.ExcelFile(file_path)
            
            # Prefer known packing list tabs, but keep a fallback for real-world file variations.
            packing_sheet = None
            preferred_sheet_names = ['placking list', 'packing list', 'system file']

            for preferred_name in preferred_sheet_names:
                for sheet_name in excel_file.sheet_names:
                    if str(sheet_name).strip().lower() == preferred_name:
                        packing_sheet = sheet_name
                        break
                if packing_sheet:
                    break

            if not packing_sheet:
                for sheet_name in excel_file.sheet_names:
                    normalized = str(sheet_name).strip().lower()
                    if 'pack' in normalized and 'list' in normalized:
                        packing_sheet = sheet_name
                        break
            
            if not packing_sheet:
                current_app.logger.error(f"No 'Placking list' sheet found. Available sheets: {excel_file.sheet_names}")
                return jsonify({'error': f'Could not find "Placking list" sheet in {container_number}. Available sheets: {excel_file.sheet_names}'}), 400
            
            # Read the placking list sheet with no header (raw data)
            df_packing = pd.read_excel(file_path, sheet_name=packing_sheet, header=None)
        
        except Exception as e:
            current_app.logger.error(f"Error reading Excel file sheets: {e}")
            return jsonify({'error': f'Error reading Excel file: {str(e)}'}), 400
        
        if df_packing is None:
            current_app.logger.error(f"Could not read packing list from {container_number}")
            return jsonify({'error': f'Could not read packing list from {container_number}'}), 400
        
        
        # Find the header row by scanning more columns for CTN/STYLE markers.
        header_row_idx = None
        for idx in range(min(20, len(df_packing))):
            row_str = ' '.join([str(df_packing.iloc[idx, i]).upper() for i in range(min(12, len(df_packing.columns)))])
            if 'CTN' in row_str and ('STYLE' in row_str or 'QTY' in row_str):
                header_row_idx = idx
                break
        
        if header_row_idx is None:
            current_app.logger.warning("Could not find header row with 'CTN'. Checking for data starting at row 0...")
            header_row_idx = -1  # Data starts at row 0, no real header
        
        
        # DEBUG: Dump more rows to understand structure
        for row_idx in range(min(10, len(df_packing))):
            row_data = []
            for col_idx in range(min(8, len(df_packing.columns))):
                val = df_packing.iloc[row_idx, col_idx]
                row_data.append(f"C{col_idx}:'{val}'")
        
        # Determine actual column positions for Packing List sheet
        # Packing list structure: CTN NO | STYLE NO | QTY/PER CTN | QTY/CTN | TOTAL | weights...
        ctn_no_col = 0      # CTN NO (e.g., "1-16", "17", "18-33")
        style_sku_col = 1   # STYLE NO (e.g., "EN1980 BK" - contains both style and sku)
        case_qty_col = 2    # QTY/PER CTN (quantity per carton)
        num_cartons_col = 3 # QTY/CTN (number of cartons)
        total_qty_col = 4   # TOTAL (total quantity)
        
        # DEBUG: Log the actual data range and column contents

        for row_idx in range(min(15, len(df_packing))):
            row_vals = []
            for col_idx in range(min(12, len(df_packing.columns))):
                val = df_packing.iloc[row_idx, col_idx]
                row_vals.append(f"[{col_idx}]:'{val}'")
        
        # Parse packing list data starting AFTER the header row (row 7, index 6)
        packslip_data = []
        
        start_row = header_row_idx + 1 if header_row_idx >= 0 else 0
        
        last_ctn_no = ''
        for idx in range(start_row, len(df_packing)):
            try:
                row = df_packing.iloc[idx]
                
                # Extract values using hardcoded column positions
                ctn_no = str(row.iloc[ctn_no_col]).strip() if pd.notna(row.iloc[ctn_no_col]) else ''
                style_sku = str(row.iloc[style_sku_col]).strip() if pd.notna(row.iloc[style_sku_col]) else ''
                total_qty_str = str(row.iloc[total_qty_col]).strip() if pd.notna(row.iloc[total_qty_col]) else '0'
                case_qty_str = str(row.iloc[case_qty_col]).strip() if pd.notna(row.iloc[case_qty_col]) else '0'
                num_cartons_str = str(row.iloc[num_cartons_col]).strip() if pd.notna(row.iloc[num_cartons_col]) else '0'
                

                # Skip rows where ctn_no is "/" (placeholder data)
                if ctn_no == '/':
                    continue

                # Many packing sheets use merged CTN cells; carry forward last non-empty CTN.
                if not ctn_no and last_ctn_no:
                    ctn_no = last_ctn_no
                elif ctn_no:
                    last_ctn_no = ctn_no
                
                # Skip empty rows
                if not style_sku:

                    continue
                
                # Skip header-like rows
                if any(x in style_sku.upper() for x in ['STYLE', 'ITEM', 'CONTAINER', 'CTN', 'NO']):
                    current_app.logger.info(f"  -> Skipping: header row")
                    continue
                
                # Parse style and SKU from "Style SKU" format (e.g., "EN1980 BK")
                parts = style_sku.split()
                if len(parts) >= 2:
                    style = parts[0]
                    sku = ' '.join(parts[1:])
                elif len(parts) == 1:
                    style = parts[0]
                    sku = ''
                else:
                    style = style_sku
                    sku = ''
                
                # Skip if no style or sku
                if not style or not sku:
                    if idx <= start_row + 5:
                        current_app.logger.info(f"  -> Skipping: could not parse style/sku from '{style_sku}'")
                    continue
                
                # Convert to numbers
                try:
                    total_qty = int(float(total_qty_str)) if total_qty_str else 0
                    case_qty = int(float(case_qty_str)) if case_qty_str else 0
                    num_cartons = int(float(num_cartons_str)) if num_cartons_str else 0
                except:
                    if idx <= start_row + 5:
                        current_app.logger.info(f"  -> Skipping: could not parse quantities")
                    continue
                
                # Skip rows with no quantity or case qty of 0
                if total_qty == 0 or case_qty == 0:
                    if idx <= start_row + 5:
                        current_app.logger.info(f"  -> Skipping: total_qty={total_qty}, case_qty={case_qty}")
                    continue
                
                # Calculate SKU Boxes: simple box count
                sku_boxes_count = total_qty // case_qty
                
                # Use the num_cartons value from the spreadsheet directly
                
                packslip_data.append({
                    'Style': style,
                    'SKU': sku,
                    'Full SKU': f"{style} {sku}".strip(),
                    'Quantity': total_qty,
                    'Case Qty': case_qty,
                    'SKU Boxes': sku_boxes_count,
                    'CTN No': ctn_no,
                    'Num Cartons': num_cartons,
                    'Total Boxes': None  # Will be filled in based on carton data
                })
            except Exception as e:
                current_app.logger.warning(f"Error parsing row {idx}: {e}")
                continue
        
        if not packslip_data:
            return jsonify({'error': f'No valid packing list data found in {container_number}'}), 400
        
        # Calculate Total Boxes per style = SUM of QTY/CTN column for each style
        style_carton_totals = {}  # {style: sum_of_qty_ctn}
        
        for item in packslip_data:
            style = item['Style']
            num_cartons = item.get('Num Cartons', 0)
            
            if style not in style_carton_totals:
                style_carton_totals[style] = 0
            style_carton_totals[style] += num_cartons
        
        # Fill in Total Boxes for each item using the summed carton count
        for item in packslip_data:
            style = item['Style']
            item['Total Boxes'] = style_carton_totals.get(style, 0)

        # Open orders for this SKU: not invoiced and not cancelled (all customers).
        full_skus = [item.get('Full SKU', '') for item in packslip_data]
        sku_orders_map = get_packslip_open_orders(full_skus)
        for item in packslip_data:
            full_sku = str(item.get('Full SKU', '')).strip().upper()
            orders = sku_orders_map.get(full_sku, [])
            item['Qty in Order'] = sum(int(order.get('qty') or 0) for order in orders)
            item['orders'] = orders
            item['order_text'] = _format_packslip_order_cell(orders)

        # Consolidate duplicate rows with the same Style/SKU/Case Qty.
        # This collapses repeated lines (e.g. same SKU with identical case pack)
        # into one row by summing Quantity and SKU Boxes.
        consolidated_map = {}
        consolidated_order = []
        for item in packslip_data:
            key = (item.get('Style'), item.get('SKU'), item.get('Case Qty'))
            if key not in consolidated_map:
                consolidated_map[key] = dict(item)
                consolidated_order.append(key)
                continue

            existing = consolidated_map[key]
            existing['Quantity'] = int(existing.get('Quantity', 0) or 0) + int(item.get('Quantity', 0) or 0)
            existing['SKU Boxes'] = int(existing.get('SKU Boxes', 0) or 0) + int(item.get('SKU Boxes', 0) or 0)
            existing['Num Cartons'] = int(existing.get('Num Cartons', 0) or 0) + int(item.get('Num Cartons', 0) or 0)

        packslip_data = [consolidated_map[k] for k in consolidated_order]

        # Exclude sample-only styles: Total Boxes = 0 and Total Quantity = 1.
        style_totals_for_filter = {}
        style_boxes_for_filter = {}
        for item in packslip_data:
            style = item.get('Style')
            if not style:
                continue
            style_totals_for_filter[style] = style_totals_for_filter.get(style, 0) + int(item.get('Quantity', 0) or 0)
            if style not in style_boxes_for_filter:
                style_boxes_for_filter[style] = int(item.get('Total Boxes', 0) or 0)

        excluded_sample_styles = {
            style for style, total_qty in style_totals_for_filter.items()
            if style_boxes_for_filter.get(style, 0) == 0 and total_qty == 1
        }

        if excluded_sample_styles:
            packslip_data = [item for item in packslip_data if item.get('Style') not in excluded_sample_styles]

        if not packslip_data:
            return jsonify({'error': f'No non-sample packing list data found in {container_number}'}), 400

        def _ctn_start(item):
            match = re.match(r"(\d+)", str(item.get("CTN No") or "").strip())
            return int(match.group(1)) if match else 10**9

        # Keep style order. Within a style, keep the same SKU together so a 12/box
        # row and leftover 1/box row stack, with orders shown once on that SKU.
        styles_order = []
        items_by_style = {}
        for item in packslip_data:
            style = item.get("Style")
            if style not in items_by_style:
                styles_order.append(style)
                items_by_style[style] = []
            items_by_style[style].append(item)

        packslip_data = []
        for style in styles_order:
            group = items_by_style[style]
            sku_order = []
            items_by_sku = {}
            for item in group:
                sku_key = str(item.get("SKU") or "").strip().upper()
                if sku_key not in items_by_sku:
                    sku_order.append(sku_key)
                    items_by_sku[sku_key] = []
                items_by_sku[sku_key].append(item)
            sku_order.sort(key=lambda sku: min(_ctn_start(row) for row in items_by_sku[sku]))
            for sku_key in sku_order:
                rows = sorted(
                    items_by_sku[sku_key],
                    key=lambda row: (-int(row.get("Case Qty") or 0), _ctn_start(row)),
                )
                packslip_data.extend(rows)

        remaining_need = {}
        for item in packslip_data:
            sku_key = str(item.get("Full SKU") or "").strip().upper()
            if sku_key not in remaining_need:
                remaining_need[sku_key] = int(item.get("Qty in Order") or 0)
            sold_text, stay_text, taken = _packslip_row_sold_stay(
                remaining_need[sku_key],
                item.get("Quantity"),
                item.get("Case Qty"),
                item.get("SKU Boxes"),
            )
            item["sold_text"] = sold_text
            item["stay_text"] = stay_text
            remaining_need[sku_key] = max(0, remaining_need[sku_key] - int(taken or 0))

        order_skus: dict[str, set[str]] = {}
        for item in packslip_data:
            sku_key = str(item.get("Full SKU") or "").strip().upper()
            if not sku_key:
                continue
            seen = set()
            for order in item.get("orders") or []:
                oid = str(order.get("order_id") or "").strip()
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                order_skus.setdefault(oid, set()).add(sku_key)
        shared_ids = [
            oid for oid, skus in order_skus.items() if len(skus) > 1
        ]
        shared_ids.sort(key=lambda oid: (0, int(oid)) if oid.isdigit() else (1, oid))
        order_highlights = {
            oid: _PACKSLIP_ORDER_COLORS[idx % len(_PACKSLIP_ORDER_COLORS)]
            for idx, oid in enumerate(shared_ids)
        }
        for item in packslip_data:
            item["order_text"] = _format_packslip_order_cell(
                item.get("orders") or [],
                highlights=order_highlights,
            )
        
        # Create output Excel file using openpyxl for merged cells
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Side, Font
        from openpyxl.worksheet.pagebreak import Break
        from openpyxl.worksheet.page import PageMargins
        
        wb = Workbook()
        ws = wb.active
        ws.title = 'Packslip'
        
        # Landscape letter, fit all columns on one page width.
        # pageSetUpPr.fitToPage is required — page_setup.fitToPage alone is ignored by Excel.
        ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.horizontalCentered = True
        ws.page_margins = PageMargins(
            left=0.2, right=0.2, top=0.25, bottom=0.25, header=0.12, footer=0.12
        )
        ws.print_options.horizontalCentered = True
        ws.oddFooter.center.text = "Page &P of &N"
        
        # Define border style
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        print_font = Font(name='Calibri', size=10)
        print_font_bold = Font(name='Calibri', size=10, bold=True)
        print_font_number = Font(name='Calibri', size=14, bold=True)
        print_font_es = Font(name='Calibri', size=8, italic=True, color='666666')
        center_alignment = Alignment(horizontal='center', vertical='center', wrap_text=False)
        right_alignment = Alignment(horizontal='right', vertical='center', wrap_text=False)
        order_cell_alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
        thin_side = Side(style='thin')
        qty_left_border = Border(left=thin_side, top=thin_side, bottom=thin_side)
        qty_mid_border = Border(top=thin_side, bottom=thin_side)
        qty_right_border = Border(right=thin_side, top=thin_side, bottom=thin_side)

        def write_en_es(row, col, english, spanish, merge_to=None):
            cell = ws.cell(row=row, column=col, value=english)
            cell.alignment = center_alignment
            cell.border = thin_border
            cell.font = print_font_bold
            es_cell = ws.cell(row=row + 1, column=col, value=spanish)
            es_cell.alignment = center_alignment
            es_cell.border = thin_border
            es_cell.font = print_font_es
            if merge_to and merge_to > col:
                for merge_col in range(col, merge_to + 1):
                    for merge_row in (row, row + 1):
                        merge_cell = ws.cell(row=merge_row, column=merge_col)
                        merge_cell.alignment = center_alignment
                        merge_cell.border = thin_border
                ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=merge_to)
                ws.merge_cells(start_row=row + 1, start_column=col, end_row=row + 1, end_column=merge_to)

        # Row 1 English titles, row 2 Spanish titles, row 3 values
        write_en_es(1, 2, 'Internal', 'Interno')
        write_en_es(1, 3, 'BOL NUM', 'N° BOL')
        ws.row_dimensions[2].height = 14
        
        cell = ws.cell(row=3, column=2, value=container_number)
        cell.alignment = center_alignment
        cell.border = thin_border
        
        cell = ws.cell(row=3, column=3, value=bol_num if bol_num else 'N/A')
        cell.alignment = center_alignment
        cell.border = thin_border
        
        # Row 5 English summary titles, row 6 Spanish
        write_en_es(5, 1, 'Style', 'Estilo')
        write_en_es(5, 2, 'Total Boxes', 'Total cajas')
        write_en_es(5, 3, 'Total Quantity', 'Cantidad total')
        write_en_es(5, 4, 'orders', 'Pedidos', merge_to=9)
        ws.row_dimensions[6].height = 14
        
        # Add summary data (one row per unique style)
        summary_row = 7
        styles_summary = {}
        
        for item in packslip_data:
            style = item['Style']
            if style not in styles_summary:
                styles_summary[style] = {
                    'total_boxes': item['Total Boxes'],
                    'total_qty': 0,
                    'order_pcs': 0,
                    '_skus': set(),
                }
            styles_summary[style]['total_qty'] += item['Quantity']
            sku_key = str(item.get('Full SKU') or '').strip().upper()
            if sku_key and sku_key not in styles_summary[style]['_skus']:
                styles_summary[style]['_skus'].add(sku_key)
                styles_summary[style]['order_pcs'] += sum(
                    int(order.get('qty') or 0) for order in (item.get('orders') or [])
                )
        
        for style, data in styles_summary.items():
            cell = ws.cell(row=summary_row, column=1, value=style)
            cell.alignment = center_alignment
            cell.border = thin_border
            
            cell = ws.cell(row=summary_row, column=2, value=data['total_boxes'])
            cell.alignment = center_alignment
            cell.border = thin_border
            cell.font = print_font_number
            
            cell = ws.cell(row=summary_row, column=3, value=data['total_qty'])
            cell.alignment = center_alignment
            cell.border = thin_border
            cell.font = print_font_number

            cell = ws.cell(row=summary_row, column=4, value=data['order_pcs'] or '')
            cell.alignment = center_alignment
            cell.border = thin_border
            if data['order_pcs']:
                cell.font = print_font_number
            for merge_col in range(4, 10):
                merge_cell = ws.cell(row=summary_row, column=merge_col)
                merge_cell.alignment = center_alignment
                merge_cell.border = thin_border
            ws.merge_cells(start_row=summary_row, start_column=4, end_row=summary_row, end_column=9)
            
            summary_row += 1
        
        chart_start_row = summary_row + 1
        chart_es_row = chart_start_row + 1
        
        # Qty is 6 aligned sub-columns: n /box x n boxes = n pc
        ws.column_dimensions['A'].width = 11
        ws.column_dimensions['B'].width = 11
        ws.column_dimensions['C'].width = 14
        ws.column_dimensions['D'].width = 5.5
        ws.column_dimensions['E'].width = 6.5
        ws.column_dimensions['F'].width = 5
        ws.column_dimensions['G'].width = 7.5
        ws.column_dimensions['H'].width = 6
        ws.column_dimensions['I'].width = 3.2
        ws.column_dimensions['J'].width = 12
        ws.column_dimensions['K'].width = 12
        ws.column_dimensions['L'].width = 32
        
        write_en_es(chart_start_row, 1, 'Style', 'Estilo')
        write_en_es(chart_start_row, 2, 'Total Boxes', 'Total cajas')
        write_en_es(chart_start_row, 3, 'SKU', 'SKU')
        write_en_es(chart_start_row, 4, 'Qty', 'Cantidad', merge_to=9)
        write_en_es(chart_start_row, 10, 'stay', 'Queda')
        write_en_es(chart_start_row, 11, 'sold', 'Vendido')
        write_en_es(chart_start_row, 12, 'orders', 'Pedidos')
        ws.row_dimensions[chart_es_row].height = 14
        
        def merge_style_block(start_row, end_row, style_name):
            if start_row is None or end_row < start_row:
                return
            if start_row < end_row:
                ws.merge_cells(f'A{start_row}:A{end_row}')
                ws.merge_cells(f'B{start_row}:B{end_row}')
            style_cell = ws.cell(row=start_row, column=1)
            style_cell.alignment = center_alignment
            style_cell.border = thin_border
            total_cell = ws.cell(row=start_row, column=2)
            for prev_item in packslip_data:
                if prev_item['Style'] == style_name:
                    total_cell.value = prev_item['Total Boxes']
                    break
            total_cell.alignment = center_alignment
            total_cell.border = thin_border
            total_cell.font = print_font_number

        def merge_sku_block(start_row, end_row):
            if start_row is None or end_row <= start_row:
                return
            ws.merge_cells(f'C{start_row}:C{end_row}')
            for col in range(4, 12):
                ws.merge_cells(
                    start_row=start_row, start_column=col, end_row=end_row, end_column=col
                )
            ws.merge_cells(f'L{start_row}:L{end_row}')
            ws.cell(row=start_row, column=3).alignment = center_alignment
            for col in (4, 6, 8):
                ws.cell(row=start_row, column=col).alignment = Alignment(
                    horizontal='right', vertical='center', wrap_text=True
                )
            for col in (5, 7, 9, 10, 11):
                ws.cell(row=start_row, column=col).alignment = Alignment(
                    horizontal='center', vertical='center', wrap_text=True
                )
            ws.cell(row=start_row, column=12).alignment = order_cell_alignment

        # Add data and merge cells for same styles / same SKU case-packs
        row_idx = chart_es_row + 1
        current_style = None
        merge_start_row = None
        current_sku_key = None
        sku_merge_start_row = None
        qty_num_wrap = Alignment(horizontal='right', vertical='center', wrap_text=True)
        qty_label_wrap = Alignment(horizontal='center', vertical='center', wrap_text=True)

        for item_idx, item in enumerate(packslip_data):
            style = item['Style']
            sku_key = str(item.get('Full SKU') or f"{style} {item.get('SKU') or ''}").strip().upper()
            
            if style != current_style:
                if current_style is not None and merge_start_row is not None:
                    merge_sku_block(sku_merge_start_row, row_idx - 1)
                
                current_style = style
                merge_start_row = row_idx
                current_sku_key = None
                sku_merge_start_row = None

            if sku_key != current_sku_key:
                merge_sku_block(sku_merge_start_row, row_idx - 1)
                current_sku_key = sku_key
                sku_merge_start_row = row_idx

            is_first_sku_row = sku_merge_start_row == row_idx
            pack_items = [item]
            if is_first_sku_row:
                look = item_idx + 1
                while look < len(packslip_data):
                    nxt = packslip_data[look]
                    nxt_key = str(
                        nxt.get('Full SKU') or f"{nxt.get('Style') or ''} {nxt.get('SKU') or ''}"
                    ).strip().upper()
                    if nxt_key != sku_key:
                        break
                    pack_items.append(nxt)
                    look += 1

            cell = ws.cell(row=row_idx, column=1, value=item['Style'])
            cell.alignment = center_alignment
            cell.border = thin_border
            
            cell = ws.cell(row=row_idx, column=2, value=item['Total Boxes'])
            cell.alignment = center_alignment
            cell.border = thin_border
            cell.font = print_font_number
            
            sku_with_style = f"{item['Style']} {item['SKU']}"
            cell = ws.cell(row=row_idx, column=3, value=sku_with_style)
            cell.alignment = center_alignment
            cell.border = thin_border

            if is_first_sku_row:
                case_vals = [str(int(p.get('Case Qty') or 0)) for p in pack_items]
                box_vals = [str(int(p.get('SKU Boxes') or 0)) for p in pack_items]
                pcs_vals = [str(int(p.get('Quantity') or 0)) for p in pack_items]
                qty_parts = (
                    (4, "\n".join(case_vals), print_font_number, qty_num_wrap, qty_left_border),
                    (5, "\n".join(["/box x"] * len(pack_items)), print_font, qty_label_wrap, qty_mid_border),
                    (6, "\n".join(box_vals), print_font_number, qty_num_wrap, qty_mid_border),
                    (7, "\n".join(["boxes ="] * len(pack_items)), print_font, qty_label_wrap, qty_mid_border),
                    (8, "\n".join(pcs_vals), print_font_number, qty_num_wrap, qty_mid_border),
                    (9, "\n".join(["pc"] * len(pack_items)), print_font, qty_label_wrap, qty_right_border),
                )
                sold_text = item.get("sold_text")
                stay_text = item.get("stay_text")
            else:
                qty_parts = (
                    (4, None, print_font_number, qty_num_wrap, qty_left_border),
                    (5, None, print_font, qty_label_wrap, qty_mid_border),
                    (6, None, print_font_number, qty_num_wrap, qty_mid_border),
                    (7, None, print_font, qty_label_wrap, qty_mid_border),
                    (8, None, print_font_number, qty_num_wrap, qty_mid_border),
                    (9, None, print_font, qty_label_wrap, qty_right_border),
                )
                sold_text = ""
                stay_text = ""
            for col, value, font, align, border in qty_parts:
                cell = ws.cell(row=row_idx, column=col, value=value)
                cell.font = font
                cell.alignment = align
                cell.border = border

            cell = ws.cell(row=row_idx, column=10, value=0 if stay_text == 0 else (stay_text if stay_text != "" else ""))
            cell.alignment = qty_label_wrap
            cell.border = thin_border
            if stay_text == 0:
                cell.font = print_font_number

            cell = ws.cell(row=row_idx, column=11, value=sold_text if sold_text != "" else "")
            cell.alignment = qty_label_wrap
            cell.border = thin_border
            if sold_text == "ALL" or sold_text == 0:
                cell.font = print_font_number

            orders = item.get('orders') or []
            if is_first_sku_row:
                order_text = item.get('order_text') or ''
                cell = ws.cell(row=row_idx, column=12, value=order_text)
                cell.alignment = order_cell_alignment
                line_count = max(
                    _packslip_cell_line_count(order_text),
                    len(orders) or 0,
                    len(pack_items),
                    1,
                )
                ws.row_dimensions[row_idx].height = _packslip_row_height(line_count)
            else:
                cell = ws.cell(row=row_idx, column=12, value='')
                cell.alignment = center_alignment
                ws.row_dimensions[row_idx].height = 15
            cell.border = thin_border
            
            row_idx += 1
        
        merge_sku_block(sku_merge_start_row, row_idx - 1)
        
        last_row = max(ws.max_row or 1, row_idx - 1)
        ws.print_area = f"A1:L{last_row}"
        ws.print_title_rows = f"{chart_start_row}:{chart_es_row}"

        def _row_h(r):
            h = ws.row_dimensions[r].height
            return float(h) if h else 15.0

        data_start = chart_es_row + 1
        style_blocks = []
        cursor = 0
        excel_row = data_start
        while cursor < len(packslip_data):
            style = packslip_data[cursor]['Style']
            style_start = excel_row
            style_skus = []
            while cursor < len(packslip_data) and packslip_data[cursor]['Style'] == style:
                sku_key = str(
                    packslip_data[cursor].get('Full SKU')
                    or f"{packslip_data[cursor].get('Style')} {packslip_data[cursor].get('SKU')}"
                ).strip().upper()
                sku_start = excel_row
                while cursor < len(packslip_data):
                    item = packslip_data[cursor]
                    item_key = str(
                        item.get('Full SKU') or f"{item.get('Style')} {item.get('SKU')}"
                    ).strip().upper()
                    if item['Style'] != style or item_key != sku_key:
                        break
                    cursor += 1
                    excel_row += 1
                sku_h = sum(_row_h(r) for r in range(sku_start, excel_row))
                style_skus.append((sku_start, excel_row - 1, sku_h))
            style_h = sum(block[2] for block in style_skus)
            style_blocks.append((style, style_start, excel_row - 1, style_h, style_skus))

        # Fill leftover page space SKU by SKU. Only break when the next SKU
        # cannot fit. Never split a SKU merge.
        page_pts = 548
        title_pts = _row_h(chart_start_row) + _row_h(chart_es_row)
        page_top = sum(_row_h(r) for r in range(1, data_start))
        used = page_top
        for style, style_start, style_end, style_h, style_skus in style_blocks:
            for sku_start, sku_end, sku_h in style_skus:
                if used + sku_h > page_pts and used > page_top + 0.5:
                    ws.row_breaks.append(Break(id=sku_start - 1))
                    page_top = title_pts
                    used = title_pts + sku_h
                else:
                    used += sku_h

        break_after = {int(b.id) for b in (getattr(ws.row_breaks, "brk", None) or [])}
        for style, style_start, style_end, style_h, style_skus in style_blocks:
            seg_start = style_start
            for r in range(style_start, style_end):
                if r in break_after:
                    merge_style_block(seg_start, r, style)
                    seg_start = r + 1
            merge_style_block(seg_start, style_end, style)

        qty_num_cols = {4, 6, 8}
        qty_label_cols = {5, 7, 9, 10, 11}
        for row in ws.iter_rows(min_row=1, max_row=last_row, min_col=1, max_col=12):
            for cell in row:
                if cell.value is None and not (cell.border and cell.border.left and cell.border.left.style):
                    continue
                is_rich = isinstance(cell.value, CellRichText)
                if not is_rich:
                    if cell.font and cell.font.sz == 14:
                        pass
                    elif cell.font and cell.font.italic:
                        pass
                    elif cell.font and cell.font.bold:
                        cell.font = print_font_bold
                    else:
                        cell.font = print_font
                if cell.row > chart_es_row and cell.column == 12:
                    cell.alignment = order_cell_alignment
                elif cell.row > chart_es_row and cell.column in qty_num_cols:
                    cell.alignment = qty_num_wrap
                elif cell.row > chart_es_row and cell.column in qty_label_cols:
                    cell.alignment = qty_label_wrap
                elif cell.row >= 3:
                    cell.alignment = center_alignment
        
        # Save to bytes
        from io import BytesIO
        output = BytesIO()
        wb.save(output)
        output.seek(0)

        current_user = session.get("username") or session.get("user") or "unknown"
        client_ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or (request.remote_addr or "")
        bol_disp = (bol_num or "").strip() or "N/A"
        # Same log-viewer convention as boat_service(): ***user***__@@@action@@@ …
        current_app.logger.info(
            f"***{current_user}***__@@@downloaded packing slip@@@ "
            f"https://www.fdcef.com/boat FDC_container={container_number} BOL={bol_disp} ip={client_ip}"
        )

        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'{container_number}_Packslip.xlsx'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error generating packslip for {container_number}: {e}")
        return jsonify({'error': str(e)}), 500