"""CLI to transfer FDC style master rows into EFAS."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.utilities.style_efas_transfer import (  # noqa: E402
    fetch_style_rows_from_fdc,
    transfer_styles_to_efas,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Transfer style master from FDC to EFAS")
    parser.add_argument(
        "--style",
        action="append",
        required=True,
        help="Style code(s) to transfer (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview inserts without writing to EFAS",
    )
    parser.add_argument(
        "--adjusted-by",
        default="fdcef",
        help="UPDT_BY value for inv_data (default: fdcef)",
    )
    args = parser.parse_args()

    styles = [code.strip().upper() for code in args.style if code.strip()]
    fdc_rows = fetch_style_rows_from_fdc(styles)
    print(f"FDC rows found: {len(fdc_rows)}")
    for row in fdc_rows:
        print(
            f"  {row.prod_cd} | {row.alt_cd} | {row.descrip} | "
            f"retail={row.retail_prs} cost={row.sales_cost} | "
            f"dims L/H/W={row.ut_len}/{row.ut_hi}/{row.ut_wi}"
        )

    result = transfer_styles_to_efas(
        styles,
        dry_run=args.dry_run,
        adjusted_by=args.adjusted_by,
    )

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\n=== {mode} result ===")
    print(f"success: {result.success}")
    if result.inserted:
        print(f"inserted ({len(result.inserted)}): {', '.join(result.inserted)}")
    if result.skipped:
        print(f"skipped ({len(result.skipped)}): {', '.join(result.skipped)}")
    if result.errors:
        print("errors:")
        for err in result.errors:
            print(f"  - {err}")

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
