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
from py.search.item_lookup import fetch_locations

try:
    from py.utilities.fourseller_login import get_mfa_status, login_from_env, submit_manual_mfa_code
except ImportError:
    login_from_env = None  # type: ignore[misc, assignment]
    get_mfa_status = None  # type: ignore[misc, assignment]
    submit_manual_mfa_code = None  # type: ignore[misc, assignment]


four_seller_bp = Blueprint("four_seller", __name__)


SKU_EXPORT_URL = "https://www.4seller.com/api/sku-inventory/export"
SKU_TASK_URL = "https://www.4seller.com/api/sku-inventory/task"
DASHBOARD_URL = "https://www.4seller.com/web/dashboard.html"
USER_INIT_URL = "https://www.4seller.com/api/user/user-init"

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

ZERO_STOCK_EXPORT_PAYLOAD = {
    **DEFAULT_EXPORT_PAYLOAD,
    "onHandMin": "0",
    "onHandMax": "0",
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
    return _load_cookie_cache_meta().get("cookie", "") or ""


def _load_cookie_cache_meta() -> dict[str, Any]:
    if not os.path.exists(COOKIE_CACHE_FILE):
        return {"cookie": "", "saved_at": None}
    try:
        with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {
            "cookie": str(payload.get("cookie", "") or "").strip(),
            "saved_at": payload.get("saved_at"),
        }
    except Exception:
        return {"cookie": "", "saved_at": None}


def _delete_cookie_cache() -> None:
    try:
        if os.path.exists(COOKIE_CACHE_FILE):
            os.remove(COOKIE_CACHE_FILE)
    except Exception:
        pass


def _has_cookie_cache() -> bool:
    return bool(_load_cookie_cache())


def _credentials_configured() -> bool:
    from py.utilities.nssm_env import load_nssm_env_file

    load_nssm_env_file()
    return bool(os.getenv("FOURSELLER_EMAIL", "").strip() and os.getenv("FOURSELLER_PASSWORD", "").strip())


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
    session.headers["Referer"] = "https://www.4seller.com/web/dashboard.html"
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


def _auto_login_cookie() -> str:
    """Log in with FOURSELLER_EMAIL / FOURSELLER_PASSWORD and cache the session cookie."""
    cookie_header = _login_via_subprocess()
    if cookie_header and _cookie_header_has_api_token(cookie_header):
        _save_cookie_cache(cookie_header)
        return cookie_header
    if cookie_header:
        _delete_cookie_cache()

    if login_from_env is None:
        return ""
    try:
        result = login_from_env()
    except Exception as exc:
        current_app.logger.error(f"4Seller auto-login failed: {exc}")
        return ""

    cookie_header = str(result.get("cookie_header", "") or "").strip()
    if cookie_header and _cookie_header_has_api_token(cookie_header):
        _save_cookie_cache(cookie_header)
        return cookie_header
    return ""


def _login_via_subprocess() -> str:
    """Run login in a clean Python process (ddddocr breaks after pandas/opencv import)."""
    import subprocess
    import sys

    worker = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "4seller_login_worker.py")
    if not os.path.isfile(worker):
        return ""
    try:
        completed = subprocess.run(
            [sys.executable, worker],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=os.path.dirname(os.path.dirname(worker)),
            check=False,
        )
        if completed.returncode != 0:
            current_app.logger.error(
                "4Seller login worker failed: %s",
                (completed.stderr or completed.stdout or "").strip()[:500],
            )
            return ""
        import json as _json

        payload = _json.loads((completed.stdout or "").strip().splitlines()[-1])
        if not payload.get("success"):
            return ""
        return str(payload.get("cookie_header", "") or "").strip()
    except Exception as exc:
        current_app.logger.error(f"4Seller login worker error: {exc}")
        return ""


def _resolve_cookie_header_with_auto_login(user_cookie: str) -> tuple[str, bool]:
    """Resolve cookie from user input, cache, env, or automated login."""
    from py.utilities.nssm_env import load_nssm_env_file

    load_nssm_env_file()

    cookie_header, from_cache = _resolve_cookie_header(user_cookie)
    if cookie_header and not _cookie_header_has_api_token(cookie_header):
        current_app.logger.warning("4Seller cached cookie uses wrong token format; refreshing.")
        _delete_cookie_cache()
        cookie_header = ""
        from_cache = False

    if cookie_header:
        return cookie_header, from_cache

    env_cookie = os.getenv("FOURSELLER_COOKIE", "").strip()
    if env_cookie and _cookie_header_has_api_token(env_cookie):
        return env_cookie, False

    auto_cookie = _auto_login_cookie()
    if auto_cookie:
        return auto_cookie, True

    return "", False


