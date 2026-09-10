"""Re-apply newest packing-list masterbox dims to available OMS SKUs."""
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

from py.utilities.database_utils import (  # noqa: E402
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities.style_efas_transfer import update_case_dimensions_dual  # noqa: E402


def _fetch_available_all(connect_fn) -> list[dict]:
    sql = """
        SELECT
            RTRIM(i.PROD_CD) AS sku,
            RTRIM(ISNULL(i.ALT_CD, '')) AS style,
            RTRIM(ISNULL(i.UNIT_COLOR, '')) AS color
        FROM dbo.inv AS i
        INNER JOIN dbo.inv_data AS d
            ON i.PROD_CD = d.prod_cd AND RTRIM(d.whs_num) = '01'
        WHERE CAST(ISNULL(d.in_stock, 0) - ISNULL(d.order_qty, 0) AS INT) > 0
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
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def main() -> int:
    app = Flask("apply_newest_masterbox")
    print("Building newest-wins Dropbox catalog...")
    with app.app_context():
        paths = _fill._iter_dropbox_excel_paths()
        print(f"  files={len(paths)}")
        exact, style_map, stats = _fill._build_dim_catalog(paths)
    print(
        f"  parsed={stats['parsed']} exact={stats['exact_keys']} styles={stats['styles']}"
    )
    print("  T3305 BK:", exact.get("T3305 BK"))
    print("  T3305 style:", style_map.get("T3305"))

    by_sku = {}
    for row in _fetch_available_all(get_sql_server_connection_oms_ef):
        by_sku[row["sku"]] = row
    for row in _fetch_available_all(get_sql_server_connection_fdc):
        by_sku[row["sku"]] = row

    updates = {}
    for sku, row in by_sku.items():
        dims, how = _fill._resolve_dims(row, exact, style_map)
        if not dims:
            continue
        updates[sku] = (
            Decimal(str(round(dims["length"], 4))),
            Decimal(str(round(dims["width"], 4))),
            Decimal(str(round(dims["height"], 4))),
        )
    print(f"Applying {len(updates)} SKU updates (newest Excel wins)...")
    result = update_case_dimensions_dual(updates, dry_run=False)
    print(
        f"WROTE FDC={len(result.get('updated_fdc') or [])} "
        f"EFAS={len(result.get('updated_efas') or [])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
