"""
Detailed test of why L1894 big_picture file isn't being added to database
"""
import sys
sys.path.insert(0, r'C:\FDC_FLASK_APP')

from app import app

with app.app_context():
    from py.utilities.image_upload_routes import _get_image_info_and_hash
    from py.utilities.database_utils import get_mysql_connection
    
    test_file = r'D:\Individual_big_picture\L1894\L1894_17X7.5X12.5X12_1.JPG'
    sku = 'L1894'
    
    print("=" * 80)
    print("Testing why file isn't being added to database")
    print(f"File: {test_file}")
    print(f"SKU: {sku}")
    print("=" * 80)
    
    # Step 1: Get image info and hash
    info, sha256 = _get_image_info_and_hash(test_file)
    print(f"\n1. Image Info:")
    print(f"   Width x Height: {info['width']} x {info['height']}")
    print(f"   Size: {info['size_bytes']} bytes")
    print(f"   SHA256: {sha256[:20]}...")
    
    # Step 2: Check if hash exists in database
    conn = get_mysql_connection()
    cur = conn.cursor(dictionary=True)
    
    print(f"\n2. Checking for existing hash in database...")
    cur.execute("SELECT id, source, sku, filename, is_active FROM image_assets WHERE sha256=%s", (sha256,))
    rows = cur.fetchall()
    print(f"   Found {len(rows)} record(s) with this hash:")
    for row in rows:
        print(f"     ID {row['id']}: {row['filename']} | SKU={row['sku']} | source={row['source']} | active={row['is_active']}")
    
    # Step 3: Check if filename/source/sku combo exists
    filename = 'L1894_17X7.5X12.5X12_1.JPG'
    print(f"\n3. Checking for existing filename/source/SKU combo...")
    cur.execute(
        "SELECT id, is_active FROM image_assets WHERE filename=%s AND source=%s AND sku=%s",
        (filename, 'big_picture', sku)
    )
    rows = cur.fetchall()
    print(f"   Found {len(rows)} record(s) with filename='{filename}', source='big_picture', SKU='{sku}':")
    for row in rows:
        print(f"     ID {row['id']}: active={row['is_active']}")
    
    # Step 4: Try to insert manually
    print(f"\n4. Attempting manual insert...")
    from datetime import datetime
    try:
        cur.execute("""
            INSERT INTO image_assets
            (sku, version, filename, path, source, width, height, file_size, sha256,
             is_active, is_canonical, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            sku, 1, filename, test_file, 'big_picture',
            info['width'], info['height'], info['size_bytes'], sha256,
            True, False, datetime.now(), datetime.now()
        ))
        conn.commit()
        print("   INSERT SUCCESS!")
        
        # Verify it was inserted
        cur.execute("SELECT id, is_active FROM image_assets WHERE sha256=%s AND source='big_picture'", (sha256,))
        row = cur.fetchone()
        if row:
            print(f"   Verified: New record ID {row['id']}, active={row['is_active']}")
        else:
            print("   ERROR: Record not found after insert!")
            
    except Exception as e:
        print(f"   INSERT FAILED: {e}")
        conn.rollback()
    
    cur.close()
    conn.close()
