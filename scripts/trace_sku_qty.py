"""Trace SKU quantity by PO and ship wave."""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.loris.lori_automation import get_db_connection
from scripts.compare_loris_po_list import BATCH_DATE, parse_delivery_date, USER_LIST

SHIP_BEFORE = datetime(2026, 6, 5)


def main():
    sku = sys.argv[1] if len(sys.argv) > 1 else "EN2028-BK"

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT o.po_number, o.delivery_date, i.vendor_item, i.color, i.quantity
        FROM lori_orders o
        JOIN lori_order_items i ON o.po_number = i.po_number
        WHERE o.batch_date = %s AND o.status = 'pending'
          AND i.vendor_item = %s
        ORDER BY o.delivery_date, o.po_number
        """,
        (BATCH_DATE, sku),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    wave_0604 = []
    wave_0629 = []
    for r in rows:
        dt = parse_delivery_date(r["delivery_date"])
        if dt and dt.date() < SHIP_BEFORE.date():
            wave_0604.append(r)
        else:
            wave_0629.append(r)

    def report(label, items):
        by_po = defaultdict(int)
        for r in items:
            by_po[r["po_number"]] += int(r["quantity"] or 0)
        total = sum(by_po.values())
        print(f"\n=== {label}: {sku} total={total} ({len(by_po)} POs) ===")
        for po in sorted(by_po):
            ship = next(r["delivery_date"] for r in items if r["po_number"] == po)
            print(f"  {po}  qty={by_po[po]}  ship_date={ship}")
        return total, by_po

    t4, po4 = report("06/04 wave (correct for vendor 94)", wave_0604)
    t9, po9 = report("06/29 wave (NOT in your ship-before filter)", wave_0629)

    print(f"\nIf your pivot counted BOTH waves: {t4} + {t9} = {t4 + t9}")
    print(f"Your pivot shows 96; vendor/DB 06/04 shows {t4}")

    # Check if 96 = 94 + one PO from wrong wave
    if t4 + t9 == 96 or t4 + 2 == 96:
        print("\nLikely cause: included 2 units from 06/29 wave or double-counted 1 PO.")

    # User's 153 PO list - any 0629 PO accidentally included?
    user_pos = {line.strip() for line in USER_LIST.splitlines() if line.strip()}
    po4_in_user = set(po4) & user_pos
    po9_in_user = set(po9) & user_pos
    print(f"\n06/04 {sku} POs in your 153 list: {len(po4_in_user)}/{len(po4)}")
    print(f"06/29 {sku} POs in your 153 list: {len(po9_in_user)}/{len(po9)}")
    if po9_in_user:
        print("  06/29 POs also in your list:", sorted(po9_in_user))

    # POs with EN2028-BK in 0604 but qty breakdown
    qty_dist = defaultdict(list)
    for po, q in po4.items():
        qty_dist[q].append(po)
    print("\nQty per PO distribution (06/04):")
    for q in sorted(qty_dist):
        print(f"  qty={q}: {len(qty_dist[q])} POs")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
