"""Parse weekly container shipment tracking Excel (2026 sheet) and persist to MySQL."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import pandas as pd

from py.utilities.database_utils import get_mysql_connection

_FDC_REFER_RE = re.compile(r"^FT(\d+)$", re.IGNORECASE)

_CACHE_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "output", "boat_cache")
)
LATEST_TRACKING_BASENAME = "shipment_tracking_latest"
LATEST_TRACKING_XLSX = os.path.join(_CACHE_ROOT, f"{LATEST_TRACKING_BASENAME}.xlsx")
TRACKING_META_JSON = os.path.join(_CACHE_ROOT, "shipment_tracking_meta.json")
_ALLOWED_TRACKING_EXTENSIONS = (".xls", ".xlsx", ".xlsm")


def normalize_container_id(container_val) -> str:
    text = str(container_val or "").strip().upper()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    match = _FDC_REFER_RE.match(text)
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


def _excel_engine(file_path: str) -> str | None:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".xls":
        return "xlrd"
    return None


def latest_tracking_path_for_ext(ext: str) -> str:
    normalized = (ext or ".xlsx").lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    if normalized not in _ALLOWED_TRACKING_EXTENSIONS:
        normalized = ".xlsx"
    return os.path.join(_CACHE_ROOT, f"{LATEST_TRACKING_BASENAME}{normalized}")


def find_saved_tracking_path() -> str:
    for ext in _ALLOWED_TRACKING_EXTENSIONS:
        path = latest_tracking_path_for_ext(ext)
        if os.path.isfile(path):
            return path
    return ""


def has_saved_tracking_file() -> bool:
    return bool(find_saved_tracking_path())


def _find_tracking_header_row(raw: pd.DataFrame) -> int:
    for idx in range(min(25, len(raw))):
        cells = [
            str(v).strip().lower()
            for v in raw.iloc[idx].tolist()
            if v is not None and not (isinstance(v, float) and pd.isna(v))
        ]
        joined = " ".join(cells)
        if "refer no" in joined and ("vessel" in joined or "containter" in joined or "container" in joined):
            return idx
    raise ValueError("Could not find the tracking header row on the 2026 sheet.")


def _load_2026_tracking_dataframe(file_path: str, sheet_name: str, engine: str | None) -> pd.DataFrame:
    raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None, engine=engine)
    header_row = _find_tracking_header_row(raw)
    df = pd.read_excel(file_path, sheet_name=sheet_name, header=header_row, engine=engine)
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _find_eta_column(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        label = str(col).strip().lower()
        if "eta" in label and "etd" not in label:
            return col
    return None


def parse_shipment_tracking_workbook(file_path: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse the 2026 schedule sheet. Column A (REFER NO) = FT261112 -> F261112."""
    engine = _excel_engine(file_path)
    workbook = pd.ExcelFile(file_path, engine=engine)
    sheet_name = _find_2026_sheet(workbook.sheet_names)
    df = _load_2026_tracking_dataframe(file_path, sheet_name, engine)
    if df.empty:
        return sheet_name, []

    container_col = _find_column(df, ("refer no", "fdc container", "container")) or df.columns[0]
    vessel_col = _find_column(df, ("vesso", "vessel no", "vessel"))
    container_no_col = _find_column(df, ("containter no", "container no", "container #", "container#"))
    deliver_col = _find_column(df, ("deliver date", "deliver", "delivery", "rcd"))
    etd_col = _find_column(df, ("etd",))
    eta_col = _find_eta_column(df)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, record in df.iterrows():
        raw_ref = _cell_text(record.get(container_col))
        if not _FDC_REFER_RE.match(raw_ref):
            continue
        container_id = normalize_container_id(raw_ref)
        if not container_id or container_id in seen:
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

    ext = os.path.splitext(original_filename)[1].lower() or ".xlsx"
    dest = latest_tracking_path_for_ext(ext)
    if os.path.normpath(os.path.abspath(source_path)) == os.path.normpath(os.path.abspath(dest)):
        return dest
    for other_ext in _ALLOWED_TRACKING_EXTENSIONS:
        old = latest_tracking_path_for_ext(other_ext)
        if old != dest and os.path.isfile(old):
            os.remove(old)
    shutil.copy2(source_path, dest)
    return dest


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
    saved_path = find_saved_tracking_path()
    if not saved_path:
        return "", []
    return parse_shipment_tracking_workbook(saved_path)


def load_shipment_tracking_lookup(container_ids: list[str] | None = None) -> dict[str, dict[str, str]]:
    """Load weekly tracking fields from MySQL keyed by container id."""
    conn = get_mysql_connection()
    if not conn:
        return {}

    ids = sorted({normalize_container_id(c) for c in (container_ids or []) if normalize_container_id(c)})
    if not ids:
        return {}

    cursor = conn.cursor(dictionary=True)
    try:
        ensure_shipment_tracking_table(cursor)
        placeholders = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            SELECT container_id, container_no, vessel_no, deliver_date, etd, eta
            FROM container_shipment_tracking
            WHERE container_id IN ({placeholders})
            """,
            ids,
        )
        lookup: dict[str, dict[str, str]] = {}
        for row in cursor.fetchall():
            key = normalize_container_id(row.get("container_id"))
            if not key:
                continue
            lookup[key] = {
                "container_no": str(row.get("container_no") or "").strip(),
                "vessel_no": str(row.get("vessel_no") or "").strip(),
                "deliver_date": str(row.get("deliver_date") or "").strip(),
                "etd": str(row.get("etd") or "").strip(),
                "eta": str(row.get("eta") or "").strip(),
            }
        return lookup
    finally:
        cursor.close()
        conn.close()


def reprocess_saved_shipment_tracking(
    *,
    uploaded_by: str,
) -> dict[str, Any]:
    """Re-parse the cached tracking workbook and refresh MySQL."""
    saved_path = find_saved_tracking_path()
    if not saved_path:
        raise ValueError("No uploaded tracking file found.")
    meta = load_tracking_meta()
    original_filename = str(meta.get("original_filename") or os.path.basename(saved_path))
    return process_shipment_tracking_upload(
        saved_path,
        original_filename=original_filename,
        uploaded_by=uploaded_by,
    )


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
