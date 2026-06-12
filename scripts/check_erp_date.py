import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections


def erp_date_to_display(log_dt):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT RIGHT('0' + CAST(MONTH(DATEADD(DAY, ? - 81088, '20230101')) AS VARCHAR), 2) + '/' +
               RIGHT('0' + CAST(DAY(DATEADD(DAY, ? - 81088, '20230101')) AS VARCHAR), 2) + '/' +
               CAST(YEAR(DATEADD(DAY, ? - 81088, '20230101')) AS VARCHAR)
        """,
        (log_dt, log_dt, log_dt),
    )
    return cur.fetchone()[0]


def display_to_erp_date(dt):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DATEDIFF(DAY, '20230101', ?) + 81088
        """,
        (dt,),
    )
    return int(cur.fetchone()[0])


conn, _ = get_db_connections()
cur = conn.cursor()

cur.execute(
    """
    SELECT TOP 3 rec_num, log_dt, log_time, log_num, prod_cd, log_qty, in_stock
    FROM wm_log WHERE type_cd = 'adj'
    ORDER BY rec_num DESC
    """
)
print("=== Latest adj records ===")
for row in cur.fetchall():
    print(row, "display:", erp_date_to_display(row[1]))

today = datetime.now().date()
print("\nToday display->erp:", display_to_erp_date(today))
print("Today erp->display:", erp_date_to_display(display_to_erp_date(today)))

cur.execute(
    """
    SELECT DATEDIFF(DAY, '19000101', GETDATE()) + 81088 AS alt_formula
    """
)
print("Alt formula (wrong?):", cur.fetchone()[0])

conn.close()
