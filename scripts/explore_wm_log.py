"""Explore wm_log table structure and adjustment linkage."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections


def main():
    sku = sys.argv[1] if len(sys.argv) > 1 else "N1555"
    log_num = sys.argv[2] if len(sys.argv) > 2 else "900001952"

    conn_fdc, conn_oms = get_db_connections()
    if not conn_fdc:
        print("Failed to connect")
        return 1

    cur = conn_fdc.cursor()

    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'wm_log'
        ORDER BY ORDINAL_POSITION
        """
    )
    print("=== wm_log columns ===")
    for row in cur.fetchall():
        print(row)

    cur.execute(
        """
        SELECT TOP 1 * FROM wm_log WHERE log_num = ? AND type_cd = 'adj'
        """,
        (log_num,),
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    print(f"\n=== Sample row {log_num} ===")
    if row:
        for c, v in zip(cols, row):
            print(f"{c}: {repr(v)}")

    cur.execute(
        """
        SELECT * FROM wm_log WHERE log_num = ? AND type_cd = 'adj' ORDER BY nt_num
        """,
        (log_num,),
    )
    print(f"\n=== All lines in log {log_num} ===")
    for row in cur.fetchall():
        print(dict(zip(cols, row)))

    cur.execute(
        """
        SELECT MAX(CAST(log_num AS BIGINT)) FROM wm_log
        WHERE type_cd = 'adj' AND log_num LIKE '900%'
        """
    )
    print("\nMax adj log_num (900*):", cur.fetchone()[0])

    cur.execute(
        """
        SELECT type_cd, COUNT(*) as cnt FROM wm_log GROUP BY type_cd ORDER BY cnt DESC
        """
    )
    print("\n=== type_cd distribution ===")
    for row in cur.fetchall():
        print(row)

    cur.execute(
        """
        SELECT i.PROD_CD, id.in_stock, id.order_qty, id.on_order_qty,
               CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS available
        FROM inv i
        LEFT JOIN inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE i.ALT_CD LIKE ?
        ORDER BY i.PROD_CD
        """,
        (f"{sku}%",),
    )
    print(f"\n=== {sku} stock (FDC) ===")
    for row in cur.fetchall():
        print(row)

    # Recent adjustments for SKU
    cur.execute(
        """
        SELECT prod_cd, log_num, log_qty, log_by, log_dt, bin_cd, whs_num, nt_num,
               trans_type, in_stock, bin_qty, ord_qty, updt_by
        FROM wm_log
        WHERE prod_cd LIKE ? AND type_cd = 'adj'
        ORDER BY log_dt DESC
        """,
        (f"{sku}%",),
    )
    print(f"\n=== Recent {sku} adjustments ===")
    for row in cur.fetchall():
        print(row)

    # Check if log_num appears in other tables
    for table in ("inv_data", "inv", "plog", "ord_log", "wm_skufile"):
        try:
            cur.execute(
                f"""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ? AND COLUMN_NAME LIKE '%log%'
                """,
                (table,),
            )
            log_cols = [r[0] for r in cur.fetchall()]
            if log_cols:
                print(f"\n{table} log-related columns: {log_cols}")
        except Exception as e:
            print(f"Error checking {table}: {e}")

    cur.execute(
        """
        SELECT t.name, OBJECT_NAME(t.parent_id) as table_name, te.type_desc
        FROM sys.triggers t
        JOIN sys.trigger_events te ON t.object_id = te.object_id
        WHERE OBJECT_NAME(t.parent_id) IN ('wm_log', 'inv_data', 'inv')
        """
    )
    print("\n=== Triggers on wm_log/inv_data/inv ===")
    for row in cur.fetchall():
        print(row)

    cur.execute(
        """
        SELECT name FROM sys.procedures
        WHERE name LIKE '%adj%' OR name LIKE '%wm_log%' OR name LIKE '%in_stock%'
        ORDER BY name
        """
    )
    print("\n=== Related stored procs ===")
    for row in cur.fetchall():
        print(row[0])

    cur.execute(
        """
        SELECT TOP 10 prod_cd, log_num, log_qty, in_stock, bin_qty, ord_qty, log_dt
        FROM wm_log WHERE type_cd = 'adj'
        ORDER BY log_dt DESC, log_time DESC
        """
    )
    print("\n=== Recent 10 adjustments (in_stock semantics) ===")
    for row in cur.fetchall():
        prod = row[0].strip()
        log_qty = float(row[2])
        in_stock = float(row[3])
        print(
            f"{prod:15} log_qty={log_qty:+.0f} in_stock={in_stock:.0f} "
            f"after={in_stock + log_qty:.0f}"
        )

    cur.execute(
        """
        SELECT prod_cd, sku_num FROM wm_skufile
        WHERE prod_cd LIKE ? AND whs_num = '01'
        """,
        (f"{sku}%",),
    )
    print(f"\n=== {sku} wm_skufile ===")
    for row in cur.fetchall():
        print(row[0].strip(), row[1])

    cur.execute("SELECT IDENT_CURRENT('wm_log')")
    print("\nIDENT_CURRENT wm_log:", cur.fetchone()[0])
    cur.execute("SELECT MAX(rec_num) FROM wm_log")
    print("MAX rec_num:", cur.fetchone()[0])

    # Date conversion helper - today's log_dt
    cur.execute(
        """
        SELECT DATEDIFF(DAY, '19000101', GETDATE()) + 81088 AS log_dt_today
        """
    )
    print("Today's log_dt:", cur.fetchone()[0])

    conn_fdc.close()
    if conn_oms:
        conn_oms.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
