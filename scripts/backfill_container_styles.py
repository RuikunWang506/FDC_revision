"""Remove spelled-out-color SKUs for a container and re-import with abbreviations."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.Toolkits.boat import process_container_file, scan_dropbox_containers  # noqa: E402
from py.utilities.style_efas_transfer import backfill_container_styles  # noqa: E402

# SKUs to remove when descrip matches container (spelled-out colors from bad import)
REMOVE_BY_CONTAINER = {
    "F261108": [
        "N2548 BLACK",
        "N2548 CHOCOLATE",
        "N2548 LTTN",
        "N2548 OLIVE",
    ],
    "F261109": [
        "L1901 BLACK",
        "L1901 BROWN",
        "L1901 GREEN",
        "L1901 NAVY",
        "L1901 PURPLE",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill container styles with color abbrevs")
    parser.add_argument("--container", action="append", required=True)
    parser.add_argument("--reprocess", action="store_true", help="Regenerate CSV from Dropbox Excel")
    parser.add_argument("--adjusted-by", default="fdcef")
    args = parser.parse_args()

    containers = [c.strip().upper() for c in args.container]
    files = scan_dropbox_containers() if args.reprocess else {}

    for container in containers:
        if args.reprocess:
            if container not in files:
                print(f"{container}: not found in Dropbox")
                continue
            result = process_container_file(files[container], container)
            if not result.get("success"):
                print(f"{container}: process failed: {result.get('error')}")
                continue
            print(f"{container}: regenerated CSV ({len(result.get('preview', []))} rows)")

        remove = REMOVE_BY_CONTAINER.get(container, [])
        print(f"\n=== {container} ===")
        print(f"Removing (if descrip={container}): {', '.join(remove) or '(none)'}")

        result = backfill_container_styles(
            container,
            remove_skus=remove,
            adjusted_by=args.adjusted_by,
        )
        print(f"success={result.success} total={result.total_skus}")
        for label, part in ("FDC", result.fdc), ("EFAS", result.efas):
            print(f"  {label}: inserted={part.inserted} updated={part.updated}")
            if part.errors:
                for err in part.errors:
                    print(f"    error: {err}")
        if result.missing_from_fdc or result.missing_from_efas:
            print(f"  missing FDC: {result.missing_from_fdc}")
            print(f"  missing EFAS: {result.missing_from_efas}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
