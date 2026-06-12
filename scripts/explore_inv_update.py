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
        WHERE OBJECT_DEFINITION(object_id) LIKE '%UPDATE%inv_data%'
           OR OBJECT_DEFINITION(object_id) LIKE '%inv_data%IN_STOCK%'
        ORDER BY name
        """
    )
    print("=== Procs updating inv_data ===")
    for row in cur.fetchall():
        print(row[0])

    # M1141 BK - compare after recent adj
    cur.execute(
        """
        SELECT i.PROD_CD, id.in_stock, id.order_qty,
               b.bin_cd, b.in_stock AS bin_stock
        FROM inv i
        LEFT JOIN inv_data id ON i.PROD_CD = id.Prod_cd
        LEFT JOIN wm_binfile b ON i.PROD_CD = b.prod_cd AND b.whs_num = '01'
        WHERE i.PROD_CD IN ('M1141 BK', 'M1141 BR')
        """
    )
    print("\n=== M1141 stock ===")
    for row in cur.fetchall():
        print(row)

    # Check if inv_data is a view
    cur.execute(
        """
        SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'inv_data'
        """
    )
    print("\ninv_data type:", cur.fetchone())

    # NT_NUM pattern - max nt_num per log
    cur.execute(
        """
        SELECT log_num, MAX(nt_num) as max_nt
        FROM wm_log WHERE type_cd = 'adj' AND log_num LIKE '900%'
        GROUP BY log_num
        ORDER BY MAX(rec_num) DESC
        OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY
        """
    )
    print("\n=== Recent log batches nt_num ===")
    for row in cur.fetchall():
        print(row[0].strip(), row[1])

    conn.close()


if __name__ == "__main__":
    main()
