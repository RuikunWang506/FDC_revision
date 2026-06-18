import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

print("=== Global wm_log type_cd (top) ===")
cur.execute(
    """
    SELECT RTRIM(type_cd), COUNT(*), SUM(log_qty)
    FROM wm_log
    GROUP BY RTRIM(type_cd)
    ORDER BY COUNT(*) DESC
    """
)
for r in cur.fetchall()[:15]:
    print(r)

print("\n=== INVSPICK logs 32235 / 31854 ===")
for ln in ["32235", "31854"]:
    cur.execute(
        """
        SELECT RTRIM(prod_cd), log_qty, RTRIM(type_cd), RTRIM(updt_by), RTRIM(whs_num)
        FROM wm_log
        WHERE RTRIM(log_num) = ?
        ORDER BY nt_num
        """,
        (ln,),
    )
    rows = cur.fetchall()
    print(f"log {ln}: {len(rows)} line(s)")
    for r in rows:
        print(" ", r)

print("\n=== Sample INVSPICK from other SKUs ===")
cur.execute(
    """
    SELECT TOP 8 RTRIM(prod_cd), RTRIM(log_num), log_qty, RTRIM(updt_by)
    FROM wm_log
    WHERE RTRIM(type_cd) = 'INVSPICK'
    ORDER BY log_dt DESC
    """
)
for r in cur.fetchall():
    print(r)

conn.close()
