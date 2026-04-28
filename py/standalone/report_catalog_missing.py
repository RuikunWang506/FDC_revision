#!/usr/bin/env python3
"""
Report which SKUs from a comma-separated list lack an active catalog_original asset in DAM.

Usage (from repo root):
  python py/standalone/report_catalog_missing.py
  python py/standalone/report_catalog_missing.py path/to/skus.txt

Default input: py/standalone/catalog_missing_skus_raw.txt
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from py.utilities.models import ImageAsset, SessionLocal


def load_skus(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    raw = text.replace("\n", ",")
    parts = [p.strip() for p in raw.split(",")]
    seen = set()
    ordered = []
    for p in parts:
        if not p:
            continue
        key = p.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return ordered


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, "catalog_missing_skus_raw.txt")
    if not os.path.isfile(path):
        print(f"Input not found: {path}", file=sys.stderr)
        sys.exit(1)

    wanted = load_skus(path)
    db = SessionLocal()
    try:
        rows = (
            db.query(ImageAsset.sku)
            .filter(
                ImageAsset.source == "catalog_original",
                ImageAsset.is_active == True,  # noqa: E712
            )
            .distinct()
            .all()
        )
        have = {r[0].strip().upper() for r in rows if r[0]}
    finally:
        db.close()

    missing = [s for s in wanted if s.upper() not in have]
    print(f"Unique SKUs in list: {len(wanted)}")
    print(f"Distinct SKUs with active catalog_original in DB: {len(have)}")
    print(f"Missing catalog picture (no active catalog_original row): {len(missing)}")
    print("-" * 60)
    for s in missing:
        print(s)


if __name__ == "__main__":
    main()
