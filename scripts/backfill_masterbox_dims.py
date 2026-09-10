"""Backfill masterbox CASE_LEN/WI/HI from packing-list MEASUREMENT into OMS.

Reprocesses Dropbox container Excels → styles CSV (with Case Length/Width/Height),
then updates FDC + EFAS inv_detl for SKUs that have packing-list dimensions.

Examples:
  python scripts/backfill_masterbox_dims.py --dry-run --all
  python scripts/backfill_masterbox_dims.py --all
  python scripts/backfill_masterbox_dims.py --container F26210 --container F26211
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.Toolkits.boat import (  # noqa: E402
    OUTPUT_DIR,
    normalize_container_id,
    process_container_file,
    scan_dropbox_containers,
)
from py.utilities.style_efas_transfer import (  # noqa: E402
    case_dimension_updates_from_styles_csv,
    update_case_dimensions_dual,
)


def _containers_with_styles_csv() -> list[str]:
    pattern = os.path.join(OUTPUT_DIR, "*-styles.csv")
    out = []
    for path in glob.glob(pattern):
        name = os.path.basename(path)
        if name.endswith("-styles.csv"):
            out.append(normalize_container_id(name[: -len("-styles.csv")]))
    return sorted(set(out))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill masterbox dimensions from packing lists into OMS"
    )
    parser.add_argument("--container", action="append", default=[])
    parser.add_argument(
        "--all",
        action="store_true",
        help="All containers that already have a styles CSV (boat-imported)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reprocess CSVs and report OMS updates without writing DB",
    )
    parser.add_argument(
        "--skip-reprocess",
        action="store_true",
        help="Use existing styles CSV Case Length/Width/Height (do not re-read Excel)",
    )
    args = parser.parse_args()

    if args.all:
        containers = _containers_with_styles_csv()
    else:
        containers = [normalize_container_id(c) for c in args.container if c.strip()]

    if not containers:
        print("No containers selected. Use --all or --container F#####")
        return 1

    dropbox = {} if args.skip_reprocess else scan_dropbox_containers()
    app = Flask("backfill_masterbox_dims")

    totals = {
        "containers": 0,
        "with_dims": 0,
        "no_dims": 0,
        "process_fail": 0,
        "not_in_dropbox": 0,
        "sku_updates": 0,
        "fdc_updated": 0,
        "efas_updated": 0,
        "missing": 0,
        "errors": 0,
    }

    with app.app_context():
        for container in containers:
            totals["containers"] += 1
            print(f"\n=== {container} ===")

            if not args.skip_reprocess:
                if container not in dropbox:
                    print("  skip: not found in Dropbox")
                    totals["not_in_dropbox"] += 1
                    continue
                result = process_container_file(dropbox[container], container)
                if not result.get("success"):
                    print(f"  process failed: {result.get('error')}")
                    totals["process_fail"] += 1
                    continue
                preview = result.get("preview") or []
                with_case = sum(
                    1
                    for row in preview
                    if (row.get("case_len") or 0)
                    or (row.get("case_wi") or 0)
                    or (row.get("case_hi") or 0)
                )
                print(f"  reprocessed CSV: {len(preview)} rows, {with_case} with masterbox dims")

            try:
                updates = case_dimension_updates_from_styles_csv(container)
            except FileNotFoundError as exc:
                print(f"  {exc}")
                totals["process_fail"] += 1
                continue

            if not updates:
                print("  no Case Length/Width/Height in styles CSV")
                totals["no_dims"] += 1
                continue

            totals["with_dims"] += 1
            totals["sku_updates"] += len(updates)
            sample = list(updates.items())[:3]
            sample_txt = ", ".join(
                f"{sku}={float(dims[0]):g}x{float(dims[1]):g}x{float(dims[2]):g}"
                for sku, dims in sample
            )
            print(f"  case-dim SKUs: {len(updates)} (e.g. {sample_txt})")

            oms = update_case_dimensions_dual(updates, dry_run=args.dry_run)
            totals["fdc_updated"] += len(oms.get("updated_fdc") or []) + len(
                oms.get("inserted_fdc") or []
            )
            totals["efas_updated"] += len(oms.get("updated_efas") or []) + len(
                oms.get("inserted_efas") or []
            )
            totals["missing"] += len(oms.get("missing") or [])
            totals["errors"] += len(oms.get("errors") or [])

            mode = "DRY-RUN" if args.dry_run else "WROTE"
            print(
                f"  {mode} FDC={len(oms.get('updated_fdc') or [])}+"
                f"{len(oms.get('inserted_fdc') or [])} "
                f"EFAS={len(oms.get('updated_efas') or [])}+"
                f"{len(oms.get('inserted_efas') or [])} "
                f"missing={len(oms.get('missing') or [])}"
            )
            for err in oms.get("errors") or []:
                print(f"  error: {err}")
            if oms.get("missing"):
                show = oms["missing"][:8]
                more = len(oms["missing"]) - len(show)
                print(f"  missing SKUs: {', '.join(show)}" + (f" (+{more} more)" if more else ""))

    print("\n=== SUMMARY ===")
    print(
        f"containers={totals['containers']} with_dims={totals['with_dims']} "
        f"no_dims={totals['no_dims']} process_fail={totals['process_fail']} "
        f"not_in_dropbox={totals['not_in_dropbox']}"
    )
    print(
        f"sku_updates={totals['sku_updates']} "
        f"fdc_writes={totals['fdc_updated']} efas_writes={totals['efas_updated']} "
        f"missing={totals['missing']} errors={totals['errors']}"
        + (" (dry-run)" if args.dry_run else "")
    )
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
