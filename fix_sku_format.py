"""
Fix SKU format in database - extract proper SKU from dimension-based folder names
Example: L1894_17X7.5X12.5X12_1 → L1894
"""
import re
from py.utilities.models import SessionLocal, ImageAsset

def extract_clean_sku(sku_text):
    """Extract SKU before dimension patterns or underscores"""
    if not sku_text:
        return None
    
    # Pattern 1: Extract SKU before dimension patterns like _17X7.5X12.5X12 or _12x8x10
    dimension_pattern = r'(_\d+[xX]\d+|\d+[xX]\d+[xX]\d+)'
    match = re.search(dimension_pattern, sku_text)
    if match:
        clean_sku = sku_text[:match.start()].rstrip('_')
        return clean_sku.upper() if clean_sku else None
    
    # Pattern 2: If still no clear pattern, take everything before first underscore
    if '_' in sku_text:
        clean_sku = sku_text.split('_')[0]
        return clean_sku.upper() if clean_sku else None
    
    # Default: return as-is
    return sku_text.upper()

def fix_skus():
    """Fix SKUs in database that contain dimension patterns"""
    db = SessionLocal()
    try:
        # Find all assets with SKUs containing dimension patterns
        assets = db.query(ImageAsset).filter(
            ImageAsset.is_active == True
        ).all()
        
        updated_count = 0
        for asset in assets:
            original_sku = asset.sku
            clean_sku = extract_clean_sku(original_sku)
            
            if clean_sku and clean_sku != original_sku:
                print(f"Updating: {original_sku} → {clean_sku}")
                print(f"  File: {asset.filename}")
                asset.sku = clean_sku
                updated_count += 1
        
        if updated_count > 0:
            db.commit()
            print(f"\n✓ Updated {updated_count} SKU(s)")
        else:
            print("✓ No SKUs need updating")
            
    except Exception as e:
        db.rollback()
        print(f"✗ Error: {e}")
    finally:
        db.close()

if __name__ == '__main__':
    print("=== Fixing SKU Format in Database ===")
    fix_skus()
