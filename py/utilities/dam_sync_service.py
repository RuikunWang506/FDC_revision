#!/usr/bin/env python3
"""
DAM Integration and Sync System
Synchronizes organized images with the DAM database
"""

import os
import sys
from pathlib import Path
from PIL import Image
import hashlib
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection
from py.utilities.models import ImageAsset

class DAMSyncService:
    def __init__(self):
        self.catalog_dir = r"D:\Catalog_pictures\catalog_original"
        self.big_picture_dir = r"D:\Individual_big_picture"
        self.dropbox_dir = r"Dropbox\Pictures-China (1)\container catalog"
        
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
            print(f"Error reading image {file_path}: {e}")
            return None
    
    def is_canonical_image(self, sku, filename, source):
        """Determine if this should be the canonical image for the SKU"""
        if source == 'catalog':
            # For catalog images, the main image (without version suffix) is canonical
            base_name = Path(filename).stem
            return base_name == sku
        elif source == 'big_picture':
            # For big pictures, we don't typically set canonical (or use first one)
            return False
        else:  # dropbox
            # For dropbox, simple filename match makes it canonical
            base_name = Path(filename).stem.upper()
            return base_name == sku.upper()
    
    def sync_catalog_images(self):
        """Sync catalog_original folder with DAM database"""
        print("Syncing catalog images...")
        
        if not os.path.exists(self.catalog_dir):
            print(f"Catalog directory not found: {self.catalog_dir}")
            return
        
        connection = self.get_db_connection()
        cursor = connection.cursor()
        
        synced_count = 0
        
        for file_path in Path(self.catalog_dir).glob("*.jpg"):
            try:
                filename = file_path.name
                sku = self.extract_sku_from_filename(filename)
                
                if not sku:
                    print(f"Could not extract SKU from {filename}")
                    continue
                
                # Check if already exists
                cursor.execute(
                    "SELECT id FROM image_assets WHERE filename = %s AND source = %s",
                    (filename, 'catalog')
                )
                
                if cursor.fetchone():
                    continue  # Already exists
                
                # Get image info
                image_info = self.get_image_info(str(file_path))
                if not image_info:
                    continue
                
                sha256_hash = self.calculate_file_hash(str(file_path))
                is_canonical = self.is_canonical_image(sku, filename, 'catalog')
                
                # Determine version number from filename
                version = 1
                if '_v' in filename:
                    import re
                    match = re.search(r'_v(\d+)', filename)
                    if match:
                        version = int(match.group(1))
                
                # Insert into database
                cursor.execute("""
                    INSERT INTO image_assets 
                    (sku, version, filename, path, source, width, height, file_size, sha256, is_active, is_canonical, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    sku, version, filename, str(file_path), 'catalog',
                    image_info['width'], image_info['height'], image_info['size_bytes'],
                    sha256_hash, True, is_canonical,
                    datetime.now(), datetime.now()
                ))
                
                synced_count += 1
                print(f"Added catalog image: {filename} (SKU: {sku})")
                
            except Exception as e:
                print(f"Error processing {file_path}: {e}")
        
        connection.commit()
        cursor.close()
        connection.close()
        
        print(f"Synced {synced_count} catalog images")
    
    def sync_big_picture_images(self):
        """Sync Individual_big_picture folder with DAM database"""
        print("Syncing big picture images...")
        
        if not os.path.exists(self.big_picture_dir):
            print(f"Big picture directory not found: {self.big_picture_dir}")
            return
        
        connection = self.get_db_connection()
        cursor = connection.cursor()
        
        synced_count = 0
        
        for sku_folder in Path(self.big_picture_dir).iterdir():
            if not sku_folder.is_dir():
                continue
            
            sku = sku_folder.name
            
            for file_path in sku_folder.rglob("*.jpg"):
                try:
                    filename = file_path.name
                    relative_path = str(file_path.relative_to(sku_folder))
                    
                    # Check if already exists
                    cursor.execute(
                        "SELECT id FROM image_assets WHERE filename = %s AND source = %s AND sku = %s",
                        (filename, 'big_picture', sku)
                    )
                    
                    if cursor.fetchone():
                        continue  # Already exists
                    
                    # Get image info
                    image_info = self.get_image_info(str(file_path))
                    if not image_info:
                        continue
                    
                    sha256_hash = self.calculate_file_hash(str(file_path))
                    
                    # For big pictures, version is always 1 unless specified
                    version = 1
                    
                    # For big pictures, we can store the relative path in description
                    cursor.execute("""
                        INSERT INTO image_assets 
                        (sku, version, filename, path, source, width, height, file_size, sha256, is_active, is_canonical, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        sku, version, filename, str(file_path), 'big_picture',
                        image_info['width'], image_info['height'], image_info['size_bytes'],
                        sha256_hash, True, False,
                        datetime.now(), datetime.now()
                    ))
                    
                    synced_count += 1
                    print(f"Added big picture: {sku}/{relative_path}")
                    
                except Exception as e:
                    print(f"Error processing {file_path}: {e}")
        
        connection.commit()
        cursor.close()
        connection.close()
        
        print(f"Synced {synced_count} big picture images")
    
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
    
    def remove_deleted_files(self):
        """Remove database entries for files that no longer exist"""
        print("Checking for deleted files...")
        
        connection = self.get_db_connection()
        cursor = connection.cursor()
        
        # Check catalog images
        cursor.execute("SELECT id, filename FROM image_assets WHERE source = 'catalog'")
        catalog_images = cursor.fetchall()
        
        deleted_count = 0
        for img_id, filename in catalog_images:
            file_path = Path(self.catalog_dir) / filename
            if not file_path.exists():
                cursor.execute("UPDATE image_assets SET is_active = FALSE WHERE id = %s", (img_id,))
                deleted_count += 1
                print(f"Marked as inactive: {filename}")
        
        # Check big picture images
        cursor.execute("SELECT id, sku, filename, description FROM image_assets WHERE source = 'big_picture'")
        big_picture_images = cursor.fetchall()
        
        for img_id, sku, filename, description in big_picture_images:
            if description:
                file_path = Path(self.big_picture_dir) / sku / description
            else:
                file_path = Path(self.big_picture_dir) / sku / filename
                
            if not file_path.exists():
                cursor.execute("UPDATE image_assets SET is_active = FALSE WHERE id = %s", (img_id,))
                deleted_count += 1
                print(f"Marked as inactive: {sku}/{filename}")
        
        connection.commit()
        cursor.close()
        connection.close()
        
        print(f"Marked {deleted_count} deleted files as inactive")
    
    def update_canonical_flags(self):
        """Update canonical flags based on image quality and naming"""
        print("Updating canonical flags...")
        
        connection = self.get_db_connection()
        cursor = connection.cursor()
        
        # Get all SKUs
        cursor.execute("SELECT DISTINCT sku FROM image_assets WHERE is_active = TRUE")
        skus = cursor.fetchall()
        
        for (sku,) in skus:
            # For each SKU, find the best catalog image to be canonical
            cursor.execute("""
                SELECT id, filename, width, height, source
                FROM image_assets 
                WHERE sku = %s AND source = 'catalog' AND is_active = TRUE
                ORDER BY width * height DESC, filename ASC
            """, (sku,))
            
            catalog_images = cursor.fetchall()
            
            if catalog_images:
                # Clear all canonical flags for this SKU
                cursor.execute(
                    "UPDATE image_assets SET is_canonical = FALSE WHERE sku = %s",
                    (sku,)
                )
                
                # Set the highest resolution catalog image as canonical
                best_image_id = catalog_images[0][0]
                cursor.execute(
                    "UPDATE image_assets SET is_canonical = TRUE WHERE id = %s",
                    (best_image_id,)
                )
                
                print(f"Set canonical for {sku}: {catalog_images[0][1]}")
        
        connection.commit()
        cursor.close()
        connection.close()
    
    def run_full_sync(self):
        """Run complete synchronization"""
        print("Starting full DAM synchronization...")
        
        self.sync_catalog_images()
        self.sync_big_picture_images()
        self.remove_deleted_files()
        self.update_canonical_flags()
        
        print("DAM synchronization completed!")

if __name__ == "__main__":
    sync_service = DAMSyncService()
    sync_service.run_full_sync()