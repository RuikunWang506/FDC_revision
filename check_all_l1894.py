"""
Check ALL L1894 records in database (including inactive)
"""
import sys
sys.path.insert(0, r'C:\FDC_FLASK_APP')

from app import app
from py.utilities.database_utils import get_mysql_connection

with app.app_context():
    conn = get_mysql_connection()
    if conn:
        cur = conn.cursor(dictionary=True)
        
        # Get all L1894 records (any SKU starting with L1894)
        cur.execute("""
            SELECT id, filename, sku, source, is_active, width, height, created_at, path
            FROM image_assets 
            WHERE sku LIKE 'L1894%'
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        
        print(f"Total L1894% records in database: {len(rows)}")
        print("=" * 80)
        
        for row in rows:
            path_display = row['path'][:60] + '...' if row['path'] and len(row['path']) > 60 else row['path']
            print(f"ID: {row['id']}")
            print(f"  Filename: {row['filename']}")
            print(f"  SKU: {row['sku']}")
            print(f"  Source: {row['source']}")
            print(f"  Active: {row['is_active']}")
            print(f"  Dimensions: {row['width']}x{row['height']}")
            print(f"  Created: {row['created_at']}")
            print(f"  Path: {path_display}")
            print("-" * 80)
        
        cur.close()
        conn.close()
    else:
        print("Failed to connect to MySQL")