def _is_api_session_token(token: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", (token or "").strip(), flags=re.IGNORECASE))


def _cookie_header_has_api_token(cookie_header: str) -> bool:
    cookies = _parse_cookie_header(cookie_header)
    return _is_api_session_token(str(cookies.get("userToken", "") or ""))


def _verify_4seller_session(session: requests.Session) -> bool:
    """Verify via API — dashboard HTML loads even when API cookies are invalid."""
    try:
        response = session.post(USER_INIT_URL, json={}, timeout=30)
        data = _safe_json(response)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("code") == 4001:
        return False
    msg = str(data.get("msg") or data.get("messages") or "")
    return "Validation_Failed" not in msg


def _token_preview(cookie_header: str) -> str:
    cookies = _parse_cookie_header(cookie_header) if cookie_header else {}
    token = str(cookies.get("userToken", "") or "")
    if not token:
        return ""
    if len(token) <= 12:
        return token[:4] + "…" if len(token) > 4 else token
    return f"{token[:6]}…{token[-4:]}"


def _inspect_session_status(*, attempt_login: bool = False) -> dict[str, Any]:
    """Return cookie + 4Seller login status for the dashboard status panel."""
    from py.utilities.nssm_env import load_nssm_env_file

    load_nssm_env_file()
    auto_configured = _credentials_configured()
    env_cookie = os.getenv("FOURSELLER_COOKIE", "").strip()
    cache_meta = _load_cookie_cache_meta()

    cookie_header = ""
    source = "none"
    if cache_meta.get("cookie"):
        cookie_header = str(cache_meta["cookie"])
        source = "cache"
    elif env_cookie:
        cookie_header = env_cookie
        source = "env"

    session_valid: bool | None = None
    if cookie_header:
        try:
            session_valid = _verify_4seller_session(_build_cookie_session(cookie_header))
        except Exception as exc:
            session_valid = False
            verify_error = str(exc)
        else:
            verify_error = ""
    else:
        verify_error = ""

    if session_valid:
        login_status = "connected"
        login_message = "Connected to 4Seller."
    elif not cookie_header:
        login_status = "no_cookie"
        login_message = "No cookie stored yet."
    elif session_valid is False:
        login_status = "expired"
        login_message = verify_error or "Cookie expired or invalid."
    else:
        login_status = "unknown"
        login_message = "Session not checked."

    login_attempted = False
    if attempt_login and not session_valid and auto_configured:
        login_attempted = True
        login_status = "logging_in"
        login_message = "Logging in to 4Seller (captcha OCR)…"
        refreshed = _auto_login_cookie()
        if refreshed:
            cookie_header = refreshed
            source = "cache"
            cache_meta = _load_cookie_cache_meta()
            try:
                session_valid = _verify_4seller_session(_build_cookie_session(cookie_header))
            except Exception as exc:
                session_valid = False
                verify_error = str(exc)
            if session_valid:
                login_status = "connected"
                login_message = "Logged in successfully; cookie cached."
            else:
                login_status = "failed"
                login_message = verify_error or "Login returned a cookie but session verify failed."
        else:
            login_status = "failed"
            login_message = "Auto-login failed. Check server logs or FOURSELLER_EMAIL/PASSWORD."

    cookie_valid_label: str | None
    if session_valid is True:
        cookie_valid_label = "valid"
    elif session_valid is False:
        cookie_valid_label = "invalid"
    else:
        cookie_valid_label = None

    return {
        "auto_login_configured": auto_configured,
        "cookie": {
            "present": bool(cookie_header),
            "source": source,
            "saved_at": cache_meta.get("saved_at"),
            "valid": session_valid,
            "valid_label": cookie_valid_label,
            "token_preview": _token_preview(cookie_header),
        },
        "login": {
            "status": login_status,
            "message": login_message,
            "attempted": login_attempted,
        },
    }


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

    # One-time cleanup: mirrored stock SKUs with dashes → space (e.g. S1431-CF → S1431 CF).
    # Skip rows that were already customized (internal_sku != stock_sku).
    cursor.execute(
        """
        UPDATE `4seller_stock`
        SET internal_sku = REPLACE(stock_sku, '-', ' ')
        WHERE internal_sku = stock_sku
          AND stock_sku LIKE '%-%'
        """
    )

    connection.commit()
    cursor.close()
    connection.close()


def _default_internal_sku(stock_sku: str) -> str:
    """Map 4Seller stock SKU to our internal SKU (dashes → spaces in almost all cases)."""
    return str(stock_sku or "").strip().replace("-", " ")


def _request_export_file_url(
    session: requests.Session,
    payload: dict[str, Any] | None = None,
) -> str:
    export_response = session.post(
        SKU_EXPORT_URL,
        json=payload or DEFAULT_EXPORT_PAYLOAD,
        timeout=30,
    )
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

    # 4Seller exports can repeat the same Stock SKU (e.g. multiple warehouses).
    # Aggregate On Hand so we never violate uniq_stock_sku.
    aggregated: dict[str, int] = {}
    for _, row in df.iterrows():
        stock_sku = str(row.get("Stock SKU", "")).strip()
        if not stock_sku or stock_sku.lower() == "nan":
            continue
        aggregated[stock_sku] = aggregated.get(stock_sku, 0) + _to_int(row.get("On Hand", 0))

    rows: list[tuple[str, str, int]] = []
    for stock_sku, on_hand in aggregated.items():
        rows.append((stock_sku, _default_internal_sku(stock_sku), on_hand))

    return rows


def _replace_4seller_stock(rows: list[tuple[str, str, int]]) -> int:
    _ensure_4seller_table()

    connection = get_mysql_connection()
    if connection is None:
        raise RuntimeError("MySQL connection failed.")

    cursor = connection.cursor()

    cursor.execute("SELECT stock_sku, internal_sku, status FROM `4seller_stock`")
    existing_map: dict[str, tuple[str, str]] = {}
    for sku, internal_sku, status in cursor.fetchall():
        existing_map[str(sku)] = (
            str(internal_sku or "").strip(),
            str(status or "active").lower(),
        )

    cursor.execute("DELETE FROM `4seller_stock`")

    insert_sql = (
        "INSERT INTO `4seller_stock` (stock_sku, internal_sku, `4seller_stock`, status) "
        "VALUES (%s, %s, %s, %s)"
    )
    if rows:
        # Final safety net against duplicate stock_sku in the payload.
        merged: dict[str, tuple[str, int, str]] = {}
        for stock_sku, internal_sku, four_seller_stock in rows:
            prev_internal, status = existing_map.get(stock_sku, ("", "active"))
            if status not in {"active", "inactive"}:
                status = "active"
            # Keep manual overrides; re-apply dash→space when still mirrored to stock SKU.
            if prev_internal and prev_internal != stock_sku:
                resolved_internal = prev_internal
            else:
                resolved_internal = _default_internal_sku(stock_sku)

            if stock_sku in merged:
                prev_resolved, prev_qty, prev_status = merged[stock_sku]
                merged[stock_sku] = (prev_resolved, prev_qty + int(four_seller_stock or 0), prev_status)
            else:
                merged[stock_sku] = (resolved_internal, int(four_seller_stock or 0), status)

        rows_with_status = [
            (stock_sku, internal_sku, qty, status)
            for stock_sku, (internal_sku, qty, status) in merged.items()
        ]
        cursor.executemany(insert_sql, rows_with_status)

    connection.commit()
    count = len(rows_with_status) if rows else 0
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

    regular_file_url = _request_export_file_url(session_client, DEFAULT_EXPORT_PAYLOAD)
    zero_stock_file_url = _request_export_file_url(session_client, ZERO_STOCK_EXPORT_PAYLOAD)

    regular_rows = _load_rows_from_xlsx_bytes(_download_export_bytes(regular_file_url))
    zero_stock_rows = _load_rows_from_xlsx_bytes(_download_export_bytes(zero_stock_file_url))
    rows = regular_rows + zero_stock_rows
    inserted = _replace_4seller_stock(rows)

    return {
        "success": True,
        "message": (
            f"Imported {inserted} SKU rows from 4Seller exports "
            f"({len(regular_rows)} regular, {len(zero_stock_rows)} zero-stock)."
        ),
        "file_url": regular_file_url,
        "file_urls": {
            "regular": regular_file_url,
            "zero_stock": zero_stock_file_url,
        },
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
        auto_login_configured=_credentials_configured(),
    )


@four_seller_bp.route("/4seller/session-status", methods=["GET"])
@login_required
def four_seller_session_status():
    """Fast check of cached cookie and 4Seller session (no auto-login)."""
    try:
        return jsonify({"success": True, **_inspect_session_status(attempt_login=False)})
    except Exception as exc:
        current_app.logger.error(f"4Seller session-status error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/warm-session", methods=["POST"])
@login_required
def four_seller_warm_session():
    """Verify session; auto-login if credentials are configured and cookie is missing/invalid."""
    try:
        quick = _inspect_session_status(attempt_login=False)
        if quick.get("cookie", {}).get("valid"):
            return jsonify({"success": True, **quick})
        warmed = _inspect_session_status(attempt_login=True)
        return jsonify({"success": True, **warmed})
    except Exception as exc:
        current_app.logger.error(f"4Seller warm-session error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/mfa-status", methods=["GET"])
@login_required
def four_seller_mfa_status():
    """Whether an auto-login is currently waiting for a 4Seller email 2FA code."""
    if get_mfa_status is None:
        return jsonify({"success": True, "pending": False})
    return jsonify({"success": True, **get_mfa_status()})


@four_seller_bp.route("/4seller/mfa-code", methods=["POST"])
@login_required
def four_seller_mfa_code():
    """Hand a manually entered 2FA code to the waiting auto-login."""
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code") or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z]{4,10}", code):
        return jsonify({"success": False, "message": "Enter the 4-10 character code from the email."}), 400
    if submit_manual_mfa_code is None or not submit_manual_mfa_code(code):
        return jsonify({"success": False, "message": "No 4Seller login is waiting for a code right now."}), 409
    return jsonify({"success": True, "message": "Code submitted. Verifying with 4Seller…"})


