#!/usr/bin/env python3
"""
DAM Source Reconciliation - One-Time Process
Compares existing files in DAM directories with database records
and updates source designation for already-imported images.

This script will:
1. Scan catalog and big_picture directories for existing files
2. Look up each file in the database
3. Determine the correct source (catalog_original or big_picture_original)
4. Update database records for files that are already imported
"""

import os
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config
from py.utilities.logging_setup import log_event

# Safe print wrapper
import builtins
_original_print = builtins.print
def _safe_print(*args, **kwargs):
    try:
        _original_print(*args, **kwargs)
    except (UnicodeEncodeError, OSError) as e:
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

builtins.print = _safe_print

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class DAMSourceReconciler:
    def __init__(self):
        self.catalog_dir = Config.IMAGE_DAM_CATALOG_DIR
        self.big_picture_dir = Config.IMAGE_DAM_BIG_PICTURE_DIR
        self.db_conn = None
    
    def get_fresh_db_connection(self):
        """Get a fresh database connection"""
        return get_mysql_connection()
    
    def close_db_connection(self):
        """Close database connection"""
        if self.db_conn:
            try:
                self.db_conn.close()
            except:
                pass
            self.db_conn = None
    
    def reconcile_catalog_images(self):
        """Process all images in catalog directory and update source"""
        print("\n" + "=" * 60)
        print("📖 RECONCILING CATALOG IMAGES")
        print("=" * 60)
        
        if not os.path.exists(self.catalog_dir):
            print(f"⚠️  Catalog directory not found: {self.catalog_dir}")
            return 0
        
        updated_count = 0
        skipped_count = 0
        error_count = 0
        
        # Scan all JPG files in catalog directory
        catalog_path = Path(self.catalog_dir)
        jpg_files = list(catalog_path.glob('*.jpg')) + list(catalog_path.glob('*.JPG'))
        
        print(f"📊 Found {len(jpg_files)} image files in catalog directory")
        
        for idx, file_path in enumerate(jpg_files):
            if idx % 100 == 0 and idx > 0:
                print(f"  Processing: {idx}/{len(jpg_files)}...")
            
            filename = file_path.name
            
            try:
                conn = self.get_fresh_db_connection()
                cursor = conn.cursor()
                
                # Check if file exists in database
                cursor.execute(
                    "SELECT id, source FROM image_assets WHERE filename = %s",
                    (filename,)
                )
                result = cursor.fetchone()
                
                if result:
                    record_id, current_source = result
                    
                    # If it's already marked as catalog_original, skip
                    if current_source == 'catalog_original':
                        skipped_count += 1
                    else:
                        # Update source to catalog_original
                        cursor.execute(
                            "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                            ('catalog_original', datetime.now(), record_id)
                        )
                        conn.commit()
                        updated_count += 1
                        if updated_count <= 10 or updated_count % 100 == 0:
                            print(f"  ✅ Updated {filename}: {current_source} → catalog_original")
                
                cursor.close()
                conn.close()
                    
            except Exception as e:
                error_count += 1
                if error_count <= 5:
                    print(f"  ❌ Error processing {filename}: {e}")
        
        print(f"\n📈 Catalog reconciliation results:")
        print(f"   ✅ Updated: {updated_count}")
        print(f"   ⏭️  Skipped (already correct): {skipped_count}")
        print(f"   ❌ Errors: {error_count}")
        
        return updated_count
    
    def reconcile_big_picture_images(self):
        """Process all images in big_picture directory and update source"""
        print("\n" + "=" * 60)
        print("🖼️  RECONCILING BIG PICTURE IMAGES")
        print("=" * 60)
        
        if not os.path.exists(self.big_picture_dir):
            print(f"⚠️  Big picture directory not found: {self.big_picture_dir}")
            return 0
        
        updated_count = 0
        skipped_count = 0
        error_count = 0
        
        # Scan all JPG files in big_picture directory
        big_picture_path = Path(self.big_picture_dir)
        jpg_files = list(big_picture_path.glob('*.jpg')) + list(big_picture_path.glob('*.JPG'))
        
        print(f"📊 Found {len(jpg_files)} image files in big picture directory")
        
        for idx, file_path in enumerate(jpg_files):
            if idx % 100 == 0 and idx > 0:
                print(f"  Processing: {idx}/{len(jpg_files)}...")
            
            filename = file_path.name
            
            try:
                conn = self.get_fresh_db_connection()
                cursor = conn.cursor()
                
                # Check if file exists in database
                cursor.execute(
                    "SELECT id, source FROM image_assets WHERE filename = %s",
                    (filename,)
                )
                result = cursor.fetchone()
                
                if result:
                    record_id, current_source = result
                    
                    # If it's already marked as big_picture_original, skip
                    if current_source == 'big_picture_original':
                        skipped_count += 1
                    else:
                        # Update source to big_picture_original
                        cursor.execute(
                            "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                            ('big_picture_original', datetime.now(), record_id)
                        )
                        conn.commit()
                        updated_count += 1
                        if updated_count <= 10 or updated_count % 100 == 0:
                            print(f"  ✅ Updated {filename}: {current_source} → big_picture_original")
                
                cursor.close()
                conn.close()
                    
            except Exception as e:
                error_count += 1
                if error_count <= 5:
                    print(f"  ❌ Error processing {filename}: {e}")
        
        print(f"\n📈 Big picture reconciliation results:")
        print(f"   ✅ Updated: {updated_count}")
        print(f"   ⏭️  Skipped (already correct): {skipped_count}")
        print(f"   ❌ Errors: {error_count}")
        
        return updated_count
    
    def reconcile_dropbox_sourced(self):
        """Check for any images with 'dropbox' source that exist in DAM directories"""
        print("\n" + "=" * 60)
        print("📦 CHECKING FOR MISCLASSIFIED 'DROPBOX' SOURCED IMAGES")
        print("=" * 60)
        
        conn = self.get_fresh_db_connection()
        cursor = conn.cursor()
        
        # Get all images with 'dropbox' source
        cursor.execute(
            "SELECT id, filename, path FROM image_assets WHERE source = 'dropbox'"
        )
        dropbox_images = cursor.fetchall()
        cursor.close()
        conn.close()
        
        print(f"📊 Found {len(dropbox_images)} images marked with 'dropbox' source")
        
        updated_count = 0
        
        for idx, (image_id, filename, path) in enumerate(dropbox_images):
            if idx % 100 == 0 and idx > 0:
                print(f"  Processing: {idx}/{len(dropbox_images)}...")
            
            try:
                # Check if file exists in catalog directory
                catalog_file = Path(self.catalog_dir) / filename
                if catalog_file.exists():
                    conn = self.get_fresh_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                        ('catalog_original', datetime.now(), image_id)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                    updated_count += 1
                    if updated_count <= 10:
                        print(f"  ✅ Updated {filename}: dropbox → catalog_original")
                    continue
                
                # Check if file exists in big_picture directory
                bp_file = Path(self.big_picture_dir) / filename
                if bp_file.exists():
                    conn = self.get_fresh_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                        ('big_picture_original', datetime.now(), image_id)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                    updated_count += 1
                    if updated_count <= 10:
                        print(f"  ✅ Updated {filename}: dropbox → big_picture_original")
                    continue
                
                # If path is stored, check if it's valid
                if path and os.path.exists(path):
                    if 'catalog' in path.lower():
                        conn = self.get_fresh_db_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                            ('catalog_original', datetime.now(), image_id)
                        )
                        conn.commit()
                        cursor.close()
                        conn.close()
                        updated_count += 1
                        if updated_count <= 10:
                            print(f"  ✅ Updated {filename}: dropbox → catalog_original (path-based)")
                    elif 'big_picture' in path.lower():
                        conn = self.get_fresh_db_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE image_assets SET source = %s, updated_at = %s WHERE id = %s",
                            ('big_picture_original', datetime.now(), image_id)
                        )
                        conn.commit()
                        cursor.close()
                        conn.close()
                        updated_count += 1
                        if updated_count <= 10:
                            print(f"  ✅ Updated {filename}: dropbox → big_picture_original (path-based)")
                        
            except Exception as e:
                if idx < 5:
                    print(f"  ❌ Error processing {filename}: {e}")
        
        print(f"\n📈 Dropbox source reconciliation results:")
        print(f"   ✅ Reclassified: {updated_count} out of {len(dropbox_images)}")
        
        return updated_count
    
    def run_full_reconciliation(self):
        """Run complete reconciliation process"""
        print("\n")
        print("╔" + "=" * 58 + "╗")
        print("║" + "DAM SOURCE RECONCILIATION - ONE-TIME PROCESS".center(58) + "║")
        print("║" + f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".ljust(58) + "║")
        print("╚" + "=" * 58 + "╝")
        
        log_event('info', 'DAM Source Reconciliation process started')
        
        total_updated = 0
        
        try:
            # Step 1: Reconcile catalog images
            catalog_updated = self.reconcile_catalog_images()
            total_updated += catalog_updated
            
            # Step 2: Reconcile big_picture images
            bp_updated = self.reconcile_big_picture_images()
            total_updated += bp_updated
            
            # Step 3: Check for misclassified dropbox sources
            dropbox_reclassified = self.reconcile_dropbox_sourced()
            total_updated += dropbox_reclassified
            
            # Summary
            print("\n" + "=" * 60)
            print("📋 FINAL RECONCILIATION SUMMARY")
            print("=" * 60)
            print(f"✅ Catalog images updated: {catalog_updated}")
            print(f"✅ Big picture images updated: {bp_updated}")
            print(f"✅ Dropbox sources reclassified: {dropbox_reclassified}")
            print(f"\n🎯 TOTAL UPDATES: {total_updated}")
            print("=" * 60)
            
            log_event('info', f'DAM Source Reconciliation completed: {total_updated} total updates')
            
            print("\n✅ Reconciliation process completed successfully!")
            print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
        except Exception as e:
            print(f"\n❌ Fatal error during reconciliation: {e}")
            log_event('error', f'DAM Source Reconciliation failed: {e}')
            raise
        finally:
            self.close_db_connection()


if __name__ == "__main__":
    try:
        reconciler = DAMSourceReconciler()
        reconciler.run_full_reconciliation()
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        log_event('error', f'DAM Source Reconciliation failed with error: {e}')
        sys.stderr.flush()
        sys.exit(1)
