import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections, fetch_sku_history

conn, _ = get_db_connections()
cur = conn.cursor()
sku = "L1849 BK            "

h = fetch_sku_history(sku, conn)
cur.execute(
    "SELECT in_stock, order_qty FROM inv_data id JOIN inv i ON i.prod_cd=id.prod_cd WHERE RTRIM(i.prod_cd)=? AND RTRIM(id.whs_num)=?",
    ("L1849 BK", "01"),
)
stock, oq = cur.fetchone()
expected_stock = h["history_po"] - h["history_sold"] + h["history_fix"] + int(oq or 0)
checker = h["history_po"] - h["history_sold"] - (int(stock or 0) - int(oq or 0)) + h["history_fix"]
print("history_po", h["history_po"])
print("history_sold", h["history_sold"])
print("history_fix", h["history_fix"])
print("in_stock", int(stock), "order_qty", int(oq), "available", int(stock) - int(oq))
print("expected in_stock (po - sold + fix + order_qty):", expected_stock)
print("stock shortfall:", expected_stock - int(stock))
print("checker off by:", checker)

cur.execute(
    """
    SELECT ISNULL(SUM(b.in_stock), 0)
    FROM wm_binfile b
    WHERE RTRIM(b.prod_cd) = ?
    """,
    ("L1849 BK",),
)
print("bin sum:", float(cur.fetchone()[0]))

# stock before F261101 receive date - approximate from plog
cur.execute(
    """
    SELECT SUM(hdl_chg) FROM plog
    WHERE RTRIM(prod_cd)='L1849 BK' AND hdl_chg>0 AND RTRIM(pur_num) <> 'F261101'
    """
)
prior_po = float(cur.fetchone()[0] or 0)
print("plog received before F261101:", prior_po)
print("F261101 plog received: 1300")
print("if stock should be prior_po - sold + fix + oq + 1300:", prior_po - h["history_sold"] + h["history_fix"] + int(oq) + 1300)

conn.close()
