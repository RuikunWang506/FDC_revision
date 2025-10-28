#!/usr/bin/env python3
"""
Fast catalog-only sync - skips big_picture processing
Use this when you only need to update catalog images for item_lookup
"""
import os
import sys

sys.path.insert(0, os.path.abspath('.'))

from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2

print("Starting CATALOG-ONLY sync (skipping big_picture)")
print("="*60)

sync_service = DAMSyncServiceV2()

# Initialize stats
import time
sync_service.stats['start_time'] = time.time()

# 1. Sync catalog images only (with forced index rebuild)
print("\n1. Syncing catalog_original images...")
sync_service.sync_catalog_images_batch(force_rebuild_index=True)

# 2. Update canonical flags
print("\n2. Updating canonical flags...")
sync_service.update_canonical_flags()

sync_service.stats['end_time'] = time.time()

# Print final statistics
sync_service.print_final_stats()

print("\n✓ Catalog-only sync completed!")
print("Big picture sync skipped to save time.")
print("All catalog images should now appear in item_lookup.")
