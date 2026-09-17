"""OMS-first catalog backfill: in-stock / ocean styles missing DAM catalog_original.

Sold-out historical styles are skipped. Dropbox is searched only for styles that
still have warehouse stock or inbound ocean qty in FDC or E_FAS.
"""

from __future__ import annotations

import re

from py.utilities.database_utils import (
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities.ocean_qty import PLOG_OCEAN_JOIN, ocean_qty_sql_sum

STYLE_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,29}$")

ACTIVE_OMS_STYLE_SQL = f"""
SELECT
    UPPER(LTRIM(RTRIM(
        CASE
            WHEN i.ALT_CD IS NOT NULL AND LTRIM(RTRIM(i.ALT_CD)) <> '' THEN i.ALT_CD
            ELSE LEFT(i.PROD_CD, CHARINDEX(' ', i.PROD_CD + ' ') - 1)
        END
    ))) AS style,
    SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) AS stock,
    {ocean_qty_sql_sum()} AS ocean
FROM dbo.inv AS i
LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
{PLOG_OCEAN_JOIN}
WHERE i.PROD_CD IS NOT NULL AND LTRIM(RTRIM(i.PROD_CD)) <> ''
GROUP BY UPPER(LTRIM(RTRIM(
        CASE
            WHEN i.ALT_CD IS NOT NULL AND LTRIM(RTRIM(i.ALT_CD)) <> '' THEN i.ALT_CD
            ELSE LEFT(i.PROD_CD, CHARINDEX(' ', i.PROD_CD + ' ') - 1)
        END
    )))
"""


def normalize_oms_style(raw) -> str | None:
    text = (raw or "").strip().upper()
    if not text:
        return None
    if " " in text:
        text = text.split()[0]
    if not STYLE_CODE_RE.match(text):
        return None
    return text


def merge_active_oms_styles(*row_groups) -> list[str]:
    """Union styles that still have stock or ocean qty in any OMS database."""
    totals: dict[str, dict[str, int]] = {}
    for rows in row_groups:
        for style, stock, ocean in rows or []:
            key = normalize_oms_style(style)
            if not key:
                continue
            bucket = totals.setdefault(key, {"stock": 0, "ocean": 0})
            bucket["stock"] += int(stock or 0)
            bucket["ocean"] += int(ocean or 0)
    return sorted(
        style for style, qty in totals.items() if qty["stock"] > 0 or qty["ocean"] > 0
    )


def _query_oms_style_stock(connect, label: str, log_fn) -> list[tuple]:
    conn = None
    try:
        conn = connect()
        cursor = conn.cursor()
        try:
            cursor.execute(ACTIVE_OMS_STYLE_SQL)
            rows = [(row[0], row[1], row[2]) for row in cursor.fetchall()]
        finally:
            cursor.close()
        return rows
    except Exception as exc:
        log_fn(f"OMS catalog backfill: {label} style query failed: {exc}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetch_active_oms_styles(log_fn=print) -> list[str]:
    """Styles still selling: warehouse stock or ocean qty in FDC and/or E_FAS."""
    fdc_rows = _query_oms_style_stock(get_sql_server_connection_fdc, "FDC", log_fn)
    ef_rows = _query_oms_style_stock(get_sql_server_connection_oms_ef, "E_FAS", log_fn)
    styles = merge_active_oms_styles(fdc_rows, ef_rows)
    log_fn(
        f"OMS in-stock/ocean styles: {len(styles)} "
        f"(FDC rows={len(fdc_rows)}, E_FAS rows={len(ef_rows)}; sold-out skipped)"
    )
    return styles


def run_oms_dropbox_catalog_backfill(*, apply: bool = True, log_fn=print, limit: int = 0) -> dict:
    """Copy highest-res Dropbox image for OMS styles missing catalog_original."""
    from py.standalone.backfill_catalog_from_dropbox import run_backfill_for_skus

    log_fn("\n=== OMS missing catalog from Dropbox (in-stock / ocean only) ===")
    styles = fetch_active_oms_styles(log_fn)
    stats = run_backfill_for_skus(styles, apply=apply, log_fn=log_fn, limit=limit)
    stats["oms_active_styles"] = len(styles)
    log_fn(
        f"OMS catalog backfill done: active={stats.get('oms_active_styles', 0)}, "
        f"missing={stats.get('oms_missing_catalog', 0)}, "
        f"copied={stats.get('oms_copied', 0)}, "
        f"no Dropbox file={stats.get('oms_no_dropbox', 0)}"
    )
    return stats
