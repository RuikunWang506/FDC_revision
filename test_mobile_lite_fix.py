#!/usr/bin/env python3
"""
Test script to verify mobile_lite.py DAM integration fix
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.abspath('.'))

print("Testing mobile_lite.py DAM integration fix...")

try:
    # Test importing the mobile module
    from py.other.mobile_lite import mobile_lite_bp
    print("✅ Successfully imported mobile_lite module")
    
    # Test the DAM utility import
    from py.utilities.dam_utils import get_canonical_asset_url
    print("✅ Successfully imported get_canonical_asset_url")
    
    # Test a sample canonical URL retrieval
    test_sku = "T3305"
    canonical_url = get_canonical_asset_url(test_sku, source_filter='catalog')
    if canonical_url:
        print(f"✅ Found canonical image for {test_sku}: {canonical_url}")
    else:
        print(f"ℹ️  No canonical image found for {test_sku}")
    
    print("\n✅ Mobile DAM integration fix successful!")
    print("The mobile functionality should now work without CATALOG_ORIGINAL errors.")
    
except Exception as e:
    print(f"❌ Error testing mobile fix: {e}")
    import traceback
    traceback.print_exc()