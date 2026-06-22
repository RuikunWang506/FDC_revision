"""Set ALT_CD (style) for SKU(s) in FDC or EFAS dbo.inv and dbo.inv_data."""
from __future__ import annotations

import sys

from py.utilities.database_utils import (
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)

DEFAULT_SKU = "BUNNY"
DEFAULT_STYLE = "BUNNY"


def pad_alt_cd(style: str, width: int = 20) -> str:
    return (style or "")[:width].ljust(width)


def get_connection(db: str):
    db = (db or "fdc").strip().lower()
    if db in ("fdc", "omsdata_fdc"):
        return get_sql_server_connection_fdc(), "FDC"
    if db in ("efas", "ef", "oms_ef", "oms_efas"):
        return get_sql_server_connection_oms_ef(), "EFAS"
    raise ValueError(f"Unknown database: {db!r}")


def set_sku_style(conn, sku: str, style: str) -> tuple[int, int, list]:
    alt_cd = pad_alt_cd(style)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT RTRIM(PROD_CD), RTRIM(ALT_CD), RTRIM(DESCRIP)
        FROM dbo.inv
        WHERE UPPER(RTRIM(PROD_CD)) = UPPER(?)
        ORDER BY PROD_CD
        """,
        (sku,),
    )
    before = cur.fetchall()
    if not before:
        cur.close()
        return 0, 0, before

    cur.execute(
        """
        UPDATE dbo.inv
        SET ALT_CD = ?
        WHERE UPPER(RTRIM(PROD_CD)) = UPPER(?)
        """,
        (alt_cd, sku),
    )
    inv_updated = cur.rowcount

    cur.execute(
        """
        UPDATE dbo.inv_data
        SET ALT_CD = ?
        WHERE UPPER(RTRIM(prod_cd)) = UPPER(?)
        """,
        (alt_cd, sku),
    )
    inv_data_updated = cur.rowcount
    conn.commit()

    cur.execute(
        """
        SELECT RTRIM(PROD_CD), RTRIM(ALT_CD), RTRIM(DESCRIP)
        FROM dbo.inv
        WHERE UPPER(RTRIM(PROD_CD)) = UPPER(?)
        ORDER BY PROD_CD
        """,
        (sku,),
    )
    after = cur.fetchall()
    cur.close()
    return inv_updated, inv_data_updated, before if not after else after


def main() -> int:
    args = sys.argv[1:]
    db = "fdc"
    if args and args[0].lower() in ("fdc", "efas", "ef"):
        db = args.pop(0).lower()
        if db == "ef":
            db = "efas"

    sku = (args[0] if args else DEFAULT_SKU).strip()
    style = (args[1] if len(args) > 1 else DEFAULT_STYLE).strip()
    if not sku or not style:
        print("Usage: python scripts/set_sku_style.py [fdc|efas] [sku] [style]")
        return 1

    conn, db_label = get_connection(db)
    try:
        inv_updated, inv_data_updated, rows = set_sku_style(conn, sku, style)
        if not rows and inv_updated == 0:
            print(f"No inv row found for SKU {sku!r} in {db_label}.")
            return 1
        print(f"{db_label}: updated inv={inv_updated}, inv_data={inv_data_updated}")
        for row in rows:
            print(" ", row)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
