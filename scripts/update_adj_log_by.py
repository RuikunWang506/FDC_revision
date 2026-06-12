"""Update log_by / updt_by on wm_log adjustment batches."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

LOG_NUMS = ("900002045", "900002046")
NEW_BY = "fdcef"


def pad_char(value: str, width: int) -> str:
    text = (value or "").strip()
    if len(text) > width:
        return text[:width]
    return text + (" " * (width - len(text)))


def main():
    conn_fdc, conn_ef = get_db_connections()
    if not conn_fdc:
        print("Failed to connect", file=sys.stderr)
        return 1

    log_by = pad_char(NEW_BY, 8)
    cur = conn_fdc.cursor()

    placeholders = ", ".join("?" for _ in LOG_NUMS)
    cur.execute(
        f"""
        SELECT rec_num, RTRIM(log_num), RTRIM(prod_cd), log_qty,
               RTRIM(log_by), RTRIM(updt_by)
        FROM wm_log
        WHERE RTRIM(log_num) IN ({placeholders}) AND RTRIM(type_cd) = 'ADJ'
        ORDER BY log_num, nt_num
        """,
        LOG_NUMS,
    )
    before = cur.fetchall()
    print("Before:")
    for row in before:
        print(row)
    if not before:
        print("No rows found.")
        return 1

    cur.execute(
        f"""
        UPDATE wm_log
        SET log_by = ?, updt_by = ?
        WHERE RTRIM(log_num) IN ({placeholders}) AND RTRIM(type_cd) = 'ADJ'
        """,
        (log_by, log_by, *LOG_NUMS),
    )
    print(f"\nUpdated {cur.rowcount} wm_log row(s) -> {NEW_BY!r}")

    # Keep inv_data consistent for SKUs touched by these logs
    cur.execute(
        f"""
        SELECT DISTINCT RTRIM(prod_cd)
        FROM wm_log
        WHERE RTRIM(log_num) IN ({placeholders}) AND RTRIM(type_cd) = 'ADJ'
        """,
        LOG_NUMS,
    )
    skus = [row[0] for row in cur.fetchall()]
    for sku in skus:
        prod_cd = pad_char(sku, 20)
        cur.execute(
            """
            UPDATE inv_data
            SET updt_by = ?
            WHERE prod_cd = ? AND RTRIM(whs_num) = '01'
            """,
            (log_by, prod_cd),
        )

    conn_fdc.commit()

    cur.execute(
        f"""
        SELECT rec_num, RTRIM(log_num), RTRIM(prod_cd), log_qty,
               RTRIM(log_by), RTRIM(updt_by)
        FROM wm_log
        WHERE RTRIM(log_num) IN ({placeholders}) AND RTRIM(type_cd) = 'ADJ'
        ORDER BY log_num, nt_num
        """,
        LOG_NUMS,
    )
    print("\nAfter:")
    for row in cur.fetchall():
        print(row)

    conn_fdc.close()
    if conn_ef:
        conn_ef.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
