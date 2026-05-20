#!/usr/bin/env python3
"""
Build a searchable index of image files under Config Dropbox roots.

Extracts SKU as: leading letters + exactly 4 digits (e.g. GN2432, T3689, N1151).
Handles stems like GN2596版2 via prefix match on the stem start.

Output: instance/dropbox_image_index.json
  - by_sku: { "GN2432": [ {path, width, height, pixels, size_bytes, mtime}, ... ] }

Usage (repo root):
  python py/standalone/build_dropbox_image_index.py
  python py/standalone/build_dropbox_image_index.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from py.utilities.setting import Config

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
SKU_RE = re.compile(r"^([A-Z]+)(\d{4})")
DEFAULT_INDEX = Path(__file__).resolve().parents[2] / "instance" / "dropbox_image_index.json"


def dropbox_roots() -> list[Path]:
    candidates = [
        getattr(Config, "IMAGE_DAM_DROPBOX_DIR", None),
        getattr(Config, "DROPBOX_CONTAINER_CATALOG_DIR", None),
        getattr(Config, "LOCAL_DROPBOX_DIR", None),
        getattr(Config, "BURLINGTON_HIGHLIGHT", None),
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if not p.is_dir():
            continue
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def extract_sku(stem: str) -> str | None:
    """Letters + 4 digits at start of stem (before 版, _, -, space, etc.)."""
    base = stem.upper().strip()
    base = re.sub(r"_V\d+$", "", base)
    m = SKU_RE.match(base)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}"


def image_meta(path: Path) -> dict | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.width, img.height
        st = path.stat()
        return {
            "path": str(path),
            "width": w,
            "height": h,
            "pixels": w * h,
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
        }
    except Exception:
        return None


def build_index(roots: list[Path], progress_every: int = 5000) -> dict:
    by_sku: dict[str, list[dict]] = {}
    scanned = 0
    indexed = 0
    skipped_no_sku = 0
    skipped_bad_image = 0
    t0 = time.time()

    for root in roots:
        print(f"Scanning {root} ...")
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() not in IMAGE_EXT:
                    continue
                scanned += 1
                if scanned % progress_every == 0:
                    elapsed = time.time() - t0
                    rate = scanned / elapsed if elapsed > 0 else 0
                    print(
                        f"  … {scanned:,} files, {indexed:,} indexed, "
                        f"{len(by_sku):,} SKUs ({rate:.0f} files/s)"
                    )

                sku = extract_sku(path.stem)
                if not sku:
                    skipped_no_sku += 1
                    continue

                meta = image_meta(path)
                if not meta:
                    skipped_bad_image += 1
                    continue

                meta["sku"] = sku
                by_sku.setdefault(sku, []).append(meta)
                indexed += 1

    elapsed = time.time() - t0
    return {
        "built_at": datetime.now().isoformat(),
        "roots": [str(r) for r in roots],
        "stats": {
            "files_scanned": scanned,
            "entries_indexed": indexed,
            "unique_skus": len(by_sku),
            "skipped_no_sku": skipped_no_sku,
            "skipped_bad_image": skipped_bad_image,
            "elapsed_seconds": round(elapsed, 1),
        },
        "by_sku": by_sku,
    }


def index_fingerprint(roots: list[Path]) -> str:
    parts = "|".join(str(r.resolve()) for r in roots)
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build Dropbox image index for catalog backfill")
    ap.add_argument(
        "--output",
        default=str(DEFAULT_INDEX),
        help=f"Index JSON path (default: {DEFAULT_INDEX})",
    )
    ap.add_argument("--force", action="store_true", help="Rebuild even if index exists")
    args = ap.parse_args()

    out_path = Path(args.output)
    roots = dropbox_roots()
    if not roots:
        print("No Dropbox roots found — check Config paths.", file=sys.stderr)
        sys.exit(1)

    fp = index_fingerprint(roots)
    if out_path.is_file() and not args.force:
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if existing.get("fingerprint") == fp:
                stats = existing.get("stats", {})
                print(f"Index already exists ({out_path})")
                print(
                    f"  SKUs: {stats.get('unique_skus')}, built: {existing.get('built_at')}"
                )
                print("  Use --force to rebuild.")
                return
        except Exception:
            pass

    print(f"Building index → {out_path}")
    print(f"Roots: {[str(r) for r in roots]}")
    data = build_index(roots)
    data["fingerprint"] = fp

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)

    stats = data["stats"]
    print("-" * 60)
    print(f"Done in {stats['elapsed_seconds']}s")
    print(f"  Scanned:  {stats['files_scanned']:,}")
    print(f"  Indexed:  {stats['entries_indexed']:,}")
    print(f"  SKUs:     {stats['unique_skus']:,}")
    print(f"  Written:  {out_path}")


if __name__ == "__main__":
    main()
