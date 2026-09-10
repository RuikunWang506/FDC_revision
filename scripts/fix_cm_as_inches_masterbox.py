"""Fix masterbox dims that were imported as cm-as-inches (any side >= 40).

1) Convert inflated OMS CASE_* values (/2.54) as a safety pass
2) Rebuild catalog with year cutoffs and overwrite from newer Excels
3) Write corrected dims for all available SKUs that match the catalog
"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_fill_path = ROOT / "scripts" / "fill_missing_masterbox_from_dropbox.py"
_spec = importlib.util.spec_from_file_location("fill_missing_masterbox_from_dropbox", _fill_path)
_fill = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_fill)

_build_dim_catalog = _fill._build_dim_catalog
_iter_dropbox_excel_paths = _fill._iter_dropbox_excel_paths
_resolve_dims = _fill._resolve_dims

from py.utilities.database_utils import (  # noqa: E402
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities.style_efas_transfer import update_case_dimensions_dual  # noqa: E402


def _convert_inflated_case_dims(connect_fn, label: str) -> int:
    sql = """
        UPDATE dbo.inv_detl
        SET
            CASE_LEN = CASE_LEN / 2.54,
            CASE_WI = CASE_WI / 2.54,
            CASE_HI = CASE_HI / 2.54
        WHERE (
            ISNULL(CASE_LEN, 0) >= 40
            OR ISNULL(CASE_WI, 0) >= 40
            OR ISNULL(CASE_HI, 0) >= 40
        )
    """
    conn = connect_fn()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        n = cur.rowcount
        conn.commit()
        print(f"{label}: converted {n} inflated CASE_* row(s) (/2.54)")
        return int(n or 0)
    finally:
        conn.close()


def _fetch_available_all(connect_fn) -> list[dict]:
    sql = """
        SELECT
            RTRIM(i.PROD_CD) AS sku,
            RTRIM(ISNULL(i.ALT_CD, '')) AS style,
            RTRIM(ISNULL(i.UNIT_COLOR, '')) AS color,
            CAST(ISNULL(d.in_stock, 0) - ISNULL(d.order_qty, 0) AS INT) AS available
        FROM dbo.inv AS i
        INNER JOIN dbo.inv_data AS d
            ON i.PROD_CD = d.prod_cd AND RTRIM(d.whs_num) = '01'
        WHERE CAST(ISNULL(d.in_stock, 0) - ISNULL(d.order_qty, 0) AS INT) > 0
        ORDER BY i.ALT_CD, i.UNIT_COLOR, i.PROD_CD
    """
    conn = connect_fn()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [
            {
                "sku": str(r[0] or "").strip(),
                "style": str(r[1] or "").strip(),
                "color": str(r[2] or "").strip(),
                "available": int(r[3] or 0),
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def main() -> int:
    app = Flask("fix_cm_inches")
    print("Step 1: convert inflated OMS case dims (>=40) ...")
    _convert_inflated_case_dims(get_sql_server_connection_fdc, "FDC")
    _convert_inflated_case_dims(get_sql_server_connection_oms_ef, "EFAS")

    print("\nStep 2: rebuild Dropbox catalog with year cutoffs ...")
    with app.app_context():
        paths = _iter_dropbox_excel_paths()
        print(f"  files={len(paths)}")
        exact, style_map, stats = _build_dim_catalog(paths)
    print(
        f"  parsed={stats['parsed']} exact_keys={stats['exact_keys']} styles={stats['styles']}"
    )
    print("  N1223 GY catalog:", exact.get("N1223 GY"))
    print("  N1223 BG catalog:", exact.get("N1223 BG"))

    print("\nStep 3: rewrite available SKUs from corrected catalog ...")
    fdc_rows = _fetch_available_all(get_sql_server_connection_fdc)
    efas_rows = _fetch_available_all(get_sql_server_connection_oms_ef)
    by_sku = {r["sku"]: r for r in efas_rows}
    by_sku.update({r["sku"]: r for r in fdc_rows})

    updates = {}
    for sku, row in by_sku.items():
        dims, how = _resolve_dims(row, exact, style_map)
        if not dims:
            continue
        updates[sku] = (
            Decimal(str(round(dims["length"], 4))),
            Decimal(str(round(dims["width"], 4))),
            Decimal(str(round(dims["height"], 4))),
        )

    print(f"  matched updates={len(updates)}")
    result = update_case_dimensions_dual(updates, dry_run=False)
    print(
        f"  WROTE FDC={len(result.get('updated_fdc') or [])}+{len(result.get('inserted_fdc') or [])} "
        f"EFAS={len(result.get('updated_efas') or [])}+{len(result.get('inserted_efas') or [])}"
    )

    # Show N1223 after
    from py.utilities.database_utils import get_sql_server_connection_fdc as fdc

    conn = fdc()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT RTRIM(PROD_CD), CASE_LEN, CASE_WI, CASE_HI
        FROM dbo.inv_detl
        WHERE RTRIM(PROD_CD) LIKE 'N1223 %'
        ORDER BY PROD_CD
        """
    )
    print("\nN1223 after:")
    for r in cur.fetchall():
        print(" ", r)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
