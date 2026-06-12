"""
Create ERP-style manual inventory adjustments in omsdata_FDC / oms_EF.

Usage:
  python scripts/create_inventory_adjustment.py --dry-run --sku "N1555 BK" --qty 5
  python scripts/create_inventory_adjustment.py --apply --sku "N1555 BK" --qty 5 --by fdcef
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections
from py.utilities.inventory_adjustment import apply_adjustment, DEFAULT_ADJUSTED_BY


def print_plan(plan: dict, dry_run: bool):
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"\n=== {mode}: inventory adjustment ===")
    print(f"SKU:           {plan['sku']}")
    print(f"Log #:         {plan['log_num']}  line nt_num={plan['nt_num']}")
    print(f"Quantity:      {plan['qty']:+d}")
    print(f"Adjusted by:   {plan['adjusted_by']}")
    print(f"Warehouse/bin: {plan['warehouse']} / {plan['bin']}")
    print(
        f"Stock before:  in_stock={plan['before']['in_stock']}  "
        f"available={plan['before']['available']}  bin_sum={plan['before']['bin_sum']}"
    )
    if dry_run:
        print(
            f"Stock after:   in_stock={plan['after_in_stock']}  "
            f"available={plan['after_available']}  (expected)"
        )
    else:
        after = plan["after"]
        print(
            f"Stock after:   in_stock={after['in_stock']}  "
            f"available={after['available']}  bin_sum={after['bin_sum']}"
        )
        print(f"wm_log rec_num: {plan['rec_num']}")


def main():
    parser = argparse.ArgumentParser(description="Create ERP manual inventory adjustment")
    parser.add_argument("--sku", help='SKU including color, e.g. "N1555 BK"')
    parser.add_argument("--qty", type=int, help="Adjustment quantity (+/-)")
    parser.add_argument("--by", default="", help=f"User initials (default {DEFAULT_ADJUSTED_BY})")
    parser.add_argument("--whs", default="01", help="Warehouse number (default 01)")
    parser.add_argument(
        "--database",
        choices=("FDC", "E_FAS"),
        default="FDC",
        help="Target database (default FDC)",
    )
    parser.add_argument("--log-num", help="Existing log number to append a line to")
    parser.add_argument("--nt-num", type=int, default=1, help="Line number within log batch")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only")
    group.add_argument("--apply", action="store_true", help="Write to database")
    args = parser.parse_args()

    if not args.sku:
        args.sku = input("SKU (e.g. N1555 BK): ").strip()
    if args.qty is None:
        args.qty = int(input("Adjustment quantity (+/- integer): ").strip())
    if not args.by:
        args.by = input(f"Adjusted by [{DEFAULT_ADJUSTED_BY}]: ").strip() or DEFAULT_ADJUSTED_BY

    conn_fdc, conn_ef = get_db_connections()
    conn = conn_fdc if args.database == "FDC" else conn_ef
    if not conn:
        print("Database connection failed", file=sys.stderr)
        return 1

    try:
        plan = apply_adjustment(
            conn,
            sku=args.sku,
            qty=args.qty,
            adjusted_by=args.by or DEFAULT_ADJUSTED_BY,
            whs_num=args.whs,
            log_num=args.log_num,
            nt_num=args.nt_num,
            dry_run=not args.apply,
            database_label=args.database,
        )
        print_plan(plan, dry_run=not args.apply)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn_fdc:
            conn_fdc.close()
        if conn_ef:
            conn_ef.close()


if __name__ == "__main__":
    raise SystemExit(main())
