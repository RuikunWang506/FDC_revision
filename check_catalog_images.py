#!/usr/bin/env python3
"""
Check catalog images in DAM database
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.database_utils import get_mysql_connection

connection = get_mysql_connection()
cursor = connection.cursor()

# Check catalog images count
cursor.execute("SELECT COUNT(*) FROM image_assets WHERE source = %s", ('catalog',))
catalog_count = cursor.fetchone()[0]
print(f"Total catalog images in database: {catalog_count}")

# Check all source types
cursor.execute("SELECT source, COUNT(*) FROM image_assets GROUP BY source")
source_counts = cursor.fetchall()
print("\nImages by source:")
for source, count in source_counts:
    print(f"  - {source}: {count}")

cursor.close()
connection.close()