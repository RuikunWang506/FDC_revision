import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

cur.execute(
    """
    SELECT prod_cd, whs_num, in_stock, order_qty, updt_by, updt_dt
    FROM inv_data WHERE prod_cd LIKE 'N1555%'
    """
)
print("=== N1555 inv_data rows ===")
for row in cur.fetchall():
    print(row)

cur.execute(
    """
    SELECT prod_cd, whs_num, bin_cd, in_stock, sku_num, updt_by
    FROM wm_binfile WHERE prod_cd LIKE 'N1555%'
    """
)
print("\n=== N1555 wm_binfile ===")
for row in cur.fetchall():
    print(row)

# Template from latest single-line adj for similar SKU
cur.execute(
    """
    SELECT TOP 1 *
    FROM wm_log
    WHERE type_cd = 'adj' AND prod_cd LIKE 'N2629%'
    ORDER BY rec_num DESC
    """
)
cols = [d[0] for d in cur.description]
row = cur.fetchone()
print("\n=== Template recent small adj ===")
for c, v in zip(cols, row):
    if v not in (None, '', ' ' * 10, ' ' * 20, ' ' * 30, ' ' * 8, 0, Decimal('0')):
        print(f"  {c}: {repr(v)}")

conn.close()
