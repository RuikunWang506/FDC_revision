from __future__ import annotations

import os
import time
import json
import re
from io import BytesIO
from datetime import datetime
from typing import Any

import pandas as pd
import pyodbc
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, session, send_file

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config, login_required


four_seller_bp = Blueprint("four_seller", __name__)


SKU_EXPORT_URL = "https://www.4seller.com/api/sku-inventory/export"
SKU_TASK_URL = "https://www.4seller.com/api/sku-inventory/task"
DASHBOARD_URL = "https://www.4seller.com/web/dashboard.html"

DEFAULT_EXPORT_PAYLOAD = {
    "pageCurrent": 1,
    "status": "",
    "searchType": "sku",
    "searchValue": "",
    "warehouseId": "",
    "tagId": "",
    "groupId": "",
    "orderBy": "OnHand",
    "desc": "asc",
    "onHandMin": "",
    "onHandMax": "",
    "availableMin": "",
    "availableMax": "",
}

COOKIE_CACHE_FILE = os.path.join("data", "4seller_cookie_cache.json")
EXCEL_IMPORT_URL = "https://www.4seller.com/api/excel/import"
EXCEL_IMPORT_SCHEDULE_URL = "https://www.4seller.com/api/excel/get-import-schedule"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.4seller.com/en-US/login.html",
            "Origin": "https://www.4seller.com",
        }
    )
    return session


def _ensure_cookie_cache_dir() -> None:
    os.makedirs(os.path.dirname(COOKIE_CACHE_FILE), exist_ok=True)


def _save_cookie_cache(cookie_header: str) -> None:
    _ensure_cookie_cache_dir()
    payload = {
        "cookie": cookie_header,
        "saved_at": datetime.now().isoformat(),
    }
    with open(COOKIE_CACHE_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)


def _load_cookie_cache() -> str:
    if not os.path.exists(COOKIE_CACHE_FILE):
        return ""
    try:
        with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return str(payload.get("cookie", "") or "").strip()
    except Exception:
        return ""


def _delete_cookie_cache() -> None:
    try:
        if os.path.exists(COOKIE_CACHE_FILE):
            os.remove(COOKIE_CACHE_FILE)
    except Exception:
        pass


def _has_cookie_cache() -> bool:
    return bool(_load_cookie_cache())


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    def _clean_latin1(value: str) -> str:
        # Requests encodes headers using latin-1, so strip unsupported chars (e.g. checkmark symbols).
        return "".join(ch for ch in value if ch.isprintable() and ord(ch) <= 255)

    def _add_cookie_pair(cookies_dict: dict[str, str], key: str, value: str) -> None:
        key = _clean_latin1((key or "").strip())
        value = _clean_latin1((value or "").strip())
        if key:
            cookies_dict[key] = value

    cookies: dict[str, str] = {}

    normalized_text = (cookie_header or "").replace("\r", "\n")

    # Support direct copy from DevTools cookie table (tab-separated rows with extra columns).
    for raw_line in normalized_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        if "\t" in line:
            cols = [c.strip() for c in line.split("\t")]
            if len(cols) >= 2 and cols[0] and cols[1] and cols[0].lower() not in {"name", "cookie"}:
                _add_cookie_pair(cookies, cols[0], cols[1])
                continue

        # Also support whitespace-separated table copy where tabs are lost.
        # Example: name value domain path expiry size ...
        ws_match = re.match(r"^([^\s=;]+)\s+([^\s]+)\s+\.?[A-Za-z0-9.-]*4seller\.com\b", line)
        if ws_match:
            _add_cookie_pair(cookies, ws_match.group(1), ws_match.group(2))
            continue

        # Also support classic Cookie header format.
        for part in line.split(";"):
            chunk = part.strip()
            if not chunk or "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            _add_cookie_pair(cookies, key, value)

    # Heal common copy typo seen in manual entry.
    if "serToken" in cookies and "userToken" not in cookies:
        cookies["userToken"] = cookies.pop("serToken")

    return cookies


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:500]}


def _build_cookie_session(cookie_header: str) -> requests.Session:
    session = _new_session()
    cookies = _parse_cookie_header(cookie_header)
    if not cookies:
        raise ValueError("No valid cookie values found.")
    session.cookies.update(cookies)
    return session


