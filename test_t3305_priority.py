"""
Test the updated DAM sync with T3305 priority handling
"""
import sys
sys.path.insert(0, '.')

from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2
from py.utilities.database_utils import get_mysql_connection

# Check before sync
print("=" * 80)
print("BEFORE SYNC - Checking T3305 records")
print("=" * 80)

conn = get_mysql_connection()
cursor = conn.cursor()

cursor.execute("""
    SELECT id, sku, filename, source, LEFT(path, 50) as short_path
    FROM image_assets 
    WHERE sku LIKE 'T3305%' 
    ORDER BY source, sku
    LIMIT 10
""")

print("\nExisting T3305 records:")
for row in cursor.fetchall():
    print(f"  ID: {row[0]}, SKU: {row[1]}, File: {row[2]}, Source: {row[3]}, Path: {row[4]}...")

cursor.close()
conn.close()

print("\n" + "=" * 80)
print("RUNNING SYNC (Catalog Original Only)")
print("=" * 80)

# Run sync
sync = DAMSyncServiceV2()
sync.sync_catalog_images_batch(force_rebuild_index=False)

print("\n" + "=" * 80)
print("AFTER SYNC - Checking T3305 records")
print("=" * 80)

conn = get_mysql_connection()
cursor = conn.cursor()

cursor.execute("""
    SELECT id, sku, filename, source, LEFT(path, 50) as short_path
    FROM image_assets 
    WHERE sku LIKE 'T3305%' 
    ORDER BY source, sku
    LIMIT 10
""")

print("\nUpdated T3305 records:")
for row in cursor.fetchall():
    print(f"  ID: {row[0]}, SKU: {row[1]}, File: {row[2]}, Source: {row[3]}, Path: {row[4]}...")

cursor.close()
conn.close()

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"Total Processed: {sync.stats['total_processed']}")
print(f"Added: {sync.stats['total_added']}")
print(f"Updated: {sync.stats['total_updated']}")
print(f"Skipped: {sync.stats['total_skipped']}")
print(f"Errors: {sync.stats['total_errors']}")
