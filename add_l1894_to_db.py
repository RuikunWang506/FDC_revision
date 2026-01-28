"""
Manually add L1894 images to database
"""
import os
from datetime import datetime
from py.utilities.models import SessionLocal, ImageAsset

def add_l1894_images():
    """Add L1894 images from Individual_big_picture to database"""
    db = SessionLocal()
    try:
        # Path to L1894 folder
        l1894_path = r"\\192.168.1.8\FDC_Database\Individual_big_picture\L1894"
        
        if not os.path.exists(l1894_path):
            print(f"Path does not exist: {l1894_path}")
            return
        
        print(f"Scanning: {l1894_path}")
        
        # Get all image files
        image_files = []
        for file in os.listdir(l1894_path):
            if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
                image_files.append(file)
        
        print(f"Found {len(image_files)} image files")
        
        added_count = 0
        for filename in image_files:
            full_path = os.path.join(l1894_path, filename)
            
            # Check if already in database
            existing = db.query(ImageAsset).filter_by(
                path=full_path,
                is_active=True
            ).first()
            
            if existing:
                print(f"  Skip: {filename} (already in DB)")
                continue
            
            # Get file stats
            file_stats = os.stat(full_path)
            file_size = file_stats.st_size
            
            # Get image dimensions
            try:
                from PIL import Image
                with Image.open(full_path) as img:
                    width, height = img.size
            except:
                width, height = None, None
            
            # Create new asset
            asset = ImageAsset(
                sku='L1894',
                filename=filename,
                path=full_path,
                source='big_picture',
                width=width,
                height=height,
                file_size=file_size,
                is_canonical=False,
                is_active=True,
                version=1,
                created_at=datetime.fromtimestamp(file_stats.st_mtime)
            )
            
            db.add(asset)
            added_count += 1
            print(f"  Added: {filename} (SKU: L1894, {width}x{height}, {file_size} bytes)")
        
        db.commit()
        print(f"\n✓ Successfully added {added_count} images to database")
        
    except Exception as e:
        db.rollback()
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == '__main__':
    print("=== Adding L1894 Images to Database ===")
    add_l1894_images()
