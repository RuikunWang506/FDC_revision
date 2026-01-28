"""
Compare SQLAlchemy vs raw MySQL connection for L1894
"""
import sys
sys.path.insert(0, r'C:\FDC_FLASK_APP')

from app import app

with app.app_context():
    from py.utilities.models import SessionLocal, ImageAsset
    from py.utilities.database_utils import get_mysql_connection
    
    print("=" * 60)
    print("SQLAlchemy (via SessionLocal):")
    db = SessionLocal()
    try:
        count_sql_alchemy = db.query(ImageAsset).filter(ImageAsset.sku == 'L1894').count()
        print(f"Count: {count_sql_alchemy}")
        
        # Get all rows to see what's there
        rows = db.query(ImageAsset).filter(ImageAsset.sku == 'L1894').all()
        for row in rows:
            print(f"  - {row.filename} | active={row.is_active} | source={row.source}")
    finally:
        db.close()
    
    print("\n" + "=" * 60)
    print("Raw MySQL (via get_mysql_connection):")
    conn = get_mysql_connection()
    if conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM image_assets WHERE sku='L1894'")
        rows = cur.fetchall()
        print(f"Count: {len(rows)}")
        for row in rows:
            print(f"  - {row['filename']} | active={row['is_active']} | source={row['source']}")
        cur.close()
        conn.close()
    else:
        print("Connection failed")
