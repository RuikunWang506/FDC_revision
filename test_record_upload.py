"""
Test the _record_big_picture_uploads function to see if it works
"""
import sys
import os
import logging

# Add the app directory to the path
sys.path.insert(0, r'C:\FDC_FLASK_APP')

# Set up logging to console for debugging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')

# Import Flask app to get the context
from app import app

# Test the function
with app.app_context():
    from py.utilities.image_upload_routes import _record_big_picture_uploads, _get_image_info_and_hash
    from py.utilities.database_utils import get_mysql_connection
    
    # First, test if we can get image info
    print("=" * 60)
    print("Testing image info extraction...")
    test_file = r'D:\Individual_big_picture\L1894\L1894_17X7.5X12.5X12_1.JPG'
    print(f"File: {test_file}")
    print(f"File exists: {os.path.exists(test_file)}")
    
    info, sha256 = _get_image_info_and_hash(test_file)
    print(f"Info: {info}")
    print(f"SHA256: {sha256[:20]}..." if sha256 else "SHA256: None")
    
    # Test MySQL connection
    print("\n" + "=" * 60)
    print("Testing MySQL connection...")
    conn = get_mysql_connection()
    if conn:
        print("MySQL connection: OK")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM image_assets WHERE sku='L1894'")
        count = cur.fetchone()[0]
        print(f"Current count of L1894 images in DB: {count}")
        cur.close()
        conn.close()
    else:
        print("MySQL connection: FAILED")
    
    # Now test the full function
    print("\n" + "=" * 60)
    print("Testing _record_big_picture_uploads...")
    test_files = [
        r'D:\Individual_big_picture\L1894\L1894_17X7.5X12.5X12_1.JPG',
        r'D:\Individual_big_picture\L1894\L1894_17X7.5X12.5X12_1_01.JPG'
    ]
    print(f"Files to test: {test_files}")
    print(f"SKU: L1894")
    print("-" * 60)
    
    try:
        _record_big_picture_uploads(test_files, 'L1894')
        print("\nSUCCESS: Function completed without errors")
        
        # Check if they were added
        conn = get_mysql_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM image_assets WHERE sku='L1894'")
            count = cur.fetchone()[0]
            print(f"\nFinal count of L1894 images in DB: {count}")
            cur.close()
            conn.close()
            
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
