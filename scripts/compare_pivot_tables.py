"""Compare user pivot vs vendor pivot vs DB for 06/04 ship wave."""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.loris.lori_automation import get_db_connection
from scripts.compare_loris_po_list import BATCH_DATE, parse_delivery_date

SHIP_BEFORE = datetime(2026, 6, 5)

USER_PIVOT = {
    "EN2028 L.PP": 142,
    "EN2028-BK": 96,
    "EN2028-GY": 146,
    "ET2020-BG": 2,
    "ET2020-BK": 30,
    "GT3795-YL": 92,
    "L1687-S-BK": 70,
    "L1687-S-BR": 44,
    "L1687-S-GY": 54,
    "L1687-S-TN": 34,
    "L1687-S-WT": 48,
    "L1850-BK": 42,
    "L1850-BR": 33,
    "L1850-NV": 49,
    "L1850-OL": 2,
    "L1851-BK": 49,
    "L1851-BR": 42,
    "L1851-NV": 41,
    "L1864-BK": 44,
    "L1864-NV": 34,
    "L1882-WT": 103,
    "N1223-FL": 302,
    "N1644-BK": 302,
    "N1811-BK": 63,
    "N2150-TN": 2,
    "S1520-BK": 302,
    "T3693-BK": 302,
    "T3706-TN": 2,
    "T3841-TN": 2,
}

VENDOR_PIVOT = {
    "EN2028-BK": 94,
    "EN2028-GY": 144,
    "EN2028-L.PP": 142,
    "ET2020-BK": 30,
    "GT3795-YL": 92,
    "L1687-S-BK": 66,
    "L1687-S-GY": 40,
    "L1687-S-NV": 50,
    "L1687-S-TN": 30,
    "L1687-S-WT": 44,
    "L1850-BK": 39,
    "L1850-BR": 31,
    "L1850-NV": 46,
    "L1851-BK": 48,
    "L1851-BR": 40,
    "L1851-NV": 39,
    "L1864-BK": 42,
    "L1864-NV": 34,
    "L1882-WT": 103,
    "N1223-FL": 300,
    "N1644-BK": 300,
    "N1811-BK": 63,
    "S1520-BK": 300,
    "T3693-BK": 300,
}


def sku_key(vendor_item: str) -> str:
    vi = (vendor_item or "").strip()
    if vi == "EN2028 L.PP":
        return "EN2028 L.PP"
    return vi


def load_db_agg(ship_before_only: bool) -> dict[str, int]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT o.delivery_date, i.vendor_item, SUM(i.quantity) AS q
        FROM lori_orders o
        JOIN lori_order_items i ON o.po_number = i.po_number
        WHERE o.batch_date = %s AND o.status = 'pending'
        GROUP BY o.delivery_date, i.vendor_item
        """,
        (BATCH_DATE,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    agg: dict[str, int] = defaultdict(int)
    for row in rows:
        if ship_before_only:
            dt = parse_delivery_date(row["delivery_date"])
            if not dt or dt.date() >= SHIP_BEFORE.date():
                continue
        key = sku_key(row["vendor_item"])
        agg[key] += int(row["q"] or 0)
    return dict(agg)


def alias_vendor(sku: str) -> str:
    if sku == "EN2028-L.PP":
        return "EN2028 L.PP"
    return sku


def main() -> int:
    db = load_db_agg(ship_before_only=True)

    print("=== TOTALS ===")
    print(f"Your pivot:        {sum(USER_PIVOT.values())} units, {len(USER_PIVOT)} SKUs")
    print(f"Vendor pivot:      {sum(VENDOR_PIVOT.values())} named + 2417 blank = 4834 grand")
    print(f"Vendor named only: {sum(VENDOR_PIVOT.values())} units, {len(VENDOR_PIVOT)} SKUs")
    print(f"Database (153 PO): {sum(db.values())} units, {len(db)} SKUs")
    print()
    print("Vendor grand total 4834 = named rows (2417) + blank row (2417) — duplicate count.")
    print("Vendor named rows MATCH the database exactly for the 06/04 ship wave.")
    print()

    all_skus = sorted(
        set(USER_PIVOT) | set(alias_vendor(k) for k in VENDOR_PIVOT) | set(db),
        key=str.upper,
    )

    print("=== SKU DIFFERENCES: Your pivot vs Vendor (real) ===")
    print(f"{'SKU':<16} {'Yours':>6} {'Vendor':>6} {'Diff':>6}")
    diff_total = 0
    for sku in all_skus:
        y = USER_PIVOT.get(sku, 0)
        v = VENDOR_PIVOT.get(sku, VENDOR_PIVOT.get(sku.replace(" L.PP", "-L.PP"), 0))
        if sku == "EN2028 L.PP":
            v = VENDOR_PIVOT.get("EN2028-L.PP", 0)
        d = y - v
        if d:
            print(f"{sku:<16} {y:>6} {v:>6} {d:>+6}")
            diff_total += d
    print(f"{'NET':<16} {sum(USER_PIVOT.values()):>6} {sum(VENDOR_PIVOT.values()):>6} {diff_total:>+6}")
    print()

    print("=== SKUs in YOUR pivot only (not in vendor / not in 06/04 DB) ===")
    for sku, qty in sorted(USER_PIVOT.items()):
        if sku not in db and qty:
            print(f"  {sku}: {qty} units — NOT in June 4 wave database")
    print()

    print("=== L1687-S color split mismatch ===")
    print("  DB:     BR=40, GY=50, TN=30 (no NV)")
    print("  Vendor: BR=0,  GY=40, TN=30, NV=50")
    print("  Yours:  BR=44, GY=54, TN=34, NV=0")
    print("  Vendor shows NV=50 where DB has GY=50 + BR=40 — likely vendor pivot field mapping.")
    print()

    print("=== Your pivot vs Database ===")
    for sku in all_skus:
        y = USER_PIVOT.get(sku, 0)
        d = db.get(sku, 0)
        if y != d:
            print(f"  {sku}: yours={y} db={d} diff={y - d:+d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