def _resolve_cookie_header(user_cookie: str) -> tuple[str, bool]:
    """Return (cookie_header, from_cache)."""
    cleaned_user_cookie = (user_cookie or "").strip()
    if cleaned_user_cookie:
        return cleaned_user_cookie, False

    cached_cookie = _load_cookie_cache()
    if cached_cookie:
        return cached_cookie, True

    return "", False


def _verify_4seller_session(session: requests.Session) -> bool:
    dashboard_response = session.get(DASHBOARD_URL, allow_redirects=True, timeout=30)
    if dashboard_response.status_code != 200:
        return False
    if "login.html" in dashboard_response.url.lower():
        return False
    return "dashboard" in dashboard_response.url.lower()


def _ensure_4seller_table() -> None:
    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `4seller_stock` (
            id INT AUTO_INCREMENT PRIMARY KEY,
            stock_sku VARCHAR(120) NOT NULL,
            internal_sku VARCHAR(120) NOT NULL,
            `4seller_stock` INT NOT NULL DEFAULT 0,
            status VARCHAR(10) NOT NULL DEFAULT 'active',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_stock_sku (stock_sku),
            INDEX idx_internal_sku (internal_sku)
        )
        """
    )

    # Backward-compatible migration for existing environments.
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = '4seller_stock'
          AND COLUMN_NAME = 'status'
        """
    )
    has_status = cursor.fetchone()[0] > 0
    if not has_status:
        cursor.execute("ALTER TABLE `4seller_stock` ADD COLUMN status VARCHAR(10) NOT NULL DEFAULT 'active'")

    connection.commit()
    cursor.close()
    connection.close()


def _request_export_file_url(session: requests.Session) -> str:
    export_response = session.post(SKU_EXPORT_URL, json=DEFAULT_EXPORT_PAYLOAD, timeout=30)
    export_data = _safe_json(export_response)

    if not isinstance(export_data, dict) or export_data.get("code") != 0:
        raise RuntimeError(f"4Seller export failed: {export_data}")

    task_id = export_data.get("data")
    if not task_id:
        raise RuntimeError("4Seller export did not return task id.")

    deadline = time.time() + 120
    while time.time() < deadline:
        task_response = session.get(SKU_TASK_URL, params={"taskId": task_id}, timeout=30)
        task_data = _safe_json(task_response)
        if isinstance(task_data, dict) and task_data.get("code") == 0:
            data = task_data.get("data") or {}
            file_url = data.get("fileUrl")
            if file_url:
                return file_url
        time.sleep(2)

    raise TimeoutError("Timed out waiting for 4Seller export file URL.")


