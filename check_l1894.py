"""
Check if L1894 images exist in database and what SKUs they have
"""
from py.utilities.models import SessionLocal, ImageAsset

def check_l1894():
    """Check for L1894 related images"""
    db = SessionLocal()
    try:
        # Search for any images with L1894 in the filename or SKU
        print("=== Searching for L1894 images ===\n")
        
        # Check by SKU
        print("1. By SKU 'L1894':")
        assets_by_sku = db.query(ImageAsset).filter(
            ImageAsset.sku == 'L1894',
            ImageAsset.is_active == True
        ).all()
        print(f"   Found {len(assets_by_sku)} active images with SKU='L1894'")
        for asset in assets_by_sku:
            print(f"   - {asset.filename} (source: {asset.source})")
        
        # Check by SKU pattern (in case it was saved wrong)
        print("\n2. By SKU pattern 'L1894%':")
        assets_by_pattern = db.query(ImageAsset).filter(
            ImageAsset.sku.like('L1894%'),
            ImageAsset.is_active == True
        ).all()
        print(f"   Found {len(assets_by_pattern)} active images with SKU starting with 'L1894'")
        for asset in assets_by_pattern:
            print(f"   - SKU: {asset.sku} | File: {asset.filename} (source: {asset.source})")
        
        # Check by filename
        print("\n3. By filename pattern '%L1894%':")
        assets_by_filename = db.query(ImageAsset).filter(
            ImageAsset.filename.like('%L1894%'),
            ImageAsset.is_active == True
        ).all()
        print(f"   Found {len(assets_by_filename)} active images with 'L1894' in filename")
        for asset in assets_by_filename:
            print(f"   - SKU: {asset.sku} | File: {asset.filename} (source: {asset.source})")
        
        # Check specifically for the uploaded file
        print("\n4. Specific check for 'L1894_17X7.5X12.5X12_1.JPG':")
        specific_file = db.query(ImageAsset).filter(
            ImageAsset.filename.like('%L1894_17X7.5X12.5X12_1%'),
            ImageAsset.is_active == True
        ).all()
        print(f"   Found {len(specific_file)} images")
        for asset in specific_file:
            print(f"   - SKU: {asset.sku} | File: {asset.filename}")
            print(f"     Source: {asset.source}")
            print(f"     Path: {asset.path}")
            print(f"     Created: {asset.created_at}")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == '__main__':
    check_l1894()
