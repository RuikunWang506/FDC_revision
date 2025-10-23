"""
Test script to verify DAM source filtering functionality
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from py.utilities.dam_utils import search_assets_by_sku_pattern

def test_source_filtering():
    print("Testing DAM source filtering...")
    
    # Test catalog source only
    print("\n1. Testing catalog source filtering:")
    catalog_assets = search_assets_by_sku_pattern('T3186', source_filter='catalog')
    print(f"Found {len(catalog_assets)} catalog assets for T3186")
    for asset in catalog_assets:
        print(f"   - {asset.filename} (source: {asset.source})")
    
    # Test big_picture source only  
    print("\n2. Testing big_picture source filtering:")
    big_picture_assets = search_assets_by_sku_pattern('T3186', source_filter='big_picture')
    print(f"Found {len(big_picture_assets)} big_picture assets for T3186")
    for asset in big_picture_assets:
        print(f"   - {asset.filename} (source: {asset.source})")
    
    # Test all sources
    print("\n3. Testing all sources (no filter):")
    all_assets = search_assets_by_sku_pattern('T3186')
    print(f"Found {len(all_assets)} total assets for T3186")
    for asset in all_assets:
        print(f"   - {asset.filename} (source: {asset.source})")
    
    print("\n✅ Source filtering test completed!")

if __name__ == "__main__":
    test_source_filtering()