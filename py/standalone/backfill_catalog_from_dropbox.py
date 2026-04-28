#!/usr/bin/env python3
"""
Find Dropbox images for SKUs that lack catalog_original rows, pick highest-resolution
candidate per SKU, copy (convert to JPG if needed) into IMAGE_DAM_CATALOG_DIR, and INSERT into MySQL.

Uses Config paths (Dropbox root + container catalog + legacy Dropbox folder + Burlington highlight).

Preview by default; pass --apply to write files and DB.

Usage (repo root):
  python py/standalone/backfill_catalog_from_dropbox.py
  python py/standalone/backfill_catalog_from_dropbox.py --apply
  python py/standalone/backfill_catalog_from_dropbox.py --apply --limit 5
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from mysql.connector import Error as MySQLError
from mysql.connector.errors import IntegrityError

from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event
from py.utilities.models import ImageAsset, SessionLocal
from py.utilities.setting import Config


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def load_skus(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    raw = text.replace("\n", ",")
    parts = [p.strip() for p in raw.split(",")]
    seen = set()
    ordered: list[str] = []
    for p in parts:
        if not p:
            continue
        key = p.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return ordered


def skus_missing_catalog(wanted: list[str]) -> list[str]:
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
    return [s for s in wanted if s.upper() not in have]


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


def iter_dropbox_images(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        try:
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in IMAGE_EXT:
                    yield p
        except OSError as e:
            print(f"[warn] skip subtree under {root}: {e}")


def stem_matches_sku(stem_upper: str, sku_upper: str) -> bool:
    """Exact stem, or SKU as prefix before _ or - (not arbitrary substring)."""
    if stem_upper == sku_upper:
        return True
    if stem_upper.startswith(sku_upper + "_"):
        return True
    if stem_upper.startswith(sku_upper + "-"):
        return True
    return False


def collect_candidates_for_skus(
    missing_sorted_longest_first: list[str], paths: Iterable[Path]
) -> dict[str, list[Path]]:
    """Attach each path to at most one SKU (longest SKU wins). Keys are UPPERCASE."""
    wanted_order = sorted(
        {s.upper().strip() for s in missing_sorted_longest_first}, key=len, reverse=True
    )
    buckets: dict[str, list[Path]] = {k: [] for k in wanted_order}

    for path in paths:
        stem = path.stem.upper()
        for su_k in wanted_order:
            if stem_matches_sku(stem, su_k):
                buckets[su_k].append(path)
                break
    return buckets


def image_pixels(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.width, img.height
    except Exception as e:
        print(f"[warn] cannot read dimensions {path}: {e}")
        return None


def pick_highest_resolution(paths: list[Path]) -> Path | None:
    best: Path | None = None
    best_mp = -1
    for p in paths:
        dim = image_pixels(p)
        if not dim:
            continue
        w, h = dim
        mp = w * h
        if mp > best_mp:
            best_mp = mp
            best = p
    return best


def catalog_safe_filename(sku_display: str) -> str:
    bad = '<>:"/\\|?*'
    s = sku_display.strip()
    for ch in bad:
        s = s.replace(ch, "_")
    return f"{s}.jpg"


def save_catalog_jpg(src: Path, dest: Path) -> bool:
    """Write dest as JPEG; convert PNG/WebP with alpha to RGB."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    suf = src.suffix.lower()
    try:
        if suf in {".jpg", ".jpeg"}:
            shutil.copy2(src, dest)
            return True
        from PIL import Image

        im = Image.open(src)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            bg.save(dest, quality=92, optimize=True)
            return True
        im = im.convert("RGB")
        im.save(dest, quality=92, optimize=True)
        return True
    except Exception as e:
        print(f"[warn] failed to save catalog JPG from {src}: {e}")
        return False


