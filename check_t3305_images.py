#!/usr/bin/env python3
"""
Check T3305 images in the DAM database
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.abspath('.'))

print("Checking T3305 images in DAM database...")

try:
    from py.utilities.database_utils import get_mysql_connection
    from py.utilities.models import ImageAsset
    from py.utilities.dam_utils import get_canonical_asset_url, get_asset_by_sku
    
    # Check database connection
    connection = get_mysql_connection()
    if not connection:
        print("❌ Failed to connect to database")
        exit(1)
    
    cursor = connection.cursor()
    
    # Check all T3305 related images in database
    print("\n1. Searching for T3305 images in database:")
    query = "SELECT id, sku, filename, source, is_active, is_canonical FROM image_assets WHERE sku LIKE %s"
    cursor.execute(query, ('%T3305%',))
    results = cursor.fetchall()
    
    if results:
        print(f"Found {len(results)} T3305 images:")
        for row in results:
            id_, sku, filename, source, is_active, is_canonical = row
            status = "✅ Active" if is_active else "❌ Inactive"
            canonical = "📍 Canonical" if is_canonical else ""
            print(f"  - ID: {id_}, SKU: {sku}, File: {filename}, Source: {source}, {status} {canonical}")
    else:
        print("❌ No T3305 images found in database")
    
    # Test DAM utility functions
    print("\n2. Testing DAM utility functions:")
    
    # Test get_asset_by_sku
    asset = get_asset_by_sku('T3305', source_filter='catalog')
    if asset:
        print(f"✅ get_asset_by_sku found: {asset.filename} (source: {asset.source})")
    else:
        print("❌ get_asset_by_sku returned None")
    
    # Test get_canonical_asset_url
    canonical_url = get_canonical_asset_url('T3305', source_filter='catalog')
    if canonical_url:
        print(f"✅ get_canonical_asset_url found: {canonical_url}")
    else:
        print("❌ get_canonical_asset_url returned None")
    
    # Check exact SKU match
    print("\n3. Checking exact SKU matches:")
    cursor.execute("SELECT id, sku, filename, source, is_active, is_canonical FROM image_assets WHERE sku = %s", ('T3305',))
    exact_results = cursor.fetchall()
    
    if exact_results:
        print(f"Found {len(exact_results)} exact T3305 matches:")
        for row in exact_results:
            id_, sku, filename, source, is_active, is_canonical = row
            status = "✅ Active" if is_active else "❌ Inactive"
            canonical = "📍 Canonical" if is_canonical else ""
            print(f"  - ID: {id_}, SKU: {sku}, File: {filename}, Source: {source}, {status} {canonical}")
    else:
        print("❌ No exact T3305 SKU matches found")
    
    # Check if files exist with different variations
    print("\n4. Checking T3305 variations:")
    variations = ['T3305', 'T3305版6', 'T3305_v2', 'T3305_v3', 'T3305_v4']
    for var in variations:
        cursor.execute("SELECT id, sku, filename, source, is_active FROM image_assets WHERE sku = %s AND source = 'catalog'", (var,))
        var_results = cursor.fetchall()
        if var_results:
            print(f"  ✅ Found {var}: {len(var_results)} images")
            for row in var_results:
                print(f"    - {row[2]} (ID: {row[0]}, Active: {row[4]})")
        else:
            print(f"  ❌ No images found for: {var}")
    
    cursor.close()
    connection.close()
    
except Exception as e:
    print(f"❌ Error checking T3305 images: {e}")
    import traceback
    traceback.print_exc()