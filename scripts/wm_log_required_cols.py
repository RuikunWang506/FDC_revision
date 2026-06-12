import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()
cur.execute(
    """
    SELECT COLUMN_NAME, COLUMN_DEFAULT
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'wm_log' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
      AND COLUMN_NAME <> 'REC_NUM'
    ORDER BY ORDINAL_POSITION
    """
)
print("Required wm_log columns without defaults:")
for row in cur.fetchall():
    print(row)
conn.close()
