#!/usr/bin/env python3
"""Parallel fetch China FTP images for one SKU into big_picture + DB."""
import hashlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import paramiko
from sqlalchemy.exc import IntegrityError

from py.utilities.models import ImageAsset, SessionLocal
from py.utilities.setting import Config

SSH_HOST = "219.135.198.130"
SSH_USER = "fdc"
SSH_PASSWORD = "Bst@93109310"
REMOTE_BASE = r"D:\BST\BST_PIC\BSTpic2\梁佳怡"
DEFAULT_WORKERS = 4
MAX_RETRIES = 3


def _download_with_retries(remote_path: str, local_path: str) -> None:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            transport = paramiko.Transport((SSH_HOST, 22))
            transport.connect(username=SSH_USER, password=SSH_PASSWORD)
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.get(remote_path, local_path)
            sftp.close()
            transport.close()
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                return
            if os.path.exists(local_path):
                os.remove(local_path)
            last_exc = RuntimeError("empty download")
        except Exception as exc:
            last_exc = exc
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
    raise last_exc or RuntimeError("download failed")


def list_remote_files(sku: str) -> list[str]:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASSWORD)
    command = (
        f"powershell -command \"Get-ChildItem -Path '{REMOTE_BASE}' "
        f"-Recurse -File -Filter '{sku}*' | Select-Object FullName\""
    )
    stdin, stdout, stderr = ssh.exec_command(command)
    output = stdout.read().decode("gbk", errors="ignore")
    ssh.close()
    return [line.strip() for line in output.strip().split("\n")[2:] if line.strip()]


def existing_filenames(sku: str) -> set[str]:
    db = SessionLocal()
    try:
        rows = (
            db.query(ImageAsset.filename)
            .filter(
                ImageAsset.sku == sku,
                ImageAsset.source == "big_picture",
                ImageAsset.is_active == True,
            )
            .all()
        )
        return {r[0] for r in rows}
    finally:
        db.close()


def download_and_sync(remote_path: str, sku_folder: str, sku: str, lock: threading.Lock) -> tuple[str, str]:
    filename = os.path.basename(remote_path)
    local_path = os.path.join(sku_folder, filename)
    try:
        _download_with_retries(remote_path, local_path)

        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            if os.path.exists(local_path):
                os.remove(local_path)
            return filename, "failed: empty download"

        with open(local_path, "rb") as f:
            sha256_hash = hashlib.sha256(f.read()).hexdigest()

        with lock:
            db = SessionLocal()
            try:
                existing = (
                    db.query(ImageAsset)
                    .filter(
                        ImageAsset.sku == sku,
                        ImageAsset.filename == filename,
                        ImageAsset.source == "big_picture",
                    )
                    .first()
                )
                if not existing:
                    db.add(
                        ImageAsset(
                            sku=sku,
                            source="big_picture",
                            filename=filename,
                            path=local_path,
                            sha256=sha256_hash,
                            version=1,
                            is_canonical=False,
                            is_active=True,
                        )
                    )
                    db.commit()
                elif "china_ftp_downloads" in (existing.path or "").replace("/", "\\"):
                    existing.path = local_path
                    db.commit()
            except IntegrityError:
                db.rollback()
            finally:
                db.close()
        return filename, "synced"
    except Exception as exc:
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass
        return filename, f"failed: {exc}"


def main():
    sku = (sys.argv[1] if len(sys.argv) > 1 else "N2640").upper()
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_WORKERS

    print(f"Listing remote files for {sku}...")
    remote_paths = list_remote_files(sku)
    print(f"Found {len(remote_paths)} remote files")

    existing = existing_filenames(sku)
    sku_folder = os.path.join(Config.IMAGE_DAM_BIG_PICTURE_DIR, sku)
    os.makedirs(sku_folder, exist_ok=True)

    to_fetch = []
    already = 0
    for remote_path in remote_paths:
        filename = os.path.basename(remote_path)
        local_path = os.path.join(sku_folder, filename)
        if filename in existing and os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
            already += 1
            continue
        to_fetch.append(remote_path)

    print(f"Already synced: {already}, to fetch: {len(to_fetch)}, workers: {workers}")
    if not to_fetch:
        print("Nothing to do.")
        return

    lock = threading.Lock()
    synced = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(download_and_sync, rp, sku_folder, sku, lock): rp for rp in to_fetch
        }
        for i, future in enumerate(as_completed(futures), start=1):
            filename, status = future.result()
            if status == "synced":
                synced += 1
            else:
                failed += 1
            print(f"[{i}/{len(to_fetch)}] {filename}: {status}")

    print(f"Done — synced: {synced}, failed: {failed}, already: {already}, total remote: {len(remote_paths)}")


if __name__ == "__main__":
    main()
