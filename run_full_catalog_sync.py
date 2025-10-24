"""
Run full catalog sync to update all dropbox duplicates to catalog_original
"""
import sys
sys.path.insert(0, '.')

from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2
import time

print("=" * 80)
print("Starting Full Catalog Sync")
print("=" * 80)

sync = DAMSyncServiceV2()
sync.stats['start_time'] = time.time()

try:
    sync.sync_catalog_images_batch(force_rebuild_index=False)
    
    print("\n" + "=" * 80)
    print("SYNC COMPLETE")
    print("=" * 80)
    print(f"Total Processed: {sync.stats['total_processed']:,}")
    print(f"Added: {sync.stats['total_added']:,}")
    print(f"Updated: {sync.stats['total_updated']:,}")
    print(f"Skipped: {sync.stats['total_skipped']:,}")
    print(f"Errors: {sync.stats['total_errors']:,}")
    
except Exception as e:
    print(f"\n✗ Error during sync: {e}")
    import traceback
    traceback.print_exc()
