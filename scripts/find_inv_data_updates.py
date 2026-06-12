import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

cur.execute(
    """
    SELECT o.name, o.type_desc
    FROM sys.objects o
    WHERE o.type IN ('P', 'TR', 'FN', 'IF', 'TF')
      AND OBJECT_DEFINITION(o.object_id) LIKE '%inv_data%'
      AND OBJECT_DEFINITION(o.object_id) LIKE '%in_stock%'
    ORDER BY o.type_desc, o.name
    """
)
print("=== Objects touching inv_data.in_stock ===")
for row in cur.fetchall():
    print(row)

# Search for UPDATE inv_data in all modules
cur.execute(
    """
    SELECT o.name, o.type_desc
    FROM sys.sql_modules m
    JOIN sys.objects o ON m.object_id = o.object_id
    WHERE m.definition LIKE '%UPDATE%inv_data%'
       OR m.definition LIKE '%UPDATE [dbo].[inv_data]%'
    """
)
print("\n=== UPDATE inv_data anywhere ===")
for row in cur.fetchall():
    print(row)

conn.close()
