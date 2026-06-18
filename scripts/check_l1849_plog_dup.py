import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

print("=== plog receive by PO (hdl_chg>0) ===")
cur.execute(
    """
    SELECT RTRIM(pur_num), SUM(hdl_chg), COUNT(*)
    FROM plog
    WHERE RTRIM(prod_cd) = 'L1849 BK' AND hdl_chg > 0
    GROUP BY RTRIM(pur_num)
    ORDER BY RTRIM(pur_num)
    """
)
plog_by_po = cur.fetchall()
plog_total = 0
for r in plog_by_po:
    print(r)
    plog_total += float(r[1])
print("plog total", plog_total)

print("\n=== bin_pur PUR_CD=1 rcvd by PO ===")
cur.execute(
    """
    SELECT RTRIM(pur_num), SUM(rcvd_qty), COUNT(*)
    FROM bin_pur
    WHERE RTRIM(prod_cd) = 'L1849 BK' AND pur_cd = 1
    GROUP BY RTRIM(pur_num)
    ORDER BY RTRIM(pur_num)
    """
)
bin_total = 0
for r in cur.fetchall():
    print(r)
    bin_total += float(r[1])
print("bin_pur rcvd total", bin_total)

print("\n=== plog vs bin_pur rcvd diff by PO ===")
cur.execute(
    """
    SELECT COALESCE(p.pur_num, b.pur_num) AS po,
           ISNULL(p.plog_rcvd, 0) AS plog_rcvd,
           ISNULL(b.bin_rcvd, 0) AS bin_rcvd,
           ISNULL(p.plog_rcvd, 0) - ISNULL(b.bin_rcvd, 0) AS diff
    FROM (
        SELECT RTRIM(pur_num) AS pur_num, SUM(hdl_chg) AS plog_rcvd
        FROM plog
        WHERE RTRIM(prod_cd) = 'L1849 BK' AND hdl_chg > 0
        GROUP BY RTRIM(pur_num)
    ) p
    FULL OUTER JOIN (
        SELECT RTRIM(pur_num) AS pur_num, SUM(rcvd_qty) AS bin_rcvd
        FROM bin_pur
        WHERE RTRIM(prod_cd) = 'L1849 BK' AND pur_cd = 1
        GROUP BY RTRIM(pur_num)
    ) b ON p.pur_num = b.pur_num
    WHERE ISNULL(p.plog_rcvd, 0) <> ISNULL(b.bin_rcvd, 0)
    ORDER BY po
    """
)
rows = cur.fetchall()
if rows:
    for r in rows:
        print(r)
else:
    print("no mismatches")

print("\n=== duplicate plog receive rows (same PO+nt_num) ===")
cur.execute(
    """
    SELECT RTRIM(pur_num), nt_num, hdl_chg, COUNT(*)
    FROM plog
    WHERE RTRIM(prod_cd) = 'L1849 BK' AND hdl_chg > 0
    GROUP BY RTRIM(pur_num), nt_num, hdl_chg
    HAVING COUNT(*) > 1
    """
)
print(cur.fetchall() or "none")

conn.close()