def sha256_file(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        print(f"[warn] hash failed {path}: {e}")
        return None


def insert_catalog_row(conn, cursor, sku_upper: str, dest_path: Path) -> bool:
    """Insert new catalog_original row (canonical filename on disk)."""
    sha = sha256_file(dest_path)
    if not sha:
        return False

    dim = image_pixels(dest_path)
    if not dim:
        return False
    w, h = dim
    size_b = dest_path.stat().st_size
    filename = dest_path.name

    try:
        cursor.execute(
            "SELECT id, sha256 FROM image_assets WHERE filename = %s AND source = %s",
            (filename, "catalog_original"),
        )
        row = cursor.fetchone()
        if row:
            ex_id, ex_sha = row
            if ex_sha == sha:
                cursor.execute(
                    "UPDATE image_assets SET sku=%s, path=%s, width=%s, height=%s, file_size=%s, "
                    "is_active=TRUE, updated_at=%s WHERE id=%s",
                    (sku_upper, str(dest_path), w, h, size_b, datetime.now(), ex_id),
                )
                conn.commit()
                print(f"  [db] updated existing row id={ex_id} for {filename}")
                return True
            cursor.execute(
                "UPDATE image_assets SET sku=%s, path=%s, sha256=%s, width=%s, height=%s, file_size=%s, "
                "is_active=TRUE, updated_at=%s WHERE id=%s",
                (sku_upper, str(dest_path), sha, w, h, size_b, datetime.now(), ex_id),
            )
            conn.commit()
            print(f"  [db] replaced hash for id={ex_id} {filename}")
            return True

        is_canonical = dest_path.stem.upper() == sku_upper.upper()

        cursor.execute(
            "SELECT id, sku, source FROM image_assets WHERE sha256 = %s LIMIT 1",
            (sha,),
        )
        hash_row = cursor.fetchone()
        if hash_row:
            ex_id, ex_sku, _ex_src = hash_row
            if ex_sku.strip().upper() == sku_upper.upper():
                cursor.execute(
                    """
                    UPDATE image_assets SET
                        source = %s, filename = %s, path = %s,
                        width = %s, height = %s, file_size = %s,
                        is_active = TRUE, is_canonical = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        "catalog_original",
                        filename,
                        str(dest_path),
                        w,
                        h,
                        size_b,
                        is_canonical,
                        datetime.now(),
                        ex_id,
                    ),
                )
                conn.commit()
                print(f"  [db] promoted/relinked id={ex_id} to catalog_original for {sku_upper}")
                return True
            print(f"  [db] skip insert: sha256 owned by sku={ex_sku}")
            return False

        cursor.execute(
            """
            INSERT INTO image_assets
            (sku, version, filename, path, source, width, height, file_size, sha256,
             is_active, is_canonical, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                sku_upper,
                1,
                filename,
                str(dest_path),
                "catalog_original",
                w,
                h,
                size_b,
                sha,
                True,
                is_canonical,
                datetime.now(),
                datetime.now(),
            ),
        )
        conn.commit()
        print(f"  [db] inserted {sku_upper} -> {filename}")
        return True
    except IntegrityError as e:
        if "sha256" in str(e).lower():
            print(f"  [db] skip insert: duplicate sha256 (race or concurrent write)")
        else:
            print(f"  [db] integrity error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    except MySQLError as e:
        print(f"  [db] error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def main() -> None:
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Backfill catalog_original from Dropbox")
    ap.add_argument(
        "--skus-file",
        default=str(base / "catalog_missing_skus_raw.txt"),
        help="Comma-separated SKU list (same format as report_catalog_missing)",
    )
    ap.add_argument("--apply", action="store_true", help="Copy files and write DB (default is preview only)")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N SKUs (after filtering)")
    args = ap.parse_args()

    skus_file = Path(args.skus_file)
    if not skus_file.is_file():
        print(f"SKU list not found: {skus_file}", file=sys.stderr)
        sys.exit(1)

    wanted = load_skus(str(skus_file))
    missing = skus_missing_catalog(wanted)
    missing_sorted = sorted(missing, key=len, reverse=True)

    if args.limit:
        missing_sorted = missing_sorted[: args.limit]

    display_for_upper = {m.upper().strip(): m for m in missing_sorted}

    roots = dropbox_roots()
    catalog_dir = Path(Config.IMAGE_DAM_CATALOG_DIR)

    print(f"Dropbox roots ({len(roots)}): {[str(r) for r in roots]}")
    print(f"Catalog destination: {catalog_dir}")
    print(f"SKUs in list (unique): {len(wanted)}")
    print(f"Still missing catalog_original: {len(missing_sorted)}")
    print(f"Mode: {'APPLY (write disk + DB)' if args.apply else 'DRY RUN (preview only)'}")
    print("-" * 60)

    if not roots:
        print("No Dropbox folders exist — check Config paths.")
        sys.exit(1)

    print("Scanning Dropbox for images (may take a while)...")
    all_paths = list(iter_dropbox_images(roots))
    print(f"Found {len(all_paths)} image files.")

    buckets = collect_candidates_for_skus(missing_sorted, all_paths)

    ok_db = 0
    preview_ok = 0
    skipped_no_file = 0
    skipped_pixels = 0
    skipped_sha_conflict = 0

    for sku_raw in missing_sorted:
        su = sku_raw.upper().strip()
        label = display_for_upper.get(su, sku_raw)
        cands = buckets.get(su, [])
        if not cands:
            print(f"[--] {label}: no Dropbox candidates")
            skipped_no_file += 1
            continue
        best = pick_highest_resolution(cands)
        if not best:
            print(f"[--] {label}: candidates unreadable")
            skipped_pixels += 1
            continue

        dim = image_pixels(best)
        if not dim:
            skipped_pixels += 1
            continue
        dest_name = catalog_safe_filename(sku_raw)
        dest = catalog_dir / dest_name

        tag = "++" if args.apply else ">>"
        print(f"[{tag}] {label}: best={best.name} ({dim[0]}x{dim[1]} px) -> {dest.name}")

        if not args.apply:
            preview_ok += 1
            continue

        src_sha = sha256_file(best)
        if not src_sha:
            skipped_pixels += 1
            continue

        conn_pf = get_mysql_connection()
        if not conn_pf:
            print("  [db] no MySQL connection")
            continue
        cur_pf = conn_pf.cursor(buffered=True)
        try:
            cur_pf.execute(
                "SELECT id, sku FROM image_assets WHERE sha256 = %s LIMIT 1",
                (src_sha,),
            )
            dup_row = cur_pf.fetchone()
            if dup_row:
                ex_id, ex_sku = dup_row
                if ex_sku.strip().upper() != su:
                    print(
                        f"  [~~] image bytes already in DB as sku={ex_sku} (id={ex_id}), skip copy"
                    )
                    skipped_sha_conflict += 1
                    continue
        finally:
            cur_pf.close()
            conn_pf.close()

        if not save_catalog_jpg(best, dest):
            skipped_pixels += 1
            continue

        conn = get_mysql_connection()
        if not conn:
            print("  [db] no MySQL connection")
            continue
        cursor = conn.cursor(buffered=True)
        try:
            if insert_catalog_row(conn, cursor, su, dest):
                ok_db += 1
                try:
                    log_event(
                        "info",
                        f"Backfill catalog from Dropbox: {su} from {best} -> {dest}",
                    )
                except Exception:
                    pass
        finally:
            cursor.close()
            conn.close()

    print("-" * 60)
    if args.apply:
        print(f"Written disk + DB OK: {ok_db}")
    else:
        print(f"Preview (would pick): {preview_ok}")
    print(
        f"No Dropbox candidates: {skipped_no_file}, unreadable / save failed: {skipped_pixels}, "
        f"sha conflict (other SKU): {skipped_sha_conflict}"
    )


if __name__ == "__main__":
    main()
