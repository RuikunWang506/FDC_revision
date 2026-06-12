import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

for sku in ("EN1824 BK", "EN1824 WN", "N1555 BK"):
    cur.execute(
        """
        SELECT id.in_stock, id.order_qty,
               (SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = i.PROD_CD) AS bin_sum
        FROM inv i
        LEFT JOIN inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE i.PROD_CD = ?
        """,
        (sku.ljust(20),),
    )
    row = cur.fetchone()
    print(f"{sku.strip():12} inv_data.in_stock={row[0]} bin_sum={row[2]}")

cur.execute(
    """
    SELECT prod_cd, log_qty, in_stock, log_num, log_dt
    FROM wm_log
    WHERE log_num = '900002044' AND type_cd = 'adj'
    ORDER BY nt_num
    """
)
print("\n=== Log 900002044 lines ===")
for row in cur.fetchall():
    print(row[0].strip(), float(row[1]), float(row[2]), row[4])

conn.close()
