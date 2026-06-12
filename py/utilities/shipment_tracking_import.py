"""Parse weekly container shipment tracking Excel (2026 sheet) and persist to MySQL."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import pandas as pd

from py.utilities.database_utils import get_mysql_connection

_FT_CONTAINER_RE = re.compile(r"^FT(\d+)$", re.IGNORECASE)

_CACHE_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "output", "boat_cache")
)
LATEST_TRACKING_XLSX = os.path.join(_CACHE_ROOT, "shipment_tracking_latest.xlsx")
TRACKING_META_JSON = os.path.join(_CACHE_ROOT, "shipment_tracking_meta.json")


def normalize_container_id(container_val) -> str:
    text = str(container_val or "").strip().upper()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    match = _FT_CONTAINER_RE.match(text)
    if match:
        return f"F{match.group(1)}"
    return text


def _ensure_cache_dir() -> None:
    os.makedirs(_CACHE_ROOT, exist_ok=True)


def _format_date_value(raw) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    if isinstance(raw, datetime):
        return raw.strftime("%m/%d/%Y")
    if isinstance(raw, pd.Timestamp):
        return raw.strftime("%m/%d/%Y")
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    try:
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.notna(parsed):
            return parsed.strftime("%m/%d/%Y")
    except Exception:
        pass
    return text


def _find_2026_sheet(sheet_names: list[str]) -> str:
    matches = [name for name in sheet_names if "2026" in str(name)]
    if not matches:
        for name in sheet_names:
            if str(name).strip() == "2026":
                return name
        raise ValueError("No worksheet containing '2026' was found in this file.")
    return matches[-1]


def _find_column(df: pd.DataFrame, needles: tuple[str, ...]) -> str | None:
    for col in df.columns:
        label = str(col).strip().lower()
        for needle in needles:
            if needle in label:
                return col
    return None


def _cell_text(raw) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    return str(raw).strip()


def parse_shipment_tracking_workbook(file_path: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse the latest 2026 sheet. Container id is column A (first column)."""
    workbook = pd.ExcelFile(file_path)
    sheet_name = _find_2026_sheet(workbook.sheet_names)
    df = pd.read_excel(file_path, sheet_name=sheet_name, header=0)
    if df.empty:
        return sheet_name, []

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    container_col = df.columns[0]
    vessel_col = _find_column(df, ("vesso", "vessel"))
    container_no_col = _find_column(df, ("container no", "container #", "container#", "bol"))
    deliver_col = _find_column(df, ("deliver", "delivery", "rcd"))
    etd_col = _find_column(df, ("etd",))
    eta_col = None
    for col in df.columns:
        label = str(col).strip().lower()
        if "eta" in label and "etd" not in label:
            eta_col = col
            break

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, record in df.iterrows():
        container_id = normalize_container_id(record.get(container_col))
        if not container_id or container_id in seen:
            continue
        if not re.match(r"^[A-Z]\d", container_id):
            continue
        seen.add(container_id)

        vessel_no = _cell_text(record.get(vessel_col)) if vessel_col else ""
        container_no = _cell_text(record.get(container_no_col)) if container_no_col else ""
        deliver_date = _format_date_value(record.get(deliver_col)) if deliver_col else ""
        etd = _format_date_value(record.get(etd_col)) if etd_col else ""
        eta = _format_date_value(record.get(eta_col)) if eta_col else ""

        rows.append(
            {
                "container_id": container_id,
                "container_no": container_no,
                "vessel_no": vessel_no,
                "deliver_date": deliver_date,
                "etd": etd,
                "eta": eta,
            }
        )
    return sheet_name, rows


def ensure_shipment_tracking_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS container_shipment_tracking (
            container_id VARCHAR(32) NOT NULL PRIMARY KEY,
            container_no VARCHAR(64) NULL,
            vessel_no VARCHAR(128) NULL,
            deliver_date VARCHAR(32) NULL,
            etd VARCHAR(32) NULL,
            eta VARCHAR(32) NULL,
            source_file VARCHAR(255) NULL,
            uploaded_by VARCHAR(64) NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def save_tracking_rows_to_mysql(
    rows: list[dict[str, Any]],
    *,
    source_file: str,
    uploaded_by: str,
) -> int:
    conn = get_mysql_connection()
    if not conn:
        raise RuntimeError("MySQL connection failed")

    cursor = conn.cursor()
    try:
        ensure_shipment_tracking_table(cursor)
        saved = 0
        for row in rows:
            cursor.execute(
                """
                INSERT INTO container_shipment_tracking (
                    container_id, container_no, vessel_no, deliver_date, etd, eta,
                    source_file, uploaded_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    container_no = VALUES(container_no),
                    vessel_no = VALUES(vessel_no),
                    deliver_date = VALUES(deliver_date),
                    etd = VALUES(etd),
                    eta = VALUES(eta),
                    source_file = VALUES(source_file),
                    uploaded_by = VALUES(uploaded_by),
                    uploaded_at = CURRENT_TIMESTAMP
                """,
                (
                    row["container_id"],
                    row.get("container_no") or None,
                    row.get("vessel_no") or None,
                    row.get("deliver_date") or None,
                    row.get("etd") or None,
                    row.get("eta") or None,
                    source_file,
                    uploaded_by,
                ),
            )
            saved += 1
        conn.commit()
        return saved
    finally:
        cursor.close()
        conn.close()


def save_uploaded_workbook(source_path: str, *, original_filename: str) -> str:
    _ensure_cache_dir()
    import shutil

    shutil.copy2(source_path, LATEST_TRACKING_XLSX)
    return LATEST_TRACKING_XLSX


def save_tracking_meta(
    *,
    original_filename: str,
    sheet_name: str,
    row_count: int,
    uploaded_by: str,
    matched_sheet_rows: int,
) -> dict[str, Any]:
    payload = {
        "original_filename": original_filename,
        "sheet_name": sheet_name,
        "row_count": row_count,
        "matched_sheet_rows": matched_sheet_rows,
        "uploaded_by": uploaded_by,
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _ensure_cache_dir()
    with open(TRACKING_META_JSON, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return payload


def load_tracking_meta() -> dict[str, Any]:
    if not os.path.isfile(TRACKING_META_JSON):
        return {}
    with open(TRACKING_META_JSON, encoding="utf-8") as handle:
        return json.load(handle)


def load_saved_tracking_rows() -> tuple[str, list[dict[str, Any]]]:
    if not os.path.isfile(LATEST_TRACKING_XLSX):
        return "", []
    return parse_shipment_tracking_workbook(LATEST_TRACKING_XLSX)


def process_shipment_tracking_upload(
    temp_path: str,
    *,
    original_filename: str,
    uploaded_by: str,
) -> dict[str, Any]:
    sheet_name, rows = parse_shipment_tracking_workbook(temp_path)
    if not rows:
        raise ValueError(f"No container rows found on sheet '{sheet_name}'.")

    save_uploaded_workbook(temp_path, original_filename=original_filename)
    mysql_saved = save_tracking_rows_to_mysql(
        rows,
        source_file=original_filename,
        uploaded_by=uploaded_by,
    )
    return {
        "sheet_name": sheet_name,
        "rows": rows,
        "mysql_saved": mysql_saved,
    }