@four_seller_bp.route("/4seller/sync", methods=["POST"])
@login_required
def four_seller_sync():
    payload = request.get_json(silent=True) or {}
    user_cookie = (payload.get("cookie") or request.form.get("cookie") or "").strip()
    cookie_header, from_cache = _resolve_cookie_header_with_auto_login(user_cookie)

    if not cookie_header:
        return jsonify({"success": False, "cookie_required": True, "message": "Missing cookie header. Paste a fresh 4Seller cookie or set FOURSELLER_EMAIL/FOURSELLER_PASSWORD."}), 400

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
            if cookie_invalid and not user_cookie:
                try:
                    refreshed = _auto_login_cookie()
                    if refreshed:
                        result = sync_4seller_from_cookie(refreshed)
                        return jsonify(result)
                except Exception as retry_exc:
                    current_app.logger.error(f"4Seller auto-login retry failed: {retry_exc}")
        need_manual_cookie = (from_cache or cookie_invalid) and not _credentials_configured()
        current_app.logger.error(f"4Seller sync error: {exc}")
        return jsonify({"success": False, "cookie_required": need_manual_cookie, "message": str(exc)}), 500


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


@four_seller_bp.route("/4seller/internal-sku", methods=["POST"])
@login_required
def four_seller_update_internal_sku():
    payload = request.get_json(silent=True) or {}
    stock_sku = str(payload.get("stock_sku", "")).strip()
    internal_sku = str(payload.get("internal_sku", "")).strip()

    if not stock_sku:
        return jsonify({"success": False, "message": "Missing stock_sku."}), 400
    if not internal_sku:
        return jsonify({"success": False, "message": "Internal SKU cannot be empty."}), 400
    if len(internal_sku) > 120:
        return jsonify({"success": False, "message": "Internal SKU is too long."}), 400

    try:
        _ensure_4seller_table()
        connection = get_mysql_connection()
        if connection is None:
            raise RuntimeError("MySQL connection failed.")

        cursor = connection.cursor()
        cursor.execute(
            "UPDATE `4seller_stock` SET internal_sku = %s WHERE stock_sku = %s",
            (internal_sku, stock_sku),
        )
        connection.commit()
        changed = cursor.rowcount
        cursor.close()
        connection.close()

        if changed == 0:
            return jsonify({"success": False, "message": "SKU not found."}), 404

        return jsonify({"success": True, "stock_sku": stock_sku, "internal_sku": internal_sku})
    except Exception as exc:
        current_app.logger.error(f"4Seller internal SKU update error: {exc}")
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
    cookie_header, from_cache = _resolve_cookie_header_with_auto_login(user_cookie)

    if not cookie_header:
        return jsonify({"success": False, "cookie_required": True, "message": "Missing cookie header. Paste a fresh 4Seller cookie or set FOURSELLER_EMAIL/FOURSELLER_PASSWORD."}), 400

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
            if cookie_invalid and not user_cookie:
                try:
                    refreshed = _auto_login_cookie()
                    if refreshed:
                        session_client = _build_cookie_session(refreshed)
                        if _verify_4seller_session(session_client):
                            rows = _build_page_rows()
                            excel_bytes = _build_upload_xlsx_bytes(rows)
                            upload_data = _upload_excel_to_4seller(session_client, excel_bytes)
                            async_task = str(upload_data.get("data", "") or "")
                            schedule_data = _poll_import_schedule(session_client, async_task)
                            return jsonify(
                                {
                                    "success": True,
                                    "upload": upload_data,
                                    "schedule": schedule_data,
                                    "async_task": async_task,
                                }
                            )
                except Exception as retry_exc:
                    current_app.logger.error(f"4Seller auto-login export retry failed: {retry_exc}")
        need_manual_cookie = (from_cache or cookie_invalid) and not _credentials_configured()
        current_app.logger.error(f"4Seller export_to_4seller error: {exc}")
        return jsonify({"success": False, "cookie_required": need_manual_cookie, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/refresh-cookie", methods=["POST"])
@login_required
def four_seller_refresh_cookie():
    """Force a fresh 4Seller login and update the cached cookie."""
    _delete_cookie_cache()
    try:
        cookie_header = _auto_login_cookie()
        if not cookie_header:
            return jsonify(
                {
                    "success": False,
                    "message": "Auto-login not configured. Set FOURSELLER_EMAIL and FOURSELLER_PASSWORD.",
                }
            ), 400
        valid = _verify_4seller_session(_build_cookie_session(cookie_header))
        return jsonify(
            {
                "success": True,
                "message": "4Seller cookie refreshed." if valid else "Logged in; session verify returned false (cookie saved).",
                "session_valid": valid,
            }
        )
    except Exception as exc:
        current_app.logger.error(f"4Seller refresh-cookie error: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


@four_seller_bp.route("/4seller/picklist-locations", methods=["GET"])
def four_seller_picklist_locations():
    """Resolve warehouse locations for 4Seller stock SKUs (Chrome extension picklist helper)."""
    skus_param = request.args.get("skus", "").strip()
    if not skus_param:
        return jsonify({"error": "skus parameter is required"}), 400

    stock_skus = [s.strip() for s in skus_param.split(",") if s.strip()]
    if not stock_skus:
        return jsonify({"error": "No valid SKUs provided"}), 400

    lookup_skus: list[str] = []
    stock_to_internal: dict[str, str] = {}

    try:
        _ensure_4seller_table()
        connection = get_mysql_connection()
        if connection is not None:
            cursor = connection.cursor(dictionary=True)
            placeholders = ",".join(["%s"] * len(stock_skus))
            cursor.execute(
                f"""
                SELECT stock_sku, internal_sku
                FROM `4seller_stock`
                WHERE stock_sku IN ({placeholders})
                """,
                stock_skus,
            )
            for row in cursor.fetchall():
                stock = str(row.get("stock_sku", "")).strip()
                internal = str(row.get("internal_sku", "")).strip() or stock
                stock_to_internal[stock] = internal
                lookup_skus.append(internal)
            cursor.close()
            connection.close()
    except Exception as exc:
        current_app.logger.warning(f"4Seller stock SKU mapping lookup failed: {exc}")

    for stock in stock_skus:
        if stock not in stock_to_internal:
            stock_to_internal[stock] = stock
            lookup_skus.append(stock)

    unique_lookup = list(dict.fromkeys(s for s in lookup_skus if s))
    location_by_internal = fetch_locations(unique_lookup)

    import re

    def alnum(value: str) -> str:
        return re.sub(r"[^0-9a-z]", "", value.lower())

    def resolve_location(sku: str) -> str:
        for key in (sku.lower(), sku.lower().replace(" ", ""), alnum(sku)):
            loc = location_by_internal.get(key)
            if loc:
                return loc
        return "N/A"

    result: dict[str, str] = {}
    for stock in stock_skus:
        internal = stock_to_internal.get(stock, stock)
        loc = resolve_location(internal)
        result[stock] = loc
        result[stock.lower()] = loc
        result[stock.lower().replace(" ", "")] = loc
        result[alnum(stock)] = loc

    return jsonify(result)