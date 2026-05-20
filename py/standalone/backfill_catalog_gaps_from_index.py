#!/usr/bin/env python3
"""
Find small gaps in catalog_original (per SKU prefix: letters + 4 digits),
match missing SKUs against dropbox_image_index.json, import highest-resolution file.

Usage (repo root):
  python py/standalone/build_dropbox_image_index.py
  python py/standalone/backfill_catalog_gaps_from_index.py              # preview
  python py/standalone/backfill_catalog_gaps_from_index.py --apply      # write disk + DB
  python py/standalone/backfill_catalog_gaps_from_index.py --max-span 10 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from py.standalone.backfill_catalog_from_dropbox import (
    catalog_safe_filename,
    insert_catalog_row,
    save_catalog_jpg,
    sha256_file,
)
from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event
from py.utilities.setting import Config

DEFAULT_INDEX = Path(__file__).resolve().parents[2] / "instance" / "dropbox_image_index.json"
CATALOG_DIR = Path(Config.IMAGE_DAM_CATALOG_DIR)
SKU_FILE_RE = re.compile(r"^([A-Z]+)(\d{4})")


def load_index(path: Path) -> dict:
    if not path.is_file():
        print(f"Index not found: {path}", file=sys.stderr)
        print("Run: python py/standalone/build_dropbox_image_index.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_numbers_by_prefix() -> dict[str, set[int]]:
    """Existing catalog SKUs grouped by letter prefix (4-digit numeric part)."""
    by_prefix: dict[str, set[int]] = {}
    for p in CATALOG_DIR.glob("*.jpg"):
        m = SKU_FILE_RE.match(p.stem.upper())
        if not m:
            continue
        pref, num = m.group(1), int(m.group(2))
        by_prefix.setdefault(pref, set()).add(num)
    return by_prefix


def has_catalog_file(sku: str) -> bool:
    """True if GN2432.jpg or GN2432_v2.jpg etc. exists."""
    upper = sku.upper()
    if (CATALOG_DIR / catalog_safe_filename(sku)).exists():
        return True
    return any(CATALOG_DIR.glob(f"{upper}_v*.jpg"))


def neighbor_gaps(by_prefix: dict[str, set[int]], max_span: int) -> list[str]:
    """SKUs missing from catalog between two close existing numbers (same prefix)."""
    gaps: list[str] = []
    for pref, nums in sorted(by_prefix.items()):
        ordered = sorted(nums)
        for i in range(len(ordered) - 1):
            a, b = ordered[i], ordered[i + 1]
            if b - a > max_span:
                continue
            for n in range(a + 1, b):
                sku = f"{pref}{n:04d}"
                if not has_catalog_file(sku):
                    gaps.append(sku)
    return gaps


def score_entry(entry: dict, sku: str) -> float:
    """Prefer catalog-style sheets over dimension/detail lifestyle shots."""
    path = entry.get("path", "")
    stem = Path(path).stem.upper()
    su = sku.upper()
    score = float(entry.get("pixels", 0))

    if stem == su or stem.startswith(su + "版") or stem.startswith(su + "_"):
        score += 10_000_000
    path_u = path.upper()
    if "FDC ARCHIVES" in path_u or "CONTAINER CATALOG" in path_u:
        score += 5_000_000
    w, h = entry.get("width", 0), entry.get("height", 0)
    if 3000 <= w <= 4000 and 2000 <= h <= 2800:
        score += 3_000_000
    if re.search(r"\d+\.?\d*X\d+", stem):
        score -= 20_000_000
    return score


def pick_best_entry(entries: list[dict], sku: str) -> dict | None:
    if not entries:
        return None
    return max(entries, key=lambda e: (score_entry(e, sku), e.get("size_bytes", 0)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill catalog gaps using Dropbox index")
    ap.add_argument("--index", default=str(DEFAULT_INDEX), help="Path to dropbox_image_index.json")
    ap.add_argument(
        "--max-span",
        type=int,
        default=10,
        help="Only fill gaps when neighbor catalog numbers are within this span (default 10)",
    )
    ap.add_argument("--apply", action="store_true", help="Copy files and write MySQL rows")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N SKUs")
    args = ap.parse_args()

    index_path = Path(args.index)
    data = load_index(index_path)
    by_sku: dict[str, list[dict]] = data.get("by_sku", {})
    stats = data.get("stats", {})
    print(f"Index: {index_path} (built {data.get('built_at')}, {stats.get('unique_skus')} SKUs)")
    print(f"Catalog: {CATALOG_DIR}")
    print(f"Max neighbor span: {args.max_span}")
    print(f"Mode: {'APPLY' if args.apply else 'PREVIEW'}")
    print("-" * 60)

    by_prefix = catalog_numbers_by_prefix()
    gaps = neighbor_gaps(by_prefix, args.max_span)
    if args.limit:
        gaps = gaps[: args.limit]

    print(f"Gap SKUs to check (missing file in catalog_original): {len(gaps)}")

    found = 0
    applied = 0
    no_index = 0
    skipped_exists = 0

    for sku in gaps:
        if has_catalog_file(sku):
            skipped_exists += 1
            continue

        entries = by_sku.get(sku.upper(), [])
        if not entries:
            no_index += 1
            continue

        best = pick_best_entry(entries, sku)
        if not best:
            no_index += 1
            continue

        src = Path(best["path"])
        if not src.is_file():
            no_index += 1
            continue

        found += 1
        w, h = best.get("width"), best.get("height")
        tag = "++" if args.apply else ">>"
        print(
            f"[{tag}] {sku}: {src.name} ({w}x{h}) "
            f"← {len(entries)} indexed, from …{str(src)[-55:]}"
        )

        if not args.apply:
            continue

        dest = CATALOG_DIR / catalog_safe_filename(sku)
        if not save_catalog_jpg(src, dest):
            print(f"  [!!] failed to save {dest}")
            continue

        conn = get_mysql_connection()
        if not conn:
            print("  [!!] no MySQL connection")
            continue
        cur = conn.cursor(buffered=True)
        try:
            if insert_catalog_row(conn, cur, sku.upper(), dest):
                applied += 1
                try:
                    log_event("info", f"Gap backfill catalog: {sku} from {src} -> {dest}")
                except Exception:
                    pass
        finally:
            cur.close()
            conn.close()

    print("-" * 60)
    print(f"In Dropbox index: {found}")
    print(f"Not in index:     {no_index}")
    if args.apply:
        print(f"Written to disk+DB: {applied}")
    else:
        print("Run with --apply to import.")


if __name__ == "__main__":
    main()
