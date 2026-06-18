import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections, fetch_sku_history, search_product_data_with_history

conn_fdc, conn_ef = get_db_connections()
sku = "L1849 BK            "

h = fetch_sku_history(sku, conn_fdc)
avail = 603
checker = h["history_po"] - h["history_sold"] - avail + h["history_fix"]
print("history_fdc", h)
print("checker", checker)

cur = conn_fdc.cursor()
cur.execute(
    "SELECT ISNULL(SUM(hdl_chg),0) FROM plog WHERE prod_cd=? AND hdl_chg>0",
    (sku,),
)
print("plog sum (exact prod_cd):", cur.fetchone()[0])

cur.execute(
    """
    SELECT RTRIM(pur_num), hdl_chg, nt_num
    FROM plog
    WHERE RTRIM(prod_cd)=? AND hdl_chg>0
    ORDER BY pur_num, nt_num
    """,
    ("L1849 BK",),
)
rows = cur.fetchall()
print("receive lines", len(rows), "sum", sum(float(r[1]) for r in rows))
for r in rows:
    print(" ", r)

cur.execute(
    "SELECT ISNULL(SUM(order_qty),0) FROM ord_log WHERE prod_cd=?",
    (sku,),
)
print("history_sold ord_log:", cur.fetchone()[0])

cur.execute(
    "SELECT ISNULL(SUM(log_qty),0) FROM wm_log WHERE prod_cd=? AND type_cd='adj'",
    (sku,),
)
print("history_fix wm_log:", cur.fetchone()[0])

# F261101 plog only
cur.execute(
    """
    SELECT hdl_chg, log_qty, nt_num FROM plog
    WHERE RTRIM(pur_num)='F261101' AND RTRIM(prod_cd)='L1849 BK'
    ORDER BY hdl_chg, nt_num
    """
)
print("F261101 plog lines:")
for r in cur.fetchall():
    print(" ", r)

# what sku does color['sku'] use?
data = search_product_data_with_history("L1849 BK")
if data:
    c = data[0]["colors"][0]
    print("color sku repr:", repr(c["sku"]))
    hf = c["history_fdc"]
    av = c["fdc"]["available"]
    chk = hf["history_po"] - hf["history_sold"] - av + hf["history_fix"]
    print("from template:", hf, "avail", av, "checker", chk)

conn_fdc.close()
conn_ef.close()

print()
print("=== inventory identity ===")
conn_fdc, _ = get_db_connections()
cur = conn_fdc.cursor()
cur.execute(
    """
    SELECT in_stock, order_qty FROM inv_data id
    JOIN inv i ON i.prod_cd=id.prod_cd
    WHERE RTRIM(i.prod_cd)=? AND RTRIM(id.whs_num)=?
    """,
    ("L1849 BK", "01"),
)
stock, oq = cur.fetchone()
avail = float(stock) - float(oq)
print("stock", float(stock), "order_qty", float(oq), "avail", avail)

cur.execute(
    "SELECT ISNULL(SUM(hdl_chg),0) FROM plog WHERE RTRIM(prod_cd)=? AND hdl_chg>0",
    ("L1849 BK",),
)
po = float(cur.fetchone()[0])
cur.execute(
    "SELECT ISNULL(SUM(order_qty),0) FROM ord_log WHERE RTRIM(prod_cd)=?",
    ("L1849 BK",),
)
sold = float(cur.fetchone()[0])
cur.execute(
    "SELECT ISNULL(SUM(log_qty),0) FROM wm_log WHERE RTRIM(prod_cd)=? AND type_cd='adj'",
    ("L1849 BK",),
)
fix = float(cur.fetchone()[0])

print("po - sold + fix =", po - sold + fix)
print("stock + order_qty =", float(stock) + float(oq))
print("gap (should be 0 if logs match inv):", (po - sold + fix) - (float(stock) + float(oq)))
print("checker =", po - sold - avail + fix)

# Compare inv order_qty vs open orders
cur.execute(
    "SELECT COUNT(*), SUM(order_qty) FROM ord_log WHERE RTRIM(prod_cd)=? AND order_qty > 0",
    ("L1849 BK",),
)
print("ord_log rows with qty>0:", cur.fetchone())

conn_fdc.close()

