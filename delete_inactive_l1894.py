"""
Delete inactive L1894 records from database
"""
import sys
sys.path.insert(0, r'C:\FDC_FLASK_APP')

from app import app

with app.app_context():
    from py.utilities.database_utils import get_mysql_connection
    
    conn = get_mysql_connection()
    if not conn:
        print("Failed to connect to database")
        exit(1)
    
    cur = conn.cursor(dictionary=True)
    
    # Find inactive L1894 records
    print("Finding inactive L1894 records...")
    cur.execute("SELECT id, filename, sku, source, is_active FROM image_assets WHERE sku='L1894' AND is_active=0")
    rows = cur.fetchall()
    
    print(f"Found {len(rows)} inactive L1894 records:")
    for row in rows:
        print(f"  ID {row['id']}: {row['filename']} | source={row['source']}")
    
    if rows:
        confirm = input(f"\nDelete these {len(rows)} records? (yes/no): ")
        if confirm.lower() == 'yes':
            for row in rows:
                cur.execute("DELETE FROM image_assets WHERE id=%s", (row['id'],))
                print(f"  Deleted ID {row['id']}: {row['filename']}")
            
            conn.commit()
            print(f"\nSuccessfully deleted {len(rows)} records")
            
            # Verify deletion
            cur.execute("SELECT COUNT(*) as count FROM image_assets WHERE sku='L1894'")
            result = cur.fetchone()
            print(f"Remaining L1894 records in database: {result['count']}")
        else:
            print("Deletion cancelled")
    else:
        print("No inactive L1894 records found")
    
    cur.close()
    conn.close()
