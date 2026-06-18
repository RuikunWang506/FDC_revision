"""Audit container PO bin_pur rows and stock for duplicate receive issues."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

PO_LINE_PUR_CD = 1


def audit_containers(container_list: list[str]) -> None:
    conn, _ = get_db_connections()
    cur = conn.cursor()

    print("=== CONTAINER OVERVIEW ===")
    header = (
        f"{'Container':<12} {'PO':<4} {'bp_rows':<8} {'skus':<6} "
        f"{'dup':<5} {'cd1':<5} {'cd2+':<5} {'rcvd1':<7} {'open1':<6} flags"
    )
    print(header)
    print("-" * len(header))

    for c in container_list:
        cur.execute("SELECT 1 FROM dbo.pur_ord WHERE RTRIM(pur_num)=?", (c,))
        if not cur.fetchone():
            print(f"{c:<12} NO")
            continue

        cur.execute(
            """
            SELECT pur_cd, COUNT(*), COUNT(DISTINCT RTRIM(prod_cd)),
                   SUM(CASE WHEN ISNULL(rcvd_qty, 0) > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ISNULL(po_qty, 0) > ISNULL(rcvd_qty, 0) THEN 1 ELSE 0 END)
            FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
            GROUP BY pur_cd
            ORDER BY pur_cd
            """,
            (c,),
        )
        by_cd = {int(row[0]): row for row in cur.fetchall()}
        total_rows = sum(row[1] for row in by_cd.values())
        total_skus = max((row[2] for row in by_cd.values()), default=0)

        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT RTRIM(prod_cd) AS prod_cd
                FROM dbo.bin_pur
                WHERE RTRIM(pur_num) = ?
                GROUP BY RTRIM(prod_cd)
                HAVING COUNT(*) > 1
            ) d
            """,
            (c,),
        )
        dup_count = int(cur.fetchone()[0] or 0)

        cd1 = by_cd.get(PO_LINE_PUR_CD)
        cd2_rows = sum(row[1] for key, row in by_cd.items() if key != PO_LINE_PUR_CD)

        flags = []
        if dup_count:
            flags.append("DUP")
        if cd2_rows:
            flags.append("PUR2+")

        print(
            f"{c:<12} YES  {total_rows:<8} {total_skus:<6} {dup_count:<5} "
            f"{cd1[1] if cd1 else 0:<5} {cd2_rows:<5} "
            f"{int(cd1[3]) if cd1 else 0:<7} {int(cd1[4]) if cd1 else 0:<6} "
            f"{' '.join(flags)}"
        )

    print("\n=== EXCESS STOCK (in_stock > PUR_CD=1 rcvd, with PUR_CD<>1 rcvd) ===")
    any_excess = False
    for c in container_list:
        cur.execute("SELECT 1 FROM dbo.bin_pur WHERE RTRIM(pur_num)=?", (c,))
        if not cur.fetchone():
            continue
        cur.execute(
            """
            SELECT RTRIM(bp.prod_cd),
                   MAX(id.in_stock) AS stock,
                   SUM(CASE WHEN bp.pur_cd = 1 THEN ISNULL(bp.rcvd_qty, 0) ELSE 0 END) AS rcvd_cd1,
                   SUM(CASE WHEN bp.pur_cd <> 1 THEN ISNULL(bp.rcvd_qty, 0) ELSE 0 END) AS rcvd_other,
                   SUM(CASE WHEN bp.pur_cd = 1 THEN ISNULL(bp.po_qty, 0) ELSE 0 END) AS po_cd1
            FROM dbo.bin_pur bp
            JOIN dbo.inv i ON RTRIM(i.prod_cd) = RTRIM(bp.prod_cd)
            JOIN dbo.inv_data id ON id.prod_cd = i.prod_cd AND RTRIM(id.whs_num) = '01'
            WHERE RTRIM(bp.pur_num) = ?
            GROUP BY RTRIM(bp.prod_cd)
            HAVING MAX(id.in_stock) > SUM(CASE WHEN bp.pur_cd = 1 THEN ISNULL(bp.rcvd_qty, 0) ELSE 0 END)
               AND SUM(CASE WHEN bp.pur_cd <> 1 THEN ISNULL(bp.rcvd_qty, 0) ELSE 0 END) > 0
            ORDER BY RTRIM(bp.prod_cd)
            """,
            (c,),
        )
        rows = cur.fetchall()
        if not rows:
            continue
        any_excess = True
        print(f"\n{c}: {len(rows)} SKU(s)")
        for prod_cd, stock, rcvd_cd1, rcvd_other, po_cd1 in rows[:10]:
            excess = int(stock or 0) - int(rcvd_cd1 or 0)
            print(
                f"  {prod_cd:<20} stock={int(stock or 0):>5}  "
                f"rcvd_cd1={int(rcvd_cd1 or 0):>5}  rcvd_other={int(rcvd_other or 0):>5}  "
                f"po_cd1={int(po_cd1 or 0):>5}  excess~{excess}"
            )
        if len(rows) > 10:
            print(f"  ... +{len(rows) - 10} more")

    if not any_excess:
        print("None found.")

    print("\n=== PUR_CD<>1 ROWS STILL PRESENT ===")
    any_pur2 = False
    for c in container_list:
        cur.execute(
            """
            SELECT pur_cd, COUNT(*), SUM(ISNULL(rcvd_qty, 0)), SUM(ISNULL(po_qty, 0))
            FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ? AND pur_cd <> ?
            GROUP BY pur_cd
            ORDER BY pur_cd
            """,
            (c, PO_LINE_PUR_CD),
        )
        rows = cur.fetchall()
        if rows:
            any_pur2 = True
            summary = [
                (int(r[0]), int(r[1]), float(r[2] or 0), float(r[3] or 0))
                for r in rows
            ]
            print(f"{c}: {summary}")

    if not any_pur2:
        print("None found.")

    print("\n=== on_order_qty OUT OF SYNC (container SKUs only) ===")
    any_sync = False
    for c in container_list:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT RTRIM(bp.prod_cd) AS sku,
                       ISNULL(id.on_order_qty, 0) AS on_order,
                       ISNULL(open_po.total_open, 0) AS expected
                FROM dbo.bin_pur bp
                JOIN dbo.inv i ON RTRIM(i.prod_cd) = RTRIM(bp.prod_cd)
                JOIN dbo.inv_data id ON id.prod_cd = i.prod_cd AND RTRIM(id.whs_num) = '01'
                LEFT JOIN (
                    SELECT RTRIM(b2.prod_cd) AS prod_cd,
                           SUM(ISNULL(b2.po_qty, 0)) AS total_open
                    FROM dbo.bin_pur b2
                    WHERE b2.pur_cd = 1
                      AND ISNULL(b2.rcvd_qty, 0) = 0
                      AND ISNULL(b2.po_qty, 0) > 0
                      AND RTRIM(b2.pur_num) NOT IN (
                          SELECT DISTINCT RTRIM(b3.pur_num)
                          FROM dbo.bin_pur b3
                          WHERE b3.pur_cd = 1
                            AND RTRIM(b3.prod_cd) = RTRIM(b2.prod_cd)
                            AND ISNULL(b3.rcvd_qty, 0) > 0
                      )
                    GROUP BY RTRIM(b2.prod_cd)
                ) open_po ON RTRIM(i.prod_cd) = open_po.prod_cd
                WHERE RTRIM(bp.pur_num) = ?
                GROUP BY RTRIM(bp.prod_cd), id.on_order_qty, open_po.total_open
            ) x
            WHERE x.on_order <> x.expected
            """,
            (c,),
        )
        cnt = int(cur.fetchone()[0] or 0)
        if cnt:
            any_sync = True
            print(f"{c}: {cnt} SKU(s)")

    if not any_sync:
        print("None found for container SKU sets.")

    print("\n=== LANDED CONTAINERS: stock = 2x rcvd_cd1 (classic double-receive) ===")
    any_double = False
    for c in container_list:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT RTRIM(bp.prod_cd) AS prod_cd
                FROM dbo.bin_pur bp
                JOIN dbo.inv i ON RTRIM(i.prod_cd) = RTRIM(bp.prod_cd)
                JOIN dbo.inv_data id ON id.prod_cd = i.prod_cd AND RTRIM(id.whs_num) = '01'
                WHERE RTRIM(bp.pur_num) = ? AND bp.pur_cd = 1
                  AND ISNULL(bp.rcvd_qty, 0) > 0
                GROUP BY RTRIM(bp.prod_cd), id.in_stock
                HAVING MAX(id.in_stock) = 2 * SUM(CASE WHEN bp.pur_cd = 1 THEN ISNULL(bp.rcvd_qty, 0) ELSE 0 END)
            ) d
            """,
            (c,),
        )
        cnt = int(cur.fetchone()[0] or 0)
        if cnt:
            any_double = True
            print(f"{c}: {cnt} SKU(s) with stock exactly 2x received")

    if not any_double:
        print("None found.")

    conn.close()


if __name__ == "__main__":
    containers = [f"F2611{i:02d}" for i in range(12)]
    audit_containers(containers)
