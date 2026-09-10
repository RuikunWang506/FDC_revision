"""Backfill native OMS receive rows (PUR_CD=2 bin_pur + receive-day plog) for boat-landed POs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.utilities.container_po_import import backfill_oms_receive_pattern_for_landed_containers


def main() -> int:
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    dry_run = "--dry-run" in args
    containers = [a.upper() for a in args if not a.startswith("--")] or None
    result = backfill_oms_receive_pattern_for_landed_containers(
        containers, dry_run=dry_run
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
