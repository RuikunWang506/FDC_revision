#!/usr/bin/env python3
"""
Enhanced DAM Integration and Sync System v2
- Batch processing with commits every 100 records
- Progress reporting every 100 inputs
- Index file generation and usage for faster processing
- Automated scheduling support (every 3 days)
"""

import os
import sys
import json
from pathlib import Path
from PIL import Image
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import time

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event

# Ensure stdout/stderr can handle Unicode on Windows consoles
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

class DAMSyncServiceV2:
    def __init__(self):
        self.catalog_dir = r"D:\Catalog_pictures\catalog_original"
        self.big_picture_dir = r"D:\Individual_big_picture"
        self.dropbox_dir = r"Dropbox\Pictures-China (1)\container catalog"
        
        # Index file paths
        self.big_picture_index = Path(self.big_picture_dir) / "index.json"
        self.catalog_index = Path(self.catalog_dir) / "index.json"
        
        # Batch processing settings
        self.batch_size = 100
        self.progress_interval = 100
        
        # Statistics
        self.stats = {
            'total_processed': 0,
            'total_added': 0,
            'total_updated': 0,
            'total_skipped': 0,
            'total_errors': 0,
            'start_time': None,
            'end_time': None
        }
        
    def get_db_connection(self):
        """Get database connection"""
        return get_mysql_connection()
    
    def calculate_file_hash(self, file_path):
        """Calculate SHA-256 hash of file for duplicate detection"""
        hash_sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()
    
    def get_image_info(self, file_path):
        """Extract image metadata"""
        try:
            with Image.open(file_path) as img:
                return {
                    'width': img.width,
                    'height': img.height,
                    'format': img.format,
                    'mode': img.mode,
                    'size_bytes': os.path.getsize(file_path)
                }
        except Exception as e:
            print(f"Error reading image {file_path}: {e}", file=sys.stderr)
            print(f"Removing corrupted image: {file_path}")
            try:
                os.remove(file_path)
                print(f"Successfully removed corrupted image: {file_path}")
            except Exception as remove_error:
                print(f"Error removing corrupted image {file_path}: {remove_error}", file=sys.stderr)
            return None
    
    def extract_sku_from_filename(self, filename):
        """Extract SKU from filename"""
        import re
        
        base_name = Path(filename).stem
        
        # Remove version suffixes like _v2, _v3
        base_name = re.sub(r'_v\d+$', '', base_name)
        
        # Look for SKU patterns
        patterns = [
            r'^([A-Z]+\d+)',  # T3305, N1151
            r'^(\d+[A-Z]*)',  # 52621
        ]
        
        for pattern in patterns:
            match = re.match(pattern, base_name.upper())
            if match:
                return match.group(1)
        
        return base_name.upper()  # Fallback to the base name
    
    def generate_index(self, root_dir: str, output_path: str, pattern: str = "*.jpg") -> List[str]:
        """
        Generate index file listing all matching files relative to root_dir
        Returns list of relative paths
        """
        print(f"Generating index for {root_dir}...")
        root_path = Path(root_dir)
        
        if not root_path.exists():
            print(f"Directory not found: {root_dir}")
            return []
        
        file_list = []
        for file_path in root_path.rglob(pattern):
            if file_path.is_file():
                try:
                    relative_path = file_path.relative_to(root_path)
                    file_list.append(str(relative_path).replace('\\', '/'))
                except Exception as e:
                    print(f"Error processing {file_path}: {e}")
        
        # Sort for consistency
        file_list.sort()
        
        # Save to JSON
        try:
            output_file = Path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(file_list, f, indent=2, ensure_ascii=False)
            
            print(f"Index saved: {output_path} ({len(file_list)} files)")
        except Exception as e:
            print(f"Error saving index: {e}")
        
        return file_list
    
    def load_index(self, index_path: str) -> List[str]:
        """Load index file if it exists"""
        try:
            if Path(index_path).exists():
                with open(index_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading index {index_path}: {e}")
        
        return []
    
    def print_progress(self, current: int, total: int, prefix: str = "Progress"):
        """Print progress update"""
        percent = (current / total * 100) if total > 0 else 0
        
        # Initialize start_time if not set
        if self.stats['start_time'] is None:
            self.stats['start_time'] = time.time()
        
        elapsed = time.time() - self.stats['start_time']
        rate = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / rate if rate > 0 else 0
        
        print(f"{prefix}: {current}/{total} ({percent:.1f}%) | "
              f"Added: {self.stats['total_added']} | "
              f"Updated: {self.stats['total_updated']} | "
              f"Skipped: {self.stats['total_skipped']} | "
              f"Errors: {self.stats['total_errors']} | "
              f"Rate: {rate:.1f} files/sec | "
              f"ETA: {eta/60:.1f} min")
    
    def sync_catalog_images_batch(self, force_rebuild_index: bool = False):
        """Sync catalog_original folder with DAM database using batch processing"""
        print("\n=== Syncing Catalog Images (Batch Mode) ===")
        
        if not os.path.exists(self.catalog_dir):
            print(f"Catalog directory not found: {self.catalog_dir}")
            return
        
        # Generate or load index
        if force_rebuild_index or not self.catalog_index.exists():
            file_list = self.generate_index(self.catalog_dir, str(self.catalog_index), "*.jpg")
        else:
            print(f"Loading existing index: {self.catalog_index}")
            file_list = self.load_index(str(self.catalog_index))
        
        if not file_list:
            print("No files to process")
            return
        
        total_files = len(file_list)
        print(f"Processing {total_files} catalog images...")
        
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        batch_count = 0
        
        for idx, relative_path in enumerate(file_list, 1):
            try:
                file_path = Path(self.catalog_dir) / relative_path
                
                if not file_path.exists():
                    self.stats['total_errors'] += 1
                    continue
                
                filename = file_path.name
                sku = self.extract_sku_from_filename(filename)
                
                if not sku:
                    self.stats['total_skipped'] += 1
                    continue
                
                # Get image info and hash first
                image_info = self.get_image_info(str(file_path))
                if not image_info:
                    self.stats['total_errors'] += 1
                    continue
                
                sha256_hash = self.calculate_file_hash(str(file_path))
                
                # PRIORITY 1: Check if this SHA256 already exists (regardless of source)
                # Catalog images take priority - update existing record to use catalog path
                cursor.execute(
                    "SELECT id, source, path FROM image_assets WHERE sha256 = %s",
                    (sha256_hash,)
                )
                existing_by_hash = cursor.fetchone()
                
                if existing_by_hash:
                    img_id, existing_source, existing_path = existing_by_hash
                    
                    # If existing source is dropbox or lower priority, update to catalog
                    if existing_source in ['dropbox', 'big_picture']:
                        print(f"  → Updating {filename}: {existing_source} → catalog_original")
                        
                        # Determine version
                        import re
                        version_match = re.search(r'_v(\d+)', filename)
                        version = int(version_match.group(1)) if version_match else 1
                        
                        # Determine if canonical
                        is_canonical = Path(filename).stem == sku
                        
                        cursor.execute("""
                            UPDATE image_assets SET 
                                sku = %s, version = %s, filename = %s, path = %s, source = %s,
                                width = %s, height = %s, file_size = %s,
                                is_active = TRUE, is_canonical = %s, updated_at = %s
                            WHERE id = %s
                        """, (
                            sku, version, filename, str(file_path), 'catalog_original',
                            image_info['width'], image_info['height'], image_info['size_bytes'],
                            is_canonical, datetime.now(), img_id
                        ))
                        self.stats['total_updated'] += 1
                    else:
                        # Already catalog or higher priority - skip
                        self.stats['total_skipped'] += 1
                    
                    self.stats['total_processed'] += 1
                    batch_count += 1  # Count updates for batch commits
                    
                    # Commit batch if needed
                    if batch_count >= self.batch_size:
                        connection.commit()
                        batch_count = 0
                    
                    continue
                
                # PRIORITY 2: Check if already exists by filename and source
                cursor.execute(
                    "SELECT id, is_active, path FROM image_assets WHERE filename = %s AND source = %s",
                    (filename, 'catalog_original')
                )
                existing = cursor.fetchone()
                
                if existing:
                    img_id, is_active, existing_path = existing
                    
                    # Only update if inactive or path changed
                    if not is_active or existing_path != str(file_path):
                        cursor.execute("""
                            UPDATE image_assets SET 
                                path = %s, is_active = TRUE, 
                                width = %s, height = %s, file_size = %s, sha256 = %s,
                                updated_at = %s
                            WHERE id = %s
                        """, (
                            str(file_path), image_info['width'], image_info['height'],
                            image_info['size_bytes'], sha256_hash, datetime.now(), img_id
                        ))
                        self.stats['total_updated'] += 1
                    else:
                        self.stats['total_skipped'] += 1
                    
                    self.stats['total_processed'] += 1
                    batch_count += 1  # Count filename updates for batch commits
                    
                    # Commit batch if needed
                    if batch_count >= self.batch_size:
                        connection.commit()
                        batch_count = 0
                    
                    continue
                
                # NEW IMAGE - Insert new record
                
                # NEW IMAGE - Insert new record
                
                # Determine version
                import re
                version_match = re.search(r'_v(\d+)', filename)
                version = int(version_match.group(1)) if version_match else 1
                
                # Determine if canonical
                is_canonical = Path(filename).stem == sku
                
                cursor.execute("""
                    INSERT INTO image_assets 
                    (sku, version, filename, path, source, width, height, file_size, sha256, 
                     is_active, is_canonical, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    sku, version, filename, str(file_path), 'catalog_original',
                    image_info['width'], image_info['height'], image_info['size_bytes'],
                    sha256_hash, True, is_canonical,
                    datetime.now(), datetime.now()
                ))
                
                self.stats['total_added'] += 1
                self.stats['total_processed'] += 1
                batch_count += 1
                
                # Commit every batch_size records
                if batch_count >= self.batch_size:
                    connection.commit()
                    batch_count = 0
                
                # Print progress every progress_interval
                if idx % self.progress_interval == 0:
                    self.print_progress(idx, total_files, "Catalog Progress")
                
            except Exception as e:
                print(f"Error processing {relative_path}: {e}")
                self.stats['total_errors'] += 1
        
        # Final commit for remaining records
        if batch_count > 0:
            connection.commit()
        
        cursor.close()
        connection.close()
        
        self.print_progress(total_files, total_files, "Catalog Complete")
    
    def sync_big_picture_images_batch(self, force_rebuild_index: bool = False):
        """Sync Individual_big_picture folder with DAM database using batch processing"""
        print("\n=== Syncing Big Picture Images (Batch Mode) ===")
        
        if not os.path.exists(self.big_picture_dir):
            print(f"Big picture directory not found: {self.big_picture_dir}")
            return
        
        # Generate or load index
        if force_rebuild_index or not self.big_picture_index.exists():
            file_list = self.generate_index(self.big_picture_dir, str(self.big_picture_index), "*.jpg")
        else:
            print(f"Loading existing index: {self.big_picture_index}")
            file_list = self.load_index(str(self.big_picture_index))
        
        if not file_list:
            print("No files to process")
            return
        
        total_files = len(file_list)
        print(f"Processing {total_files} big picture images...")
        
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        batch_count = 0
        
        for idx, relative_path in enumerate(file_list, 1):
            try:
                file_path = Path(self.big_picture_dir) / relative_path
                
                if not file_path.exists():
                    self.stats['total_errors'] += 1
                    continue
                
                filename = file_path.name
                sku = self.extract_sku_from_filename(filename)
                
                if not sku:
                    self.stats['total_skipped'] += 1
                    continue
                
                # Get image info and hash first
                image_info = self.get_image_info(str(file_path))
                if not image_info:
                    self.stats['total_errors'] += 1
                    continue
                
                sha256_hash = self.calculate_file_hash(str(file_path))
                
                # Check if this SHA256 already exists from higher priority source
                cursor.execute(
                    "SELECT id, source FROM image_assets WHERE sha256 = %s",
                    (sha256_hash,)
                )
                existing_by_hash = cursor.fetchone()
                
                if existing_by_hash:
                    # Any existing record with same content hash should be skipped to avoid UNIQUE constraint violations
                    img_id, existing_source = existing_by_hash
                    self.stats['total_skipped'] += 1
                    self.stats['total_processed'] += 1
                    # Optionally log why
                    # print(f"  → Skipping {filename}: duplicate content already stored as {existing_source}")
                    continue
                
                # Check if already exists by filename/source/sku
                cursor.execute(
                    "SELECT id FROM image_assets WHERE filename = %s AND source = %s AND sku = %s",
                    (filename, 'big_picture', sku)
                )
                
                if cursor.fetchone():
                    self.stats['total_skipped'] += 1
                    self.stats['total_processed'] += 1
                    continue
                version = 1
                
                cursor.execute("""
                    INSERT INTO image_assets 
                    (sku, version, filename, path, source, width, height, file_size, sha256, 
                     is_active, is_canonical, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    sku, version, filename, str(file_path), 'big_picture',
                    image_info['width'], image_info['height'], image_info['size_bytes'],
                    sha256_hash, True, False,
                    datetime.now(), datetime.now()
                ))
                
                self.stats['total_added'] += 1
                self.stats['total_processed'] += 1
                batch_count += 1
                
                # Commit every batch_size records
                if batch_count >= self.batch_size:
                    connection.commit()
                    batch_count = 0
                
                # Print progress every progress_interval
                if idx % self.progress_interval == 0:
                    self.print_progress(idx, total_files, "Big Picture Progress")
                
            except Exception as e:
                print(f"Error processing {relative_path}: {e}")
                self.stats['total_errors'] += 1
        
        # Final commit
        if batch_count > 0:
            connection.commit()
        
        cursor.close()
        connection.close()
        
        self.print_progress(total_files, total_files, "Big Picture Complete")
    
    def update_canonical_flags(self):
        """Update canonical image flags for each SKU"""
        print("\n=== Updating Canonical Flags ===")
        
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        # Get all unique SKUs
        cursor.execute("SELECT DISTINCT sku FROM image_assets WHERE is_active = TRUE")
        skus = [row[0] for row in cursor.fetchall()]
        
        print(f"Updating canonical flags for {len(skus)} SKUs...")
        
        for idx, sku in enumerate(skus, 1):
            # Find best catalog image (highest resolution)
            cursor.execute("""
                SELECT id, filename
                FROM image_assets
                WHERE sku = %s AND source = 'catalog_original' AND is_active = TRUE
                ORDER BY width * height DESC, filename ASC
            """, (sku,))
            
            catalog_images = cursor.fetchall()
            
            if catalog_images:
                # Clear all canonical flags for this SKU
                cursor.execute(
                    "UPDATE image_assets SET is_canonical = FALSE WHERE sku = %s",
                    (sku,)
                )
                
                # Set best image as canonical
                best_image_id = catalog_images[0][0]
                cursor.execute(
                    "UPDATE image_assets SET is_canonical = TRUE WHERE id = %s",
                    (best_image_id,)
                )
            
            if idx % 100 == 0:
                connection.commit()
                print(f"Canonical flags updated: {idx}/{len(skus)}")
        
        connection.commit()
        cursor.close()
        connection.close()
        
        print(f"Canonical flags updated for {len(skus)} SKUs")
    
    def print_final_stats(self):
        """Print final synchronization statistics"""
        elapsed = self.stats['end_time'] - self.stats['start_time']
        
        print("\n" + "="*60)
        print("SYNC SUMMARY")
        print("="*60)
        # Persistent log of final stats
        try:
            log_event('info', f"DAM SYNC SUMMARY - Processed: {self.stats['total_processed']}, Added: {self.stats['total_added']}, Updated: {self.stats['total_updated']}, Skipped: {self.stats['total_skipped']}, Errors: {self.stats['total_errors']}, Elapsed(min): {elapsed/60:.2f}")
        except Exception:
            pass
        print(f"Total Processed: {self.stats['total_processed']}")
        print(f"  - Added:       {self.stats['total_added']}")
        print(f"  - Updated:     {self.stats['total_updated']}")
        print(f"  - Skipped:     {self.stats['total_skipped']}")
        print(f"  - Errors:      {self.stats['total_errors']}")
        print(f"Time Elapsed:    {elapsed/60:.2f} minutes")
        print(f"Processing Rate: {self.stats['total_processed']/elapsed:.2f} files/sec")
        print("="*60)
    
    def run_full_sync(self, force_rebuild_index: bool = False):
        """Run complete synchronization"""
        print("\n" + "="*60)
        print("STARTING FULL DAM SYNCHRONIZATION (V2)")
        print("="*60)
        try:
            log_event('info', f"Starting full DAM sync (force_rebuild_index={force_rebuild_index})")
        except Exception:
            pass
        print(f"Batch Size: {self.batch_size}")
        print(f"Progress Interval: {self.progress_interval}")
        print(f"Force Rebuild Index: {force_rebuild_index}")
        print(f"Start Time: {datetime.now()}")
        print("="*60)
        
        self.stats['start_time'] = time.time()
        
        # Sync catalog images
        self.sync_catalog_images_batch(force_rebuild_index)
        
        # Sync big picture images
        self.sync_big_picture_images_batch(force_rebuild_index)
        
        # Update canonical flags
        self.update_canonical_flags()
        
        self.stats['end_time'] = time.time()
        
        # Print final statistics
        self.print_final_stats()
        
        print("\nDAM synchronization completed!")
    
    def should_run_scheduled_sync(self) -> bool:
        """Check if scheduled sync should run (every 3 days)"""
        sync_marker_file = Path(self.big_picture_dir) / ".last_sync"
        
        if not sync_marker_file.exists():
            return True
        
        try:
            with open(sync_marker_file, 'r') as f:
                last_sync_str = f.read().strip()
                last_sync = datetime.fromisoformat(last_sync_str)
                
            days_since_sync = (datetime.now() - last_sync).days
            return days_since_sync >= 3
        except Exception as e:
            print(f"Error reading sync marker: {e}")
            return True
    
    def update_sync_marker(self):
        """Update the last sync timestamp"""
        sync_marker_file = Path(self.big_picture_dir) / ".last_sync"
        
        try:
            with open(sync_marker_file, 'w') as f:
                f.write(datetime.now().isoformat())
        except Exception as e:
            print(f"Error updating sync marker: {e}")
    
    def run_scheduled_sync(self):
        """Run sync if scheduled (every 3 days)"""
        try:
            if self.should_run_scheduled_sync():
                print("Running scheduled sync (3-day interval)...")
                try:
                    log_event('info', 'Scheduled DAM sync starting (3-day interval)')
                except Exception:
                    pass

                self.run_full_sync(force_rebuild_index=True)
                self.update_sync_marker()

                try:
                    log_event('info', 'Scheduled DAM sync completed successfully')
                except Exception:
                    pass
            else:
                print("Sync not needed yet (last sync was less than 3 days ago)")
                try:
                    log_event('info', 'Scheduled DAM sync skipped (less than 3 days since last)')
                except Exception:
                    pass
        except Exception as e:
            try:
                log_event('error', f'Scheduled DAM sync failed: {e}')
            except Exception:
                pass
            raise

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='DAM Sync Service V2')
    parser.add_argument('--force-index', action='store_true', 
                       help='Force rebuild of index files')
    parser.add_argument('--scheduled', action='store_true',
                       help='Run only if scheduled (every 3 days)')
    
    args = parser.parse_args()
    
    sync_service = DAMSyncServiceV2()
    
    if args.scheduled:
        sync_service.run_scheduled_sync()
    else:
        sync_service.run_full_sync(force_rebuild_index=args.force_index)