def _download_export_bytes(file_url: str) -> bytes:
    response = requests.get(file_url, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Export download failed with status {response.status_code}")

    return response.content


def _to_int(value: Any) -> int:
    try:
        if pd.isna(value):
            return 0
    except Exception:
        pass

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _load_rows_from_xlsx_bytes(file_bytes: bytes) -> list[tuple[str, str, int]]:
    df = pd.read_excel(BytesIO(file_bytes))
    if "Stock SKU" not in df.columns or "On Hand" not in df.columns:
        raise ValueError("Missing required xlsx columns: 'Stock SKU' and 'On Hand'.")

    rows: list[tuple[str, str, int]] = []
    for _, row in df.iterrows():
        stock_sku = str(row.get("Stock SKU", "")).strip()
        if not stock_sku or stock_sku.lower() == "nan":
            continue

        on_hand = _to_int(row.get("On Hand", 0))
        # Current rule requested by user: internal sku mirrors stock sku.
        internal_sku = stock_sku
        rows.append((stock_sku, internal_sku, on_hand))

    return rows


def _replace_4seller_stock(rows: list[tuple[str, str, int]]) -> int:
    _ensure_4seller_table()

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()

    cursor.execute("SELECT stock_sku, status FROM `4seller_stock`")
    status_map = {str(sku): str(status or "active").lower() for sku, status in cursor.fetchall()}

    cursor.execute("DELETE FROM `4seller_stock`")

    insert_sql = (
        "INSERT INTO `4seller_stock` (stock_sku, internal_sku, `4seller_stock`, status) "
        "VALUES (%s, %s, %s, %s)"
    )
    if rows:
        rows_with_status = []
        for stock_sku, internal_sku, four_seller_stock in rows:
            status = status_map.get(stock_sku, "active")
            if status not in {"active", "inactive"}:
                status = "active"
            rows_with_status.append((stock_sku, internal_sku, four_seller_stock, status))
        cursor.executemany(insert_sql, rows_with_status)

    connection.commit()
    count = len(rows)
    cursor.close()
    connection.close()
    return count


def _build_upload_xlsx_bytes(rows: list[dict[str, Any]]) -> bytes:
    export_rows: list[dict[str, Any]] = []
    for row in rows:
        stock_sku = str(row.get("stock_sku", "")).strip()
        if not stock_sku:
            continue

        status = str(row.get("status", "active") or "active").lower()
        on_hand = int(row.get("our_available_qty", 0) or 0)

        if status == "inactive":
            on_hand = 0
        if on_hand < 0:
            on_hand = 0

        export_rows.append(
            {
                "Stock SKU*": stock_sku,
                "Warehouse Name": "827",
                "OnHand*": on_hand,
                "Safety Stock": 0,
            }
        )

    output = BytesIO()
    df = pd.DataFrame(export_rows, columns=["Stock SKU*", "Warehouse Name", "OnHand*", "Safety Stock"])
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
    output.seek(0)
    return output.getvalue()


def _upload_excel_to_4seller(session_client: requests.Session, excel_bytes: bytes) -> dict[str, Any]:
    file_name = f"4seller_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    files = {
        "file": (
            file_name,
            excel_bytes,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }

    response = session_client.post(
        EXCEL_IMPORT_URL,
        params={"excelImportType": "sku_inventory_update_onhand"},
        files=files,
        timeout=90,
    )
    data = _safe_json(response)
    if not isinstance(data, dict) or data.get("code") != 0:
        raise RuntimeError(f"4Seller import upload failed: {data}")
    return data


def _try_import_schedule_payloads(session_client: requests.Session, async_task: str) -> dict[str, Any]:
    payload_variants = [
        {},
        {"asyncTask": async_task},
        {"taskId": async_task},
        {"task_id": async_task},
        {"async_task": async_task},
        {"id": async_task},
    ]

    last_data: dict[str, Any] = {"code": -1, "msg": "No schedule response"}
    for payload in payload_variants:
        response = session_client.post(EXCEL_IMPORT_SCHEDULE_URL, json=payload, timeout=30)
        data = _safe_json(response)
        if isinstance(data, dict):
            last_data = data
            if data.get("code") == 0:
                return data
    return last_data


def _poll_import_schedule(session_client: requests.Session, async_task: str, max_wait_seconds: int = 60) -> dict[str, Any]:
    deadline = time.time() + max_wait_seconds
    last_data: dict[str, Any] = {"code": -1, "msg": "No schedule response"}

    while time.time() < deadline:
        data = _try_import_schedule_payloads(session_client, async_task)
        last_data = data if isinstance(data, dict) else last_data

        if isinstance(data, dict) and data.get("code") == 0:
            inner = data.get("data")
            if isinstance(inner, dict):
                # Completed when inner code is present and not pending zero.
                if inner.get("code") not in (None, 0):
                    return data
                # If fail/success summary exists, also treat as completed.
                if any(k in inner for k in ("failMsg", "successCount", "failCount", "total")):
                    return data

        time.sleep(2)

    return last_data


def _fetch_4seller_rows() -> list[dict[str, Any]]:
    _ensure_4seller_table()

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT stock_sku, internal_sku, `4seller_stock`, status, updated_at
        FROM `4seller_stock`
        ORDER BY stock_sku
        """
    )
    rows = cursor.fetchall()
    cursor.close()
    connection.close()
    return rows


def _query_available_map_from_db(conn: pyodbc.Connection, sku_list: list[str]) -> dict[str, int]:
    if not sku_list:
        return {}

    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(sku_list))
    query = f"""
        SELECT
            i.PROD_CD AS sku,
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS available
        FROM dbo.inv AS i
        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
        WHERE i.PROD_CD IN ({placeholders})
    """
    cursor.execute(query, sku_list)

    available: dict[str, int] = {}
    for sku, qty in cursor.fetchall():
        sku_key = str(sku).strip().upper()
        available[sku_key] = available.get(sku_key, 0) + int(qty or 0)

    cursor.close()
    return available


def _fetch_our_available_map(sku_list: list[str]) -> dict[str, int]:
    if not sku_list:
        return {}

    config = Config.SQL_SERVER_CONFIG
    conn_str_fdc = (
        f'DRIVER={config["driver"]};SERVER={config["server"]};DATABASE={config["database_fdc"]};'
        f'UID={config["username"]};PWD={config["password"]}'
    )
    conn_str_efas = (
        f'DRIVER={config["driver"]};SERVER={config["server"]};DATABASE={config["database_oms_ef"]};'
        f'UID={config["username"]};PWD={config["password"]}'
    )

    normalized_skus = [s.strip().upper() for s in sku_list if s and str(s).strip()]
    if not normalized_skus:
        return {}

    merged: dict[str, int] = {}
    conn_fdc = None
    conn_efas = None
    try:
        conn_fdc = pyodbc.connect(conn_str_fdc)
        conn_efas = pyodbc.connect(conn_str_efas)

        fdc_map = _query_available_map_from_db(conn_fdc, normalized_skus)
        efas_map = _query_available_map_from_db(conn_efas, normalized_skus)

        for sku in normalized_skus:
            merged[sku] = fdc_map.get(sku, 0) + efas_map.get(sku, 0)

    finally:
        if conn_fdc:
            conn_fdc.close()
        if conn_efas:
            conn_efas.close()

    return merged


def _build_page_rows() -> list[dict[str, Any]]:
    rows = _fetch_4seller_rows()
    sku_list = [str(row.get("internal_sku", "")).strip() for row in rows]
    available_map = _fetch_our_available_map(sku_list)

    page_rows: list[dict[str, Any]] = []
    for row in rows:
        internal_sku = str(row.get("internal_sku", "")).strip()
        sku_key = internal_sku.upper()
        page_rows.append(
            {
                "stock_sku": row.get("stock_sku", ""),
                "internal_sku": internal_sku,
                "four_seller_stock": int(row.get("4seller_stock", 0) or 0),
                "our_available_qty": int(available_map.get(sku_key, 0) or 0),
                "status": str(row.get("status", "active") or "active").lower(),
                "updated_at": row.get("updated_at"),
            }
        )

    return page_rows


def sync_4seller_from_cookie(cookie_header: str) -> dict[str, Any]:
    """Run full 4Seller export -> import pipeline using a cookie header."""
    cookie_header = (cookie_header or "").strip()
    if not cookie_header:
        raise ValueError("Missing cookie header.")

    session_client = _build_cookie_session(cookie_header)
    if not _verify_4seller_session(session_client):
        raise RuntimeError("4Seller session invalid or expired.")

    file_url = _request_export_file_url(session_client)
    file_bytes = _download_export_bytes(file_url)
    rows = _load_rows_from_xlsx_bytes(file_bytes)
    inserted = _replace_4seller_stock(rows)

    return {
        "success": True,
        "message": f"Imported {inserted} SKU rows from 4Seller export.",
        "file_url": file_url,
        "file_path": None,
        "synced_at": datetime.now().isoformat(),
        "imported_rows": inserted,
    }


@four_seller_bp.route("/4seller", methods=["GET"])
@login_required
def four_seller_page():
    try:
        rows = _build_page_rows()
    except Exception as exc:
        current_app.logger.error(f"4Seller page load error: {exc}")
        rows = []

    rows_sorted = sorted(rows, key=lambda r: r["four_seller_stock"], reverse=True)
    chart_rows = rows_sorted[:30]

    last_updated = None
    if rows:
        dt_values = [r.get("updated_at") for r in rows if r.get("updated_at")]
        if dt_values:
            last_updated = max(dt_values)

    return render_template(
        "4seller.html",
        current_user=session.get("username"),
        rows=rows,
        chart_rows=chart_rows,
        total_rows=len(rows),
        last_updated=last_updated,
        has_cookie_cache=_has_cookie_cache(),
    )


@four_seller_bp.route("/4seller/sync", methods=["POST"])
@login_required
def four_seller_sync():
    payload = request.get_json(silent=True) or {}
    user_cookie = (payload.get("cookie") or request.form.get("cookie") or "").strip()
    cookie_header, from_cache = _resolve_cookie_header(user_cookie)
    if not cookie_header:
        env_cookie = os.getenv("FOURSELLER_COOKIE", "").strip()
        if env_cookie:
            cookie_header, from_cache = env_cookie, False

    if not cookie_header:
        return jsonify({"success": False, "cookie_required": True, "message": "Missing cookie header. Paste a fresh 4Seller cookie."}), 400

    try:
        result = sync_4seller_from_cookie(cookie_header)
        if user_cookie:
            _save_cookie_cache(user_cookie)
        return jsonify(result)
    except Exception as exc:
        error_text = str(exc)
        cookie_invalid = "Validation_Failed" in error_text or "invalid or expired" in error_text.lower()
        if from_cache or cookie_invalid:
            _delete_cookie_cache()
        current_app.logger.error(f"4Seller sync error: {exc}")
        return jsonify({"success": False, "cookie_required": from_cache or cookie_invalid, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/status", methods=["POST"])
@login_required
def four_seller_update_status():
    payload = request.get_json(silent=True) or {}
    stock_sku = str(payload.get("stock_sku", "")).strip()
    status = str(payload.get("status", "")).strip().lower()

    if not stock_sku:
        return jsonify({"success": False, "message": "Missing stock_sku."}), 400
    if status not in {"active", "inactive"}:
        return jsonify({"success": False, "message": "Status must be active or inactive."}), 400

    try:
        _ensure_4seller_table()
        connection = get_mysql_connection()
        if connection is None:
            raise RuntimeError("MySQL connection failed.")

        cursor = connection.cursor()
        cursor.execute(
            "UPDATE `4seller_stock` SET status = %s WHERE stock_sku = %s",
            (status, stock_sku),
        )
        connection.commit()
        changed = cursor.rowcount
        cursor.close()
        connection.close()

        if changed == 0:
            return jsonify({"success": False, "message": "SKU not found."}), 404

        return jsonify({"success": True, "stock_sku": stock_sku, "status": status})
    except Exception as exc:
        current_app.logger.error(f"4Seller status update error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/export", methods=["GET"])
@login_required
def four_seller_export_upload_file():
    """Export current rows into 4Seller upload template format."""
    try:
        rows = _build_page_rows()
        output = BytesIO(_build_upload_xlsx_bytes(rows))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"4seller_upload_{timestamp}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller export file generation error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/export_to_4seller", methods=["POST"])
@login_required
def four_seller_export_to_4seller():
    payload = request.get_json(silent=True) or {}
    user_cookie = (payload.get("cookie") or request.form.get("cookie") or "").strip()
    cookie_header, from_cache = _resolve_cookie_header(user_cookie)
    if not cookie_header:
        env_cookie = os.getenv("FOURSELLER_COOKIE", "").strip()
        if env_cookie:
            cookie_header, from_cache = env_cookie, False

    if not cookie_header:
        return jsonify({"success": False, "cookie_required": True, "message": "Missing cookie header. Paste a fresh 4Seller cookie."}), 400

    try:
        session_client = _build_cookie_session(cookie_header)
        if not _verify_4seller_session(session_client):
            raise RuntimeError("4Seller session invalid or expired.")

        rows = _build_page_rows()
        excel_bytes = _build_upload_xlsx_bytes(rows)
        upload_data = _upload_excel_to_4seller(session_client, excel_bytes)
        async_task = str(upload_data.get("data", "") or "")
        schedule_data = _poll_import_schedule(session_client, async_task)

        if user_cookie:
            _save_cookie_cache(user_cookie)

        return jsonify(
            {
                "success": True,
                "upload": upload_data,
                "schedule": schedule_data,
                "async_task": async_task,
            }
        )
    except Exception as exc:
        error_text = str(exc)
        cookie_invalid = "Validation_Failed" in error_text or "invalid or expired" in error_text.lower()
        if from_cache or cookie_invalid:
            _delete_cookie_cache()
        current_app.logger.error(f"4Seller export_to_4seller error: {exc}")
        return jsonify({"success": False, "cookie_required": from_cache or cookie_invalid, "message": str(exc)}), 500