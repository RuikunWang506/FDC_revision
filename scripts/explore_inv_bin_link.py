import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections


def main():
    conn, _ = get_db_connections()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT name FROM sys.procedures
        WHERE OBJECT_DEFINITION(object_id) LIKE '%wm_binfile%'
          AND (OBJECT_DEFINITION(object_id) LIKE '%inv_data%'
               OR OBJECT_DEFINITION(object_id) LIKE '%INV_DATA%')
        """
    )
    print("=== Procs linking wm_binfile -> inv_data ===")
    for row in cur.fetchall():
        print(row[0])

    cur.execute(
        """
        SELECT name FROM sys.triggers
        WHERE OBJECT_DEFINITION(object_id) LIKE '%inv_data%'
           OR OBJECT_DEFINITION(object_id) LIKE '%INV_DATA%'
        """
    )
    print("\n=== Triggers referencing inv_data ===")
    for row in cur.fetchall():
        cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", (row[0],))
        defn = (cur.fetchone()[0] or "")[:200]
        print(f"{row[0]}: {defn}...")

    # N1555 BK bin file
    cur.execute(
        """
        SELECT prod_cd, bin_cd, in_stock, whs_num, sku_num
        FROM wm_binfile WHERE prod_cd LIKE 'N1555%'
        """
    )
    print("\n=== N1555 wm_binfile ===")
    for row in cur.fetchall():
        print(row)

    # Compare inv_data vs sum binfile
    cur.execute(
        """
        SELECT i.PROD_CD, id.in_stock AS inv_in_stock,
               (SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = i.PROD_CD) AS bin_sum
        FROM inv i
        LEFT JOIN inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE i.ALT_CD LIKE 'N1555%'
        """
    )
    print("\n=== N1555 inv_data vs binfile sum ===")
    for row in cur.fetchall():
        print(row)

    conn.close()


if __name__ == "__main__":
    main()
