"""CLI to import container styles CSV into FDC and EFAS."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.utilities.style_efas_transfer import (  # noqa: E402
    fetch_style_rows_from_container_csv,
    transfer_container_styles_to_oms,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import container styles CSV into FDC and EFAS"
    )
    parser.add_argument("--container", required=True, help="Container number, e.g. F261108")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--adjusted-by", default="fdcef", help="UPDT_BY for inv_data")
    args = parser.parse_args()

    container = args.container.strip().upper()
    rows = fetch_style_rows_from_container_csv(container)
    print(f"Container {container}: {len(rows)} style row(s) in CSV")
    for row in rows[:5]:
        print(
            f"  {row.prod_cd} | {row.alt_cd} | cost={row.sales_cost} | "
            f"dims={row.ut_len}/{row.ut_hi}/{row.ut_wi}"
        )
    if len(rows) > 5:
        print(f"  ... and {len(rows) - 5} more")

    result = transfer_container_styles_to_oms(
        container,
        dry_run=args.dry_run,
        adjusted_by=args.adjusted_by,
    )
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\n=== {mode} ===")
    for label, part in ("FDC", result.fdc), ("EFAS", result.efas):
        print(f"{label}: success={part.success} synced={part.to_dict()['synced']}")
        if part.inserted:
            print(f"  inserted ({len(part.inserted)}): {', '.join(part.inserted)}")
        if part.updated:
            print(f"  updated ({len(part.updated)}): {', '.join(part.updated)}")
        if part.skipped:
            print(f"  skipped ({len(part.skipped)}): {', '.join(part.skipped)}")
        if part.errors:
            for err in part.errors:
                print(f"  error: {err}")
    if result.total_skus:
        print(f"total container SKUs: {result.total_skus}")
    if result.missing_from_fdc or result.missing_from_efas:
        print("missing from FDC:", result.missing_from_fdc)
        print("missing from EFAS:", result.missing_from_efas)

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
