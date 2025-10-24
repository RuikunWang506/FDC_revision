from pathlib import Path
import hashlib
from py.utilities.database_utils import get_mysql_connection

files = ['T3305_v2.jpg', 'T3305_v3.jpg']
conn = get_mysql_connection()
cursor = conn.cursor()

for fname in files:
    fp = Path(r'D:\Catalog_pictures\catalog_original') / fname
    if fp.exists():
        sha = hashlib.sha256(fp.read_bytes()).hexdigest()
        print(f'{fname}: SHA256={sha[:16]}...')
        cursor.execute('SELECT id, sku, filename, source FROM image_assets WHERE sha256=%s', (sha,))
        row = cursor.fetchone()
        if row:
            print(f'  DB: ID={row[0]}, SKU={row[1]}, File={row[2]}, Source={row[3]}')
        else:
            print(f'  DB: NOT FOUND')
    else:
        print(f'{fname}: FILE NOT FOUND')

cursor.close()
conn.close()
