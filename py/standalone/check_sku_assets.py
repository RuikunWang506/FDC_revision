#!/usr/bin/env python3
import os
import sys
from pathlib import Path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config

SKU = sys.argv[1] if len(sys.argv) > 1 else 'M1140'

conn = get_mysql_connection()
if not conn:
    print('Failed to connect to MySQL')
    sys.exit(2)

cur = conn.cursor(dictionary=True)
cur.execute("SELECT COUNT(*) AS cnt FROM image_assets WHERE sku=%s", (SKU,))
print(f"Total records for {SKU}: ", cur.fetchone()['cnt'])

cur.execute("""
SELECT source, is_canonical, is_active, COUNT(*) AS cnt
FROM image_assets WHERE sku=%s
GROUP BY source, is_canonical, is_active
ORDER BY source
""", (SKU,))
rows = cur.fetchall()
print('Breakdown by source/canonical/active:')
for r in rows:
    print(r)

cur.execute("""
SELECT id, filename, source, width, height, file_size, sha256, is_canonical, is_active, path
FROM image_assets
WHERE sku=%s
ORDER BY source, is_canonical DESC, width*height DESC, filename
LIMIT 20
""", (SKU,))
rows = cur.fetchall()
print('Sample records:')
for r in rows:
    print(r)

cur.close()
conn.close()

# Filesystem presence check
print('\nFilesystem check:')
cat_dir = Path(Config.IMAGE_DAM_CATALOG_DIR)
big_dir = Path(Config.IMAGE_DAM_BIG_PICTURE_DIR) / SKU
cat_matches = list(cat_dir.glob(f'{SKU}*.jpg'))
print('Catalog_original files:', [str(p) for p in cat_matches])
print('Big_picture folder exists:', big_dir.exists())
if big_dir.exists():
    print('Big_picture files (first 10):', [p.name for p in list(big_dir.glob('*.jpg'))[:10]])
