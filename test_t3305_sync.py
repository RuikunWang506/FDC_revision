"""
Test script to sync only T3305 items from catalog_original
"""
import sys
import os
from pathlib import Path
from PIL import Image
import hashlib

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection

def calculate_sha256(file_path):
    """Calculate SHA256 hash of file"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_image_dimensions(file_path):
    """Get image dimensions"""
    try:
        with Image.open(file_path) as img:
            return img.size  # Returns (width, height)
    except Exception as e:
        print(f"  Error getting dimensions: {e}")
        return None, None

def test_t3305_sync():
    """Test syncing T3305 files only"""
    
    catalog_dir = Path(r'D:\Catalog_pictures\catalog_original')
    
    print("=" * 80)
    print("T3305 DAM Sync Test")
    print("=" * 80)
    
    # Find all T3305 files
    print("\n1. Scanning for T3305 files in catalog_original...")
    t3305_files = []
    
    for file_path in catalog_dir.rglob('*'):
        if file_path.is_file():
            filename = file_path.name
            if filename.startswith('T3305'):
                t3305_files.append(file_path)
    
    print(f"   Found {len(t3305_files)} T3305 files")
    
    if not t3305_files:
        print("   No T3305 files found!")
        return
    
    # Show first few files
    print("\n2. Sample files:")
    for i, file_path in enumerate(t3305_files[:5]):
        print(f"   {i+1}. {file_path.name}")
    if len(t3305_files) > 5:
        print(f"   ... and {len(t3305_files) - 5} more")
    
    # Connect to database
    print("\n3. Connecting to DAM database...")
    conn = None
    cursor = None
    
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        print("   ✓ Connected")
        
        # Check existing T3305 records
        print("\n4. Checking existing T3305 records in database...")
        cursor.execute("""
            SELECT sku, filename, source 
            FROM image_assets 
            WHERE sku LIKE 'T3305%'
            ORDER BY sku
        """)
        existing_records = cursor.fetchall()
        print(f"   Found {len(existing_records)} existing T3305 records")
        
        if existing_records:
            print("\n   Sample existing records:")
            for i, (sku, filename, source) in enumerate(existing_records[:5]):
                print(f"   {i+1}. SKU: {sku}, File: {filename}, Source: {source}")
            if len(existing_records) > 5:
                print(f"   ... and {len(existing_records) - 5} more")
        
        # Try to insert/update each T3305 file
        print("\n5. Processing T3305 files...")
        print("-" * 80)
        
        success_count = 0
        error_count = 0
        errors = []
        
        for idx, file_path in enumerate(t3305_files, 1):
            print(f"\n   [{idx}/{len(t3305_files)}] Processing: {file_path.name}")
            
            try:
                # Extract SKU from filename
                filename = file_path.name
                sku = filename.split('_')[0] if '_' in filename else filename.split('.')[0]
                
                print(f"      SKU: {sku}")
                
                # Get file info
                relative_path = str(file_path.relative_to(catalog_dir.parent))
                file_size = file_path.stat().st_size
                print(f"      Path: {relative_path}")
                print(f"      Size: {file_size:,} bytes")
                
                # Calculate hash
                print(f"      Calculating SHA256...")
                sha256 = calculate_sha256(file_path)
                print(f"      SHA256: {sha256[:16]}...")
                
                # Get dimensions
                print(f"      Getting dimensions...")
                width, height = get_image_dimensions(file_path)
                print(f"      Dimensions: {width}x{height}")
                
                # Check if exists
                cursor.execute("""
                    SELECT id, sha256_hash 
                    FROM image_assets 
                    WHERE sku = %s AND source = 'catalog_original'
                """, (sku,))
                existing = cursor.fetchone()
                
                if existing:
                    record_id, existing_hash = existing
                    print(f"      Record exists (ID: {record_id})")
                    
                    if existing_hash != sha256:
                        print(f"      Hash changed - UPDATING...")
                        cursor.execute("""
                            UPDATE image_assets 
                            SET filename = %s,
                                file_path = %s,
                                sha256_hash = %s,
                                width = %s,
                                height = %s,
                                file_size = %s,
                                updated_at = NOW()
                            WHERE id = %s
                        """, (filename, relative_path, sha256, width, height, file_size, record_id))
                        conn.commit()
                        print(f"      ✓ UPDATED")
                    else:
                        print(f"      Hash unchanged - SKIPPING")
                else:
                    print(f"      New record - INSERTING...")
                    cursor.execute("""
                        INSERT INTO image_assets 
                        (sku, filename, file_path, source, sha256_hash, width, height, file_size, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """, (sku, filename, relative_path, 'catalog_original', sha256, width, height, file_size))
                    conn.commit()
                    print(f"      ✓ INSERTED")
                
                success_count += 1
                
            except Exception as e:
                error_count += 1
                error_msg = f"File: {file_path.name}, Error: {str(e)}"
                errors.append(error_msg)
                print(f"      ✗ ERROR: {e}")
                continue
        
        # Summary
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print(f"Total files processed: {len(t3305_files)}")
        print(f"Successful: {success_count}")
        print(f"Errors: {error_count}")
        
        if errors:
            print("\nErrors encountered:")
            for i, error in enumerate(errors, 1):
                print(f"{i}. {error}")
        
    except Exception as e:
        print(f"\n✗ Database error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            print("\n✓ Database connection closed")

if __name__ == '__main__':
    test_t3305_sync()
