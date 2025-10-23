#!/usr/bin/env python3
"""
Emergency T3305 Catalog Import Script
Import missing T3305 catalog images from network path
"""

import os
import sys
import shutil
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.dam_sync_service import DAMSyncService

class T3305CatalogImporter:
    def __init__(self):
        self.network_path = r"\\192.168.254.30\FDC_Database\Catalog_pictures\catalog_original"
        self.local_catalog_dir = r"D:\Catalog_pictures\catalog_original"
        self.dam_sync = DAMSyncService()
        
    def import_t3305_images(self):
        """Import T3305 catalog images from network path"""
        print("Importing T3305 catalog images...")
        
        # Ensure local directory exists
        os.makedirs(self.local_catalog_dir, exist_ok=True)
        
        # List of T3305 files to import
        t3305_files = [
            "T3305.jpg",
            "T3305_v2.jpg", 
            "T3305_v3.jpg",
            "T3305_v4.jpg",
            "T3305版6.jpg"
        ]
        
        imported_count = 0
        
        for filename in t3305_files:
            source_path = Path(self.network_path) / filename
            dest_path = Path(self.local_catalog_dir) / filename
            
            try:
                if source_path.exists():
                    # Handle T3305版6.jpg - rename to standardized format
                    if filename == "T3305版6.jpg":
                        dest_filename = "T3305_v6.jpg"
                        dest_path = Path(self.local_catalog_dir) / dest_filename
                    
                    # Copy file if it doesn't exist or source is newer
                    if not dest_path.exists() or (
                        source_path.stat().st_mtime > dest_path.stat().st_mtime
                    ):
                        shutil.copy2(str(source_path), str(dest_path))
                        print(f"✅ Imported: {filename} -> {dest_path.name}")
                        imported_count += 1
                    else:
                        print(f"ℹ️  Already exists: {dest_path.name}")
                else:
                    print(f"❌ Not found: {source_path}")
                    
            except Exception as e:
                print(f"❌ Error importing {filename}: {e}")
        
        print(f"\nImported {imported_count} T3305 catalog images")
        
        # Sync with DAM database
        if imported_count > 0:
            print("Syncing with DAM database...")
            self.dam_sync.sync_catalog_images()
            print("✅ DAM sync completed")
        
        return imported_count
    
    def verify_import(self):
        """Verify T3305 images are properly imported"""
        print("\nVerifying T3305 import...")
        
        from py.utilities.database_utils import get_mysql_connection
        
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        # Check database
        cursor.execute(
            "SELECT filename, source, is_canonical FROM image_assets WHERE sku = %s ORDER BY source, filename",
            ('T3305',)
        )
        
        results = cursor.fetchall()
        
        print(f"Found {len(results)} T3305 images in database:")
        for filename, source, is_canonical in results:
            canonical_flag = "📍 CANONICAL" if is_canonical else ""
            print(f"  - {filename} (source: {source}) {canonical_flag}")
        
        # Check file system
        catalog_files = list(Path(self.local_catalog_dir).glob("T3305*.jpg"))
        print(f"\nFound {len(catalog_files)} T3305 catalog files:")
        for file_path in sorted(catalog_files):
            print(f"  - {file_path.name}")
        
        cursor.close()
        connection.close()
        
        # Test DAM utilities
        print("\nTesting DAM utilities:")
        from py.utilities.dam_utils import get_canonical_asset_url, get_asset_by_sku
        
        canonical_url = get_canonical_asset_url('T3305', source_filter='catalog')
        if canonical_url:
            print(f"✅ Canonical URL: {canonical_url}")
        else:
            print("❌ No canonical URL found")
        
        asset = get_asset_by_sku('T3305', source_filter='catalog')
        if asset:
            print(f"✅ Asset found: {asset.filename}")
        else:
            print("❌ No asset found")

if __name__ == "__main__":
    importer = T3305CatalogImporter()
    
    print("T3305 Emergency Catalog Import")
    print("=" * 40)
    
    # Import images
    imported_count = importer.import_t3305_images()
    
    # Verify import
    importer.verify_import()
    
    print("\n" + "=" * 40)
    print("Import completed!")