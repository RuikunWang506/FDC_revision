#!/usr/bin/env python3
"""
Smart Auto-Filter Implementation
Efficient filtering without reprocessing all images
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection

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
        """Fast catalog processing with version management"""
        import shutil
        
        try:
            os.makedirs(self.catalog_dest, exist_ok=True)
            
            filename = Path(source_path).name
            main_path = Path(self.catalog_dest) / f"{sku}.jpg"
            
            # If main doesn't exist, copy as main
            if not main_path.exists():
                shutil.copy2(source_path, str(main_path))
                print(f"✅ New catalog: {sku}.jpg")
                return True
            
            # If main exists, save as version
            version_num = self.get_next_version_fast(sku)
            version_path = Path(self.catalog_dest) / f"{sku}_v{version_num}.jpg"
            shutil.copy2(source_path, str(version_path))
            print(f"✅ Catalog version: {sku}_v{version_num}.jpg")
            return True
            
        except Exception as e:
            print(f"❌ Catalog processing error: {e}")
            return False
    
    def process_big_picture_fast(self, source_path, sku):
        """Fast big picture processing"""
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
        
        with open(self.processed_files_log, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    
    def run_incremental_filter(self):
        """Run smart incremental filtering"""
        print("🚀 Starting Smart Auto-Filter (Incremental Mode)")
        print("=" * 50)
        
        # Get only new/modified files
        new_files = self.get_new_or_modified_files()
        
        if not new_files:
            print("✅ No new files to process")
            return
        
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
        with open(self.processed_files_log, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().isoformat()}|SCAN_COMPLETE|\n")
        
        print("\n" + "=" * 50)
        print(f"✅ Processing Complete!")
        print(f"📊 Total processed: {processed_count}")
        print(f"📖 Catalog images: {catalog_count}")
        print(f"🖼️  Big pictures: {big_picture_count}")
        print(f"⚡ Processing time: Minimal (incremental)")

if __name__ == "__main__":
    filter_service = SmartAutoFilter()
    filter_service.run_incremental_filter()
