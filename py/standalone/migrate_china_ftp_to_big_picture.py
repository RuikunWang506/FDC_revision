"""One-time migration: move china_ftp_downloads cache files to D:\\Individual_big_picture."""
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from py.utilities.models import ImageAsset, SessionLocal
from py.utilities.setting import Config

BIG_PICTURE_DIR = Config.IMAGE_DAM_BIG_PICTURE_DIR
CACHE_DIR = ROOT / "output" / "china_ftp_downloads"


def _resolve_source_path(asset_path: str) -> Optional[str]:
    if not asset_path:
        return None
    normalized = os.path.normpath(asset_path)
    if os.path.isfile(normalized):
        return normalized
    # Relative paths stored from master_image_viewer (..\..\output\...)
    candidate = os.path.normpath(os.path.join(ROOT, "py", "other", asset_path))
    if os.path.isfile(candidate):
        return candidate
    basename = os.path.basename(normalized)
    cache_candidate = CACHE_DIR / basename
    if cache_candidate.is_file():
        return str(cache_candidate)
    return None


def migrate_db_records(db) -> dict:
    stats = {"updated": 0, "copied": 0, "missing_source": 0, "cache_removed": 0}
    rows = (
        db.query(ImageAsset)
        .filter(ImageAsset.path.like("%china_ftp_downloads%"))
        .all()
    )
    for asset in rows:
        dest_dir = os.path.join(BIG_PICTURE_DIR, asset.sku)
        dest_path = os.path.join(dest_dir, asset.filename)
        os.makedirs(dest_dir, exist_ok=True)

        src_path = _resolve_source_path(asset.path)
        if src_path:
            if not os.path.isfile(dest_path):
                shutil.copy2(src_path, dest_path)
                stats["copied"] += 1
            if os.path.normcase(src_path) != os.path.normcase(dest_path):
                try:
                    os.remove(src_path)
                    stats["cache_removed"] += 1
                except OSError:
                    pass
        elif not os.path.isfile(dest_path):
            stats["missing_source"] += 1
            continue

        if asset.path != dest_path:
            asset.path = dest_path
            stats["updated"] += 1

    db.commit()
    return stats


def migrate_orphan_cache_files(db) -> dict:
    """Copy cache files not referenced in DB (best-effort SKU prefix parse)."""
    stats = {"orphans_copied": 0, "orphans_skipped": 0}
    if not CACHE_DIR.is_dir():
        return stats

    known_filenames = {
        (a.sku, a.filename)
        for a in db.query(ImageAsset.sku, ImageAsset.filename)
        .filter(ImageAsset.source == "big_picture")
        .all()
    }

    for entry in CACHE_DIR.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if "_" not in name:
            stats["orphans_skipped"] += 1
            continue
        sku, filename = name.split("_", 1)
        sku = sku.upper()
        if (sku, filename) in known_filenames:
            continue

        dest_dir = os.path.join(BIG_PICTURE_DIR, sku)
        dest_path = os.path.join(dest_dir, filename)
        os.makedirs(dest_dir, exist_ok=True)
        if not os.path.isfile(dest_path):
            shutil.copy2(str(entry), dest_path)
            stats["orphans_copied"] += 1
        try:
            entry.unlink()
        except OSError:
            pass

    return stats


def main():
    print(f"Cache dir: {CACHE_DIR}")
    print(f"Big picture dir: {BIG_PICTURE_DIR}")
    db = SessionLocal()
    try:
        db_stats = migrate_db_records(db)
        orphan_stats = migrate_orphan_cache_files(db)
        print("DB migration:", db_stats)
        print("Orphan cache:", orphan_stats)

        remaining = len(list(CACHE_DIR.glob("*"))) if CACHE_DIR.is_dir() else 0
        print(f"Remaining files in cache: {remaining}")

        still_wrong = (
            db.query(ImageAsset)
            .filter(ImageAsset.path.like("%china_ftp_downloads%"))
            .count()
        )
        print(f"DB rows still pointing at cache: {still_wrong}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
