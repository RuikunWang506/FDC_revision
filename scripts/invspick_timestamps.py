import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

ERP_EPOCH = "20230101"
ERP_OFFSET = 81088


def erp_to_display(erp_day: int) -> str:
    if not erp_day:
        return ""
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT RIGHT('0' + CAST(MONTH(DATEADD(DAY, ? - {ERP_OFFSET}, '{ERP_EPOCH}')) AS VARCHAR), 2) + '/' +
               RIGHT('0' + CAST(DAY(DATEADD(DAY, ? - {ERP_OFFSET}, '{ERP_EPOCH}')) AS VARCHAR), 2) + '/' +
               CAST(YEAR(DATEADD(DAY, ? - {ERP_OFFSET}, '{ERP_EPOCH}')) AS VARCHAR)
        """,
        (erp_day, erp_day, erp_day),
    )
    return str(cur.fetchone()[0] or "")


def erp_time_display(log_time) -> str:
    if log_time is None:
        return ""
    try:
        total = int(log_time)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    seconds = total // 100
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


conn, _ = get_db_connections()
cur = conn.cursor()

print("=== L1849 BK INVSPICK (with dates) ===")
cur.execute(
    """
    SELECT RTRIM(log_num), log_dt, log_time, log_qty, in_stock, RTRIM(updt_by), updt_dt
    FROM wm_log
    WHERE RTRIM(prod_cd) = 'L1849 BK' AND RTRIM(type_cd) = 'INVSPICK'
    ORDER BY log_dt, log_time
    """
)
for log_num, log_dt, log_time, log_qty, in_stock, updt_by, updt_dt in cur.fetchall():
    date_str = erp_to_display(int(log_dt or 0))
    time_str = erp_time_display(log_time)
    updt_str = erp_to_display(int(updt_dt or 0)) if updt_dt else ""
    print(
        f"log {log_num.strip():<6}  qty={int(log_qty):>4}  "
        f"date={date_str}  time={time_str}  by={updt_by}  updt_dt={updt_str}"
    )

print("\n=== Full pick tickets 31854 / 32235 (L1849 lines only) ===")
for ln in ["31854", "32235"]:
    cur.execute(
        """
        SELECT RTRIM(prod_cd), log_qty, RTRIM(type_cd), log_dt, log_time, RTRIM(updt_by)
        FROM wm_log
        WHERE RTRIM(log_num) = ? AND RTRIM(prod_cd) LIKE 'L1849%'
        ORDER BY prod_cd
        """,
        (ln,),
    )
    rows = cur.fetchall()
    if rows:
        d = erp_to_display(int(rows[0][3] or 0))
        t = erp_time_display(rows[0][4])
        print(f"\nTicket {ln} — {d} {t} (by {rows[0][5]})")
        for r in rows:
            print(f"  {r[0]:<12} {int(r[1]):>5}  {r[2]}")

conn.close()
