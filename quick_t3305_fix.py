"""
Quick test of catalog priority for specific T3305 files
"""
import sys
sys.path.insert(0, '.')

from pathlib import Path
import hashlib
from PIL import Image
from py.utilities.database_utils import get_mysql_connection
from datetime import datetime

# T3305 files to test
test_files = [
    Path(r'D:\Catalog_pictures\catalog_original\T3305.jpg'),
    Path(r'D:\Catalog_pictures\catalog_original\T3305_v2.jpg'),
    Path(r'D:\Catalog_pictures\catalog_original\T3305_v3.jpg'),
    Path(r'D:\Catalog_pictures\catalog_original\T3305_v4.jpg'),
]

conn = get_mysql_connection()
cursor = conn.cursor()

print("=" * 80)
print("Updating T3305 files with catalog_original priority")
print("=" * 80)

for file_path in test_files:
    if not file_path.exists():
        print(f"\n✗ File not found: {file_path.name}")
        continue
    
    print(f"\nProcessing: {file_path.name}")
    
    # Calculate hash
    sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
    print(f"  SHA256: {sha256[:16]}...")
    
    # Get dimensions
    with Image.open(file_path) as img:
        width, height = img.size
    print(f"  Dimensions: {width}x{height}")
    
    # Extract SKU
    filename = file_path.name
    sku = filename.split('_')[0].split('.')[0]
    print(f"  SKU: {sku}")
    
    # Extract version
    import re
    version_match = re.search(r'_v(\d+)', filename)
    version = int(version_match.group(1)) if version_match else 1
    print(f"  Version: {version}")
    
    # Check existing
    cursor.execute("SELECT id, source, sku FROM image_assets WHERE sha256 = %s", (sha256,))
    existing = cursor.fetchone()
    
    if existing:
        img_id, existing_source, existing_sku = existing
        print(f"  Existing: ID={img_id}, Source={existing_source}, SKU={existing_sku}")
        
        if existing_source in ['dropbox', 'big_picture']:
            print(f"  → UPDATING {existing_source} → catalog_original")
            
            cursor.execute("""
                UPDATE image_assets SET 
                    sku = %s,
                    version = %s,
                    filename = %s,
                    path = %s,
                    source = %s,
                    width = %s,
                    height = %s,
                    file_size = %s,
                    is_active = TRUE,
                    is_canonical = %s,
                    updated_at = %s
                WHERE id = %s
            """, (
                sku, version, filename, str(file_path), 'catalog_original',
                width, height, file_path.stat().st_size,
                (Path(filename).stem == sku), datetime.now(), img_id
            ))
            
            print(f"  ✓ Updated {cursor.rowcount} row(s)")
        else:
            print(f"  ⊘ Skipping (already {existing_source})")
    else:
        print(f"  → NEW RECORD - INSERTING")
        
        cursor.execute("""
            INSERT INTO image_assets 
            (sku, version, filename, path, source, width, height, file_size, sha256, 
             is_active, is_canonical, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            sku, version, filename, str(file_path), 'catalog_original',
            width, height, file_path.stat().st_size, sha256, True, 
            (Path(filename).stem == sku), datetime.now(), datetime.now()
        ))
        
        print(f"  ✓ Inserted new record")

# Commit all changes
conn.commit()
print("\n" + "=" * 80)
print("✓ All changes committed")
print("=" * 80)

# Verify
print("\nVerifying T3305 records in database:")
cursor.execute("""
    SELECT sku, filename, source, LEFT(path, 50)
    FROM image_assets 
    WHERE sku = 'T3305' AND source = 'catalog_original'
    ORDER BY filename
""")

results = cursor.fetchall()
print(f"Found {len(results)} catalog_original T3305 records:")
for sku, filename, source, path in results:
    print(f"  {filename} - {source} - {path}...")

cursor.close()
conn.close()
