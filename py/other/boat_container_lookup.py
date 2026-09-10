"""Find latest FDC container per SKU using /boat status rules and container CSV exports."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

from py.Toolkits.boat import GOOGLE_SHEETS_URL, OUTPUT_DIR, get_db_connection

_CACHE_TTL_SEC = 300
_sku_index_cache: dict[str, Any] = {"built_at": 0.0, "by_sku": {}}


def _sku_match_key(sku: str) -> str:
    return re.sub(r"[\s\-]+", " ", (sku or "").replace("\u00a0", " ").strip().upper())


def _status_label_from_boat(raw_status: str, *, dennis_eta: str = "", rcd: str = "") -> str:
    """Map boat Status column values to arrived / ocean / missing."""
    s = (raw_status or "").strip()
    if s == "✔️827✔️":
        return "arrived"
    if s == "🚢ocean🚢":
        return "ocean"
    if s == "⚓port⚓":
        return "ocean"
    if dennis_eta and not rcd:
        return "ocean"
    if s == "🔄upload🔄" or not s:
        return "missing"
    return "missing"


def _load_container_status_map() -> dict[str, dict[str, str]]:
    """
    Container id -> {status, status_raw, dennis_eta, rcd}.
    Uses the same Google Sheet + bin_pur rules as /boat/data.
    """
    result: dict[str, dict[str, str]] = {}
    try:
        df = pd.read_csv(GOOGLE_SHEETS_URL)
    except Exception:
        return result

    if "FDC container" not in df.columns:
        return result

    cols = ["FDC container", "ETA", "RCD"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    if "ATA" in df.columns:
        cols.append("ATA")

    df_cleaned = df[cols].copy()
    df_cleaned = df_cleaned.rename(
        columns={"FDC container": "container", "ETA": "Dennis ETA", "RCD": "RCD"}
    )
    df_cleaned["container"] = df_cleaned["container"].astype(str).str.strip()
    df_cleaned = df_cleaned[df_cleaned["container"].notna() & (df_cleaned["container"] != "")]
    df_cleaned = df_cleaned.tail(60).sort_values(by="container", ascending=False)

    containers = df_cleaned["container"].tolist()
    if not containers:
        return result

    rcvd_lookup: dict[str, float] = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["?" for _ in containers])
        cursor.execute(
            f"""
            SELECT PUR_NUM, SUM(RCVD_QTY) AS total_rcvd
            FROM dbo.bin_pur
            WHERE PUR_NUM IN ({placeholders})
              AND pur_cd = 1
            GROUP BY PUR_NUM
            """,
            containers,
        )
        rcvd_lookup = {
            str(row[0]).strip(): float(row[1] or 0) for row in cursor.fetchall()
        }
        cursor.close()
        conn.close()
    except Exception:
        rcvd_lookup = {}

    for _, row in df_cleaned.iterrows():
        container = str(row["container"]).strip()
        dennis_eta = str(row.get("Dennis ETA") or "").strip()
        rcd = str(row.get("RCD") or "").strip()
        if pd.isna(row.get("Dennis ETA")):
            dennis_eta = ""
        if pd.isna(row.get("RCD")):
            rcd = ""

        rcvd_qty = rcvd_lookup.get(container)
        if rcvd_qty is not None and rcvd_qty > 0:
            raw = "✔️827✔️"
        elif rcvd_qty is not None and rcvd_qty == 0:
            raw = "🚢ocean🚢"
        else:
            raw = "🔄upload🔄"

        if raw != "✔️827✔️" and dennis_eta and not rcd:
            raw = "🚢ocean🚢"

        result[container] = {
            "status": _status_label_from_boat(raw, dennis_eta=dennis_eta, rcd=rcd),
            "status_raw": raw,
            "dennis_eta": dennis_eta,
            "rcd": rcd,
        }

    return result


def _container_sort_rank(container: str, order_list: list[str]) -> int:
    try:
        return order_list.index(container)
    except ValueError:
        return len(order_list) + 1


def _read_skus_from_container_csv(path: Path) -> set[str]:
    keys: set[str] = set()
    try:
        df = pd.read_csv(path, dtype=str, low_memory=False)
    except Exception:
        return keys
    item_col = None
    for col in df.columns:
        if str(col).strip().lower() in ("item#", "item"):
            item_col = col
            break
    if not item_col:
        return keys
    for val in df[item_col].dropna():
        key = _sku_match_key(str(val))
        if key:
            keys.add(key)
    return keys


def _build_sku_container_index() -> dict[str, dict[str, str]]:
    status_map = _load_container_status_map()
    container_order = list(status_map.keys())
    if not container_order:
        try:
            df = pd.read_csv(GOOGLE_SHEETS_URL)
            container_order = (
                df["FDC container"].dropna().astype(str).str.strip().tolist()
            )
            container_order = sorted(set(container_order), reverse=True)
        except Exception:
            container_order = []

    by_sku: dict[str, dict[str, str]] = {}
    output = Path(OUTPUT_DIR)
    if not output.is_dir():
        return by_sku

    csv_jobs: list[tuple[str, Path]] = []
    for po_path in output.glob("*-PO.csv"):
        name = po_path.name
        if not name.endswith("-PO.csv"):
            continue
        container = name[: -len("-PO.csv")].strip()
        if container:
            csv_jobs.append((container, po_path))

    for container, po_path in sorted(
        csv_jobs, key=lambda x: _container_sort_rank(x[0], container_order)
    ):
        st = status_map.get(container, {})
        status_label = st.get("status") or "missing"
        for sku_key in _read_skus_from_container_csv(po_path):
            if sku_key in by_sku:
                continue
            by_sku[sku_key] = {
                "container": container,
                "container_status": status_label,
            }

    for styles_path in output.glob("*-styles.csv"):
        name = styles_path.name
        container = name[: -len("-styles.csv")].strip()
        if not container:
            continue
        st = status_map.get(container, {})
        status_label = st.get("status") or "missing"
        for sku_key in _read_skus_from_container_csv(styles_path):
            if sku_key in by_sku:
                continue
            by_sku[sku_key] = {
                "container": container,
                "container_status": status_label,
            }

    return by_sku


def get_sku_container_index() -> dict[str, dict[str, str]]:
    now = time.time()
    if now - float(_sku_index_cache.get("built_at") or 0) < _CACHE_TTL_SEC:
        cached = _sku_index_cache.get("by_sku")
        if isinstance(cached, dict):
            return cached

    by_sku = _build_sku_container_index()
    _sku_index_cache["built_at"] = now
    _sku_index_cache["by_sku"] = by_sku
    return by_sku


def lookup_latest_container_for_sku(sku: str) -> dict[str, str]:
    """Latest container holding this SKU, with boat-style status."""
    key = _sku_match_key(sku)
    if not key:
        return {"container": "", "container_status": "missing"}
    hit = get_sku_container_index().get(key)
    if not hit:
        return {"container": "", "container_status": "missing"}
    return {
        "container": hit.get("container") or "",
        "container_status": hit.get("container_status") or "missing",
    }


def enrich_sku_rows_with_container(sku_rows: list[dict[str, Any]]) -> None:
    """Add container + container_status to each SKU row in po_detail."""
    if not sku_rows:
        return
    index = get_sku_container_index()
    for row in sku_rows:
        key = _sku_match_key(str(row.get("sku") or ""))
        hit = index.get(key) if key else None
        if hit:
            row["container"] = hit.get("container") or ""
            row["container_status"] = hit.get("container_status") or "missing"
        else:
            row["container"] = ""
            row["container_status"] = "missing"
