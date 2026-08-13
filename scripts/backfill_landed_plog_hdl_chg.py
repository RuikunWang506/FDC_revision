"""Backfill plog.hdl_chg for boat-landed containers still showing as ocean."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.utilities.container_po_import import backfill_plog_hdl_chg_for_landed_containers


def main() -> int:
    containers = [c.strip().upper() for c in sys.argv[1:] if c.strip()] or None
    result = backfill_plog_hdl_chg_for_landed_containers(containers)
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
