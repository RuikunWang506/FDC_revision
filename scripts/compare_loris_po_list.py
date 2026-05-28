"""Compare user PO list vs lori_orders batch (ship-before filter)."""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

USER_LIST = r"""
0015-004157
0015-004157
0018-005291
0021-003442
0023-005051
0044-004855
0055-005398
0056-006558
0072-003925
0079-005323
0082-005733
0093-002949
0094-006472
0097-004413
0114-004102
0121-005785
0123-005442
0128-004790
0137-007609
0157-006026
0181-004630
0188-008371
0194-004270
0197-004263
0200-004618
0201-005978
0217-004859
0223-004728
0226-005146
0228-004905
0232-006883
0237-004293
0247-005403
0250-004118
0251-004419
0256-004463
0266-003587
0267-003450
0270-004311
0271-004746
0286-004707
0289-003752
0304-005961
0307-003563
0319-003162
0335-003906
0339-005583
0345-005314
0347-005298
0348-006227
0354-004456
0367-005477
0369-004774
0371-004596
0375-003599
0377-005170
0378-004493
0379-004555
0394-004505
0395-003504
0397-005053
0399-004733
0402-006450
0408-004656
0412-004126
0416-004506
0417-003460
0420-003930
0424-004279
0425-005301
0430-007335
0431-005868
0433-004656
0435-005867
0441-004419
0443-005808
0445-003123
0446-004974
0452-003908
0454-004341
0461-005181
0464-004517
0465-005508
0466-003431
0470-005987
0471-003209
0473-002865
0476-003225
0477-003039
0478-003131
0484-003712
0491-003530
0494-003602
0495-005029
0501-003926
0507-004369
0514-006229
0516-004188
0517-003869
0523-005731
0524-004666
0525-004900
0528-005310
0541-004810
0543-005118
0547-003519
0549-003254
0550-003827
0559-003314
0561-003058
0574-006254
0575-002966
0576-005237
0579-004824
0586-004374
0588-005623
0589-003078
0596-004934
0597-005638
0601-006633
0602-006420
0615-002655
0619-002703
0625-004234
0633-001411
0637-001936
0642-001483
0643-001964
0645-000952
0648-002295
0649-002373
0655-000989
0658-000890
0660-001356
0661-000597
0665-000577
0668-000403
0669-000309
0670-001159
0671-000939
0677-000683
0678-000610
0680-000914
0687-000476
0688-000293
0689-000190
0690-000424
0692-000265
0694-000284
0696-000295
0700-000242
0701-000087
0705-000087
0706-000090
"""

BATCH_DATE = "2026-05-27"
SHIP_BEFORE = datetime(2026, 6, 5)


def parse_delivery_date(value):
    if not value or value == "N/A":
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt)
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None


def ship_before_match(delivery_date, before_dt):
    d = parse_delivery_date(delivery_date)
    if not d:
        return False
    return d.date() < before_dt.date()


def main():
    from py.loris.lori_automation import get_db_connection

    user_pos = sorted({line.strip() for line in USER_LIST.splitlines() if line.strip()})
    print(f"User list unique POs: {len(user_pos)}")

    conn = get_db_connection()
    if not conn:
        print("DB connection failed")
        return 1
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT po_number, delivery_date, status
        FROM lori_orders
        WHERE batch_date = %s AND status = 'pending'
        ORDER BY po_number
        """,
        (BATCH_DATE,),
    )
    batch_rows = cur.fetchall()
    cur.close()
    conn.close()

    batch_all = {r["po_number"] for r in batch_rows}
    batch_filtered = {
        r["po_number"]
        for r in batch_rows
        if ship_before_match(r["delivery_date"], SHIP_BEFORE)
    }

    user_set = set(user_pos)
    in_user_not_ui = sorted(user_set - batch_filtered)
    in_ui_not_user = sorted(batch_filtered - user_set)
    in_batch_not_user = sorted(batch_all - user_set)
    in_user_not_batch = sorted(user_set - batch_all)

    print(f"Batch {BATCH_DATE} pending total: {len(batch_all)}")
    print(f"Batch ship-before {SHIP_BEFORE.date()}: {len(batch_filtered)}")
    print()
    print(f"In your list but NOT in UI filter ({len(in_user_not_ui)}):")
    for po in in_user_not_ui:
        row = next((r for r in batch_rows if r["po_number"] == po), None)
        dd = row["delivery_date"] if row else "NOT IN BATCH"
        print(f"  {po}  delivery_date={dd}")
    print()
    print(f"In UI filter but NOT in your list ({len(in_ui_not_user)}):")
    for po in in_ui_not_user:
        row = next(r for r in batch_rows if r["po_number"] == po)
        print(f"  {po}  delivery_date={row['delivery_date']}")
    print()
    print(f"In full batch but NOT in your list ({len(in_batch_not_user)}):")
    for po in in_batch_not_user[:50]:
        row = next(r for r in batch_rows if r["po_number"] == po)
        print(f"  {po}  delivery_date={row['delivery_date']}")
    if len(in_batch_not_user) > 50:
        print(f"  ... and {len(in_batch_not_user) - 50} more")
    print()
    print(f"In your list but NOT in batch at all ({len(in_user_not_batch)}):")
    for po in in_user_not_batch:
        print(f"  {po}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
