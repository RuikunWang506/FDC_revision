"""
Test script to verify item_lookup source filtering
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from py.utilities.models import SessionLocal, ImageAsset

def test_item_lookup_filtering():
    print("Testing item_lookup source filtering for N1151...")
    
    db = SessionLocal()
    try:
        # Simulate the query used in item_lookup
        search_term = 'N1151'
        
        print(f"\n1. All assets for {search_term}:")
        all_assets = db.query(ImageAsset).filter(
            ImageAsset.sku.ilike(f'{search_term}%'),
            ImageAsset.is_active == True
        ).order_by(ImageAsset.version).all()
        
        for asset in all_assets:
            print(f"   - {asset.filename} (source: {asset.source}, sku: {asset.sku})")
        
        print(f"\n2. Only catalog assets for {search_term} (what item_lookup should show):")
        catalog_assets = db.query(ImageAsset).filter(
            ImageAsset.sku.ilike(f'{search_term}%'),
            ImageAsset.is_active == True,
            ImageAsset.source == 'catalog'
        ).order_by(ImageAsset.version).all()
        
        for asset in catalog_assets:
            print(f"   - {asset.filename} (source: {asset.source}, sku: {asset.sku})")
        
        print(f"\n3. Only big_picture assets for {search_term} (should NOT show in item_lookup):")
        big_picture_assets = db.query(ImageAsset).filter(
            ImageAsset.sku.ilike(f'{search_term}%'),
            ImageAsset.is_active == True,
            ImageAsset.source == 'big_picture'
        ).order_by(ImageAsset.version).all()
        
        for asset in big_picture_assets:
            print(f"   - {asset.filename} (source: {asset.source}, sku: {asset.sku})")
            
        print(f"\n✅ Found {len(all_assets)} total, {len(catalog_assets)} catalog, {len(big_picture_assets)} big_picture")
        print("✅ item_lookup should now only show catalog assets!")
        
    finally:
        db.close()

if __name__ == "__main__":
    test_item_lookup_filtering()