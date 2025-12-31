#!/usr/bin/env python3
"""
Smart Auto-Filter Implementation
Efficient filtering without reprocessing all images
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
import traceback
import hashlib

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event

# Install a safe print wrapper to avoid Unicode/OSError issues when printing
import builtins
_original_print = builtins.print
def _safe_print(*args, **kwargs):
    try:
        _original_print(*args, **kwargs)
    except (UnicodeEncodeError, OSError) as e:
        # Fallback: try to coerce arguments to safe strings and print a simplified message
        try:
            safe_parts = []
            for a in args:
                try:
                    safe_parts.append(str(a))
                except Exception:
                    try:
                        safe_parts.append(repr(a))
                    except Exception:
                        safe_parts.append('<unprintable>')
            _original_print(' '.join(safe_parts), **kwargs)
        except Exception:
            try:
                _original_print('<<print failed: unable to render args>>')
            except Exception:
                pass

# Monkey-patch builtins.print for the lifetime of this script to reduce Errno 22 on exit
builtins.print = _safe_print

# Ensure stdout/stderr can handle Unicode on Windows consoles
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # Best-effort only; fall back silently if reconfigure isn't supported
    pass

class SmartAutoFilter:
    def __init__(self):
        from py.utilities.setting import Config
        self.dropbox_path = Config.DROPBOX_CONTAINER_CATALOG_DIR
        self.catalog_dest = Config.IMAGE_DAM_CATALOG_DIR
        self.big_picture_dest = Config.IMAGE_DAM_BIG_PICTURE_DIR
        
        # Tracking files for incremental processing
        self.processed_files_log = "processed_files.log"
        self.last_scan_timestamp = self.get_last_scan_timestamp()
        
        # Database connection
        self.db_conn = None
    
    def get_db_connection(self):
        """Get or create database connection"""
        if self.db_conn is None:
            self.db_conn = get_mysql_connection()
        return self.db_conn
    
    def close_db_connection(self):
        """Close database connection"""
        if self.db_conn:
            try:
                self.db_conn.close()
            except:
                pass
            self.db_conn = None
    
    def calculate_file_hash(self, file_path):
        """Calculate SHA-256 hash of file"""
        hash_sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_sha256.update(chunk)
            return hash_sha256.hexdigest()
        except Exception as e:
            print(f"❌ Error hashing {file_path}: {e}")
            return None
    
    def get_image_info(self, file_path):
        """Extract image metadata"""
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                return {
                    'width': img.width,
                    'height': img.height,
                    'size_bytes': os.path.getsize(file_path)
                }
        except Exception as e:
            print(f"⚠️  Could not read image metadata for {file_path}: {e}")
            return None
    
    def sync_to_database(self, file_path, sku, classification, dest_path):
        """Insert/update image record in database after successful copy"""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            
            filename = Path(file_path).name
            sha256_hash = self.calculate_file_hash(str(dest_path))
            image_info = self.get_image_info(str(dest_path))
            
            if not sha256_hash or not image_info:
                print(f"⚠️  Skipping DB sync for {filename}: missing hash or metadata")
                return False
            
            # Determine version number
            version = 1
            if '_v' in filename:
                import re
                match = re.search(r'_v(\d+)', filename)
                if match:
                    version = int(match.group(1))
            
            # Determine source based on classification (original source, not dropbox)
            if classification == 'catalog':
                source = 'catalog_original'
            elif classification == 'big_picture':
                source = 'big_picture_original'
            else:
                source = 'dropbox'  # Fallback for unknown classifications
            
            # Check if file already exists by filename (regardless of source)
            cursor.execute(
                "SELECT id, sha256, source FROM image_assets WHERE filename = %s",
                (filename,)
            )
            existing = cursor.fetchone()
            
            if existing:
                existing_id, existing_hash, existing_source = existing
                
                # If hash is the same, just update source if needed and return
                if existing_hash == sha256_hash:
                    if existing_source != source:
                        # Update source to correct designation
                        cursor.execute(
                            "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                            (source, datetime.now(), existing_id)
                        )
                        conn.commit()
                        print(f"  ✏️  Updated source for {filename}: {existing_source} → {source}")
                    else:
                        print(f"  ℹ️  {filename} already in DB with correct source ({source}), skipping")
                    return True
                
                # If hash changed, update the record
                cursor.execute("""
                    UPDATE image_assets SET 
                        sku = %s, path = %s, sha256 = %s,
                        width = %s, height = %s, file_size = %s,
                        source = %s, is_active = TRUE, updated_at = %s
                    WHERE id = %s
                """, (
                    sku, str(dest_path), sha256_hash,
                    image_info['width'], image_info['height'], image_info['size_bytes'],
                    source, datetime.now(), existing_id
                ))
                conn.commit()
                print(f"  ✏️  Updated DB record for {filename} (hash changed, source: {source})")
                return True
            
            # Check if same content (by hash) already exists with the correct source
            cursor.execute(
                "SELECT id, source FROM image_assets WHERE sha256 = %s AND source = %s",
                (sha256_hash, source)
            )
            duplicate = cursor.fetchone()
            
            if duplicate:
                print(f"  ℹ️  Content already in DB with {source} source, skipping")
                return True
            
            # Insert new record
            is_canonical = (Path(filename).stem.upper() == sku.upper())
            cursor.execute("""
                INSERT INTO image_assets 
                (sku, version, filename, path, source, width, height, file_size, sha256, 
                 is_active, is_canonical, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                sku, version, filename, str(dest_path), source,
                image_info['width'], image_info['height'], image_info['size_bytes'],
                sha256_hash, True, is_canonical,
                datetime.now(), datetime.now()
            ))
            conn.commit()
            print(f"  ✅ Added to DB: {sku} - {filename}")
            return True
            
        except Exception as e:
            print(f"❌ DB sync error for {filename}: {e}")
            try:
                conn.rollback()
            except:
                pass
            return False
        
        
    def get_last_scan_timestamp(self):
        """Get timestamp of last scan to only process new/modified files"""
        try:
            if os.path.exists(self.processed_files_log):
                with open(self.processed_files_log, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    if lines:
                        return datetime.fromisoformat(lines[-1].strip().split('|')[0])
            return datetime.min
        except:
            return datetime.min
    
    def get_new_or_modified_files(self):
        """Only get files that are new or modified since last scan"""
        new_files = []
        
        if not os.path.exists(self.dropbox_path):
            print(f"⚠️  Dropbox path not accessible: {self.dropbox_path}")
            return new_files
        
        print(f"🔍 Scanning for files newer than: {self.last_scan_timestamp}")
        
        for root, dirs, files in os.walk(self.dropbox_path):
            for file in files:
                file_path = Path(root) / file
                
                # Check if it's a supported image format
                if file_path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}:
                    try:
                        # Get file modification time
                        file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                        
                        # Only process if newer than last scan
                        if file_mtime > self.last_scan_timestamp:
                            new_files.append(str(file_path))
                    except Exception as e:
                        print(f"❌ Error checking {file_path}: {e}")
        
        print(f"📊 Found {len(new_files)} new/modified files to process")
        return new_files
    
    def classify_image_fast(self, image_path):
        """
        Fast image classification without opening the image
        Uses filename patterns and folder structure
        """
        path_str = str(image_path).lower()
        filename = Path(image_path).name.lower()
        
        # Fast classification based on naming patterns
        catalog_keywords = ['catalog', 'spec', 'dimension', 'size', 'chart']
        big_picture_keywords = [
            'detail', 'color', 'variant', 'close', 'angle',
            'lifestyle', 'view', 'side', 'back', 'front'
        ]
        
        # Folder-based classification
        parent_folder = Path(image_path).parent.name.lower()
        if any(keyword in parent_folder for keyword in ['detail', 'color', 'variant']):
            return 'big_picture'
        
        # Filename-based classification
        if any(keyword in filename for keyword in catalog_keywords):
            return 'catalog'
        elif any(keyword in filename for keyword in big_picture_keywords):
            return 'big_picture'
        
        # Default classification based on naming pattern
        # Simple SKU names (T3305.jpg) are usually catalog
        # Complex names (T3305_detail_01.jpg) are usually big pictures
        stem = Path(image_path).stem
        if '_' not in stem or stem.count('_') <= 1:
            return 'catalog'
        else:
            return 'big_picture'
    
    def extract_sku_fast(self, file_path):
        """Fast SKU extraction using cached patterns"""
        import re
        
        # Check filename first (fastest)
        filename = Path(file_path).stem.upper()
        
        # Common SKU patterns (ordered by frequency for speed)
        patterns = [
            r'^([A-Z]+\d+)',      # T3305, N1151 (most common)
            r'^(\d+)',            # 52621 (numeric SKUs)
            r'([A-Z]+\d+)',       # SKU anywhere in filename
        ]
        
        for pattern in patterns:
            match = re.search(pattern, filename)
            if match:
                return match.group(1)
        
        # Fallback to folder name
        for part in reversed(Path(file_path).parts):
            for pattern in patterns:
                match = re.search(pattern, part.upper())
                if match:
                    return match.group(1)
        
        return None
    
    def check_if_already_processed(self, file_path, sku, classification):
        """Check if file is already organized to avoid duplicates"""
        filename = Path(file_path).name
        
        if classification == 'catalog':
            dest_path = Path(self.catalog_dest) / filename
            # Also check for versioned names
            sku_files = list(Path(self.catalog_dest).glob(f"{sku}*.jpg"))
            return dest_path.exists() or any(sku in f.stem for f in sku_files)
        
        elif classification == 'big_picture':
            sku_folder = Path(self.big_picture_dest) / sku
            if sku_folder.exists():
                return (sku_folder / filename).exists()
        
        return False
    
    def process_file_fast(self, file_path):
        """Process single file with all optimizations"""
        try:
            # Fast SKU extraction
            sku = self.extract_sku_fast(file_path)
            if not sku:
                print(f"⚠️  No SKU found: {Path(file_path).name}")
                return False
            
            # Fast classification
            classification = self.classify_image_fast(file_path)
            
            # Check if already processed
            if self.check_if_already_processed(file_path, sku, classification):
                print(f"⏭️  Already processed: {sku} - {Path(file_path).name}")
                return True
            
            # Process based on classification
            if classification == 'catalog':
                return self.process_catalog_fast(file_path, sku)
            else:
                return self.process_big_picture_fast(file_path, sku)
                
        except Exception as e:
            print(f"❌ Error processing {file_path}: {e}")
            return False
    
    def process_catalog_fast(self, source_path, sku):
        """Fast catalog processing with version management and database sync"""
        import shutil
        
        try:
            os.makedirs(self.catalog_dest, exist_ok=True)
            
            filename = Path(source_path).name
            main_path = Path(self.catalog_dest) / f"{sku}.jpg"
            
            # If main doesn't exist, copy as main
            if not main_path.exists():
                shutil.copy2(source_path, str(main_path))
                print(f"✅ New catalog: {sku}.jpg")
                # Sync to database
                self.sync_to_database(source_path, sku, 'catalog', main_path)
                return True
            
            # If main exists, save as version
            version_num = self.get_next_version_fast(sku)
            version_path = Path(self.catalog_dest) / f"{sku}_v{version_num}.jpg"
            shutil.copy2(source_path, str(version_path))
            print(f"✅ Catalog version: {sku}_v{version_num}.jpg")
            # Sync to database
            self.sync_to_database(source_path, sku, 'catalog', version_path)
            return True
            
        except Exception as e:
            print(f"❌ Catalog processing error: {e}")
            return False
    
    def process_big_picture_fast(self, source_path, sku):
        """Fast big picture processing with database sync"""
        import shutil
        
        try:
            sku_folder = Path(self.big_picture_dest) / sku
            os.makedirs(sku_folder, exist_ok=True)
            
            filename = Path(source_path).name
            dest_path = sku_folder / filename
            
            # Handle naming conflicts
            counter = 1
            original_dest = dest_path
            while dest_path.exists():
                stem = original_dest.stem
                suffix = original_dest.suffix
                dest_path = sku_folder / f"{stem}_{counter:02d}{suffix}"
                counter += 1
            
            shutil.copy2(source_path, str(dest_path))
            print(f"✅ Big picture: {sku}/{dest_path.name}")
            # Sync to database
            self.sync_to_database(source_path, sku, 'big_picture', dest_path)
            return True
            
        except Exception as e:
            print(f"❌ Big picture processing error: {e}")
            return False
    
    def get_next_version_fast(self, sku):
        """Fast version number calculation"""
        existing_files = list(Path(self.catalog_dest).glob(f"{sku}_v*.jpg"))
        if not existing_files:
            return 2  # Start with v2 since main is v1
        
        # Extract version numbers
        versions = []
        for file_path in existing_files:
            try:
                version_part = file_path.stem.split('_v')[1]
                versions.append(int(version_part))
            except:
                continue
        
        return max(versions) + 1 if versions else 2
    
    def log_processed_file(self, file_path, sku, classification):
        """Log processed file for tracking"""
        timestamp = datetime.now().isoformat()
        log_entry = f"{timestamp}|{file_path}|{sku}|{classification}\n"
        try:
            with open(self.processed_files_log, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except Exception as e:
            # Best-effort: write traceback to stderr and a fallback error file
            try:
                print(f"❌ Failed to write processed file log: {e}", file=sys.stderr)
                traceback.print_exc()
                with open('smart_auto_filter_error.log', 'a', encoding='utf-8') as ef:
                    ef.write(f"{datetime.now().isoformat()} - Failed to write processed file log: {e}\n")
                    ef.write(''.join(traceback.format_exc()))
            except Exception:
                pass
    
    def run_incremental_filter(self):
        """Run smart incremental filtering"""
        print("🚀 Starting Smart Auto-Filter (Incremental Mode)")
        print("=" * 50)
        log_event('info', 'Smart Auto-Filter started (Incremental Mode)')
        
        # Get only new/modified files
        new_files = self.get_new_or_modified_files()
        
        if not new_files:
            print("✅ No new files to process")
            log_event('info', 'Smart Auto-Filter completed: No new files to process')
            return
        
        log_event('info', f'Smart Auto-Filter: Found {len(new_files)} new/modified files to process')
        
        # Process only new files
        processed_count = 0
        catalog_count = 0
        big_picture_count = 0
        
        for file_path in new_files:
            sku = self.extract_sku_fast(file_path)
            classification = self.classify_image_fast(file_path)
            
            if self.process_file_fast(file_path):
                processed_count += 1
                if classification == 'catalog':
                    catalog_count += 1
                else:
                    big_picture_count += 1
                
                # Log processed file
                self.log_processed_file(file_path, sku, classification)
        
        # Update scan timestamp
        try:
            with open(self.processed_files_log, 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().isoformat()}|SCAN_COMPLETE|\n")
        except Exception as e:
            print(f"❌ Failed to write scan complete marker: {e}", file=sys.stderr)
            log_event('error', f'Smart Auto-Filter: Failed to write scan complete marker: {e}')
            try:
                with open('smart_auto_filter_error.log', 'a', encoding='utf-8') as ef:
                    ef.write(f"{datetime.now().isoformat()} - Failed to write scan complete marker: {e}\n")
                    ef.write(''.join(traceback.format_exc()))
            except Exception:
                pass
        
        print("\n" + "=" * 50)
        print(f"✅ Processing Complete!")
        print(f"📊 Total processed: {processed_count}")
        print(f"📖 Catalog images: {catalog_count}")
        print(f"🖼️  Big pictures: {big_picture_count}")
        print(f"⚡ Processing time: Minimal (incremental)")
        
        log_event('info', f'Smart Auto-Filter completed: Processed={processed_count}, Catalog={catalog_count}, BigPicture={big_picture_count}')
        
        # Flush stdout/stderr before exit to prevent errors
        sys.stdout.flush()
        sys.stderr.flush()

    def run_full_scan(self):
        """Walk the entire Dropbox folder and attempt to process every image file.
        This is intended for ad-hoc full imports when incremental mode may have skipped files.
        """
        print("🚀 Starting Smart Auto-Filter (Full Scan Mode)")
        print("=" * 50)
        log_event('info', 'Smart Auto-Filter started (Full Scan Mode)')

        if not os.path.exists(self.dropbox_path):
            print(f"⚠️  Dropbox path not accessible: {self.dropbox_path}")
            log_event('error', f'Smart Auto-Filter: Dropbox path not accessible: {self.dropbox_path}')
            return

        processed_count = 0
        catalog_count = 0
        big_picture_count = 0

        for root, dirs, files in os.walk(self.dropbox_path):
            for file in files:
                file_path = str(Path(root) / file)
                if Path(file_path).suffix.lower() not in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}:
                    continue

                sku = self.extract_sku_fast(file_path)
                classification = self.classify_image_fast(file_path)

                if self.process_file_fast(file_path):
                    processed_count += 1
                    if classification == 'catalog':
                        catalog_count += 1
                    else:
                        big_picture_count += 1

                    # Log processed file
                    self.log_processed_file(file_path, sku, classification)

        # Finalize
        try:
            with open(self.processed_files_log, 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().isoformat()}|SCAN_COMPLETE|FULL\n")
        except Exception as e:
            print(f"❌ Failed to write full scan complete marker: {e}", file=sys.stderr)
            log_event('error', f'Smart Auto-Filter: Failed to write full scan complete marker: {e}')
            try:
                with open('smart_auto_filter_error.log', 'a', encoding='utf-8') as ef:
                    ef.write(f"{datetime.now().isoformat()} - Failed to write full scan complete marker: {e}\n")
                    ef.write(''.join(traceback.format_exc()))
            except Exception:
                pass

        print("\n" + "=" * 50)
        print(f"✅ Full scan complete")
        print(f"📊 Total processed: {processed_count}")
        print(f"📖 Catalog images: {catalog_count}")
        print(f"🖼️  Big pictures: {big_picture_count}")
        log_event('info', f'Smart Auto-Filter Full Scan completed: Processed={processed_count}, Catalog={catalog_count}, BigPicture={big_picture_count}')
        sys.stdout.flush()
        sys.stderr.flush()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Smart Auto-Filter')
    parser.add_argument('--full', action='store_true', help='Run full scan of Dropbox folder (process all files)')
    parser.add_argument('--force', action='store_true', help='Force processing even if already present (not implemented)')
    parser.add_argument('--debug', action='store_true', help='Enable debug output and full tracebacks to logs')
    args = parser.parse_args()

    try:
        filter_service = SmartAutoFilter()
        if args.full:
            print('🔁 Running full Dropbox scan (this may take a while)')
            log_event('info', 'Smart Auto-Filter: Starting full scan mode via command line')
            # Walk all files and process each one (does internal skip if already processed)
            filter_service.run_full_scan()
        else:
            log_event('info', 'Smart Auto-Filter: Starting incremental mode via command line')
            filter_service.run_incremental_filter()

        # Ensure clean exit
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as e:
        print(f"Error in Smart Auto-Filter: {e}", file=sys.stderr)
        log_event('error', f'Smart Auto-Filter failed with error: {e}')
        sys.stderr.flush()
        sys.exit(1)
    finally:
        # Close database connection
        try:
            filter_service.close_db_connection()
        except:
            pass
