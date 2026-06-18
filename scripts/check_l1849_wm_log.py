import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

print("=== wm_log by type_cd for L1849 BK ===")
cur.execute(
    """
    SELECT type_cd, COUNT(*), SUM(log_qty)
    FROM wm_log
    WHERE RTRIM(prod_cd) = 'L1849 BK'
    GROUP BY type_cd
    ORDER BY type_cd
    """
)
for r in cur.fetchall():
    print(r)

print("\n=== recent wm_log (non-adj) ===")
cur.execute(
    """
    SELECT TOP 20 type_cd, log_qty, in_stock, log_num, trans_type, updt_by
    FROM wm_log
    WHERE RTRIM(prod_cd) = 'L1849 BK'
    ORDER BY log_dt DESC, log_num DESC
    """
)
for r in cur.fetchall():
    print(r)

print("\n=== ord_log: open vs all ===")
cur.execute(
    """
    SELECT COUNT(*), SUM(order_qty)
    FROM ord_log
    WHERE RTRIM(prod_cd) = 'L1849 BK'
    """
)
print("all ord_log", cur.fetchone())

# Check if ord_log has negative qty
cur.execute(
    """
    SELECT COUNT(*), SUM(order_qty)
    FROM ord_log
    WHERE RTRIM(prod_cd) = 'L1849 BK' AND order_qty < 0
    """
)
print("negative ord_log", cur.fetchone())

# plog hdl_chg negative?
cur.execute(
    """
    SELECT COUNT(*), SUM(hdl_chg)
    FROM plog
    WHERE RTRIM(prod_cd) = 'L1849 BK' AND hdl_chg < 0
    """
)
print("negative plog hdl_chg", cur.fetchone())

conn.close()
