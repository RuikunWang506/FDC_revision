#!/usr/bin/env python3
"""
Image Organization Automation System
Phase 1: Background Service Implementation
"""

import os
import shutil
import time
import hashlib
from PIL import Image
import schedule
import logging
from pathlib import Path
import re
from datetime import datetime

class ImageOrganizerService:
    def __init__(self):
        from py.utilities.setting import Config
        # Configuration
        self.dropbox_path = Config.DROPBOX_CONTAINER_CATALOG_DIR
        self.catalog_dest = Config.IMAGE_DAM_CATALOG_DIR
        self.big_picture_dest = Config.IMAGE_DAM_BIG_PICTURE_DIR
        
        # Supported image formats
        self.supported_formats = Config.IMAGE_EXTENSIONS
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('image_organizer.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def extract_sku_from_path(self, file_path):
        """Extract SKU from filename or folder path"""
        path_parts = Path(file_path).parts
        filename = Path(file_path).stem
        
        # Try to extract SKU patterns (letters + numbers)
        sku_patterns = [
            r'([A-Z]+\d+)',  # T3305, N1151, etc.
            r'(\d+[A-Z]*)',  # 52621, etc.
        ]
        
        # Check filename first
        for pattern in sku_patterns:
            match = re.search(pattern, filename.upper())
            if match:
                return match.group(1)
        
        # Check folder names
        for part in reversed(path_parts):
            for pattern in sku_patterns:
                match = re.search(pattern, part.upper())
                if match:
                    return match.group(1)
        
        return None
    
    def get_image_hash(self, image_path):
        """Generate hash for duplicate detection"""
        try:
            with Image.open(image_path) as img:
                # Resize to small size for comparison
                img = img.resize((64, 64))
                img = img.convert('RGB')
                return hashlib.md5(img.tobytes()).hexdigest()
        except Exception as e:
            self.logger.error(f"Error generating hash for {image_path}: {e}")
            return None
    
    def get_next_version_number(self, sku, catalog_dir):
        """Get next version number for catalog image"""
        existing_files = list(Path(catalog_dir).glob(f"{sku}*.jpg"))
        version_numbers = []
        
        for file_path in existing_files:
            filename = file_path.stem
            if filename == sku:
                version_numbers.append(1)
            else:
                # Look for _v2, _v3, etc.
                match = re.search(r'_v(\d+)$', filename)
                if match:
                    version_numbers.append(int(match.group(1)))
        
        if not version_numbers:
            return 1
        else:
            return max(version_numbers) + 1
    
    def process_catalog_image(self, source_path, sku):
        """Process and organize catalog image"""
        try:
            # Ensure catalog directory exists
            os.makedirs(self.catalog_dest, exist_ok=True)
            
            # Check for existing images
            existing_main = Path(self.catalog_dest) / f"{sku}.jpg"
            
            if existing_main.exists():
                # Compare resolutions
                with Image.open(source_path) as new_img:
                    new_size = new_img.size
                    new_area = new_size[0] * new_size[1]
                
                with Image.open(existing_main) as existing_img:
                    existing_size = existing_img.size
                    existing_area = existing_size[0] * existing_size[1]
                
                if new_area > existing_area:
                    # New image is higher resolution, replace main
                    # Move old main to version
                    version_num = self.get_next_version_number(sku, self.catalog_dest)
                    old_version_path = Path(self.catalog_dest) / f"{sku}_v{version_num}.jpg"
                    shutil.move(str(existing_main), str(old_version_path))
                    
                    # Copy new image as main
                    shutil.copy2(source_path, str(existing_main))
                    self.logger.info(f"Replaced main catalog image for {sku} with higher resolution")
                else:
                    # New image is lower resolution, save as version
                    version_num = self.get_next_version_number(sku, self.catalog_dest)
                    version_path = Path(self.catalog_dest) / f"{sku}_v{version_num}.jpg"
                    shutil.copy2(source_path, str(version_path))
                    self.logger.info(f"Saved new version {version_num} for catalog {sku}")
            else:
                # No existing image, save as main
                shutil.copy2(source_path, str(existing_main))
                self.logger.info(f"Saved new catalog image for {sku}")
                
        except Exception as e:
            self.logger.error(f"Error processing catalog image {source_path}: {e}")
    
    def process_big_picture_image(self, source_path, sku):
        """Process and organize big picture image"""
        try:
            # Create SKU folder
            sku_folder = Path(self.big_picture_dest) / sku
            os.makedirs(sku_folder, exist_ok=True)
            
            # Generate unique filename to avoid conflicts
            filename = Path(source_path).name
            dest_path = sku_folder / filename
            
            # Handle naming conflicts
            counter = 1
            original_stem = Path(source_path).stem
            original_suffix = Path(source_path).suffix
            
            while dest_path.exists():
                new_name = f"{original_stem}_{counter:02d}{original_suffix}"
                dest_path = sku_folder / new_name
                counter += 1
            
            shutil.copy2(source_path, str(dest_path))
            self.logger.info(f"Saved big picture for {sku}: {dest_path.name}")
            
        except Exception as e:
            self.logger.error(f"Error processing big picture {source_path}: {e}")
    
    # Legacy methods below - kept for backward compatibility with smart_auto_filter.py
    # Not used by bulk upload system
    
    def is_catalog_image(self, image_path):
        """
        Legacy method for automatic classification.
        Used by smart_auto_filter.py background service.
        Not used by bulk upload system (user manually chooses catalog vs detail).
        """
        try:
            from py.utilities.image_text_detector import ImageTextDetector
            detector = ImageTextDetector()
            classification = detector.classify_image_type(image_path)
            return (classification == 'catalog')
        except Exception as e:
            self.logger.error(f"Error analyzing image {image_path}: {e}")
            return False
    
    def scan_dropbox_folder(self):
        """Scan dropbox folder for new images"""
        if not os.path.exists(self.dropbox_path):
            self.logger.warning(f"Dropbox path not found: {self.dropbox_path}")
            return
        
        self.logger.info("Starting dropbox scan...")
        
        for root, dirs, files in os.walk(self.dropbox_path):
            for file in files:
                file_path = Path(root) / file
                
                # Check if it's a supported image format
                if file_path.suffix.lower() in self.supported_formats:
                    sku = self.extract_sku_from_path(str(file_path))
                    
                    if sku:
                        if self.is_catalog_image(str(file_path)):
                            self.process_catalog_image(str(file_path), sku)
                        else:
                            self.process_big_picture_image(str(file_path), sku)
                    else:
                        self.logger.warning(f"Could not extract SKU from: {file_path}")
    
    def run_scheduled_scan(self):
        """Run the scheduled scan"""
        self.logger.info("Running scheduled image organization scan")
        self.scan_dropbox_folder()
        self.logger.info("Scheduled scan completed")
    
    def start_service(self):
        """Start the background service"""
        self.logger.info("Starting Image Organization Service")
        
        # Schedule to run every 3 days at 2 AM
        schedule.every(3).days.at("02:00").do(self.run_scheduled_scan)
        
        # Run initial scan
        self.run_scheduled_scan()
        
        # Keep service running
        while True:
            schedule.run_pending()
            time.sleep(3600)  # Check every hour

if __name__ == "__main__":
    service = ImageOrganizerService()
    service.start_service()