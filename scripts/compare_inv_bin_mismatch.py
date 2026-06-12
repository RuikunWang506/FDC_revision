import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections

conn, _ = get_db_connections()
cur = conn.cursor()

cur.execute(
    """
    SELECT TOP 20 i.PROD_CD, id.in_stock,
           ISNULL((SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = i.PROD_CD), 0) AS bin_sum,
           id.in_stock - ISNULL((SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = i.PROD_CD), 0) AS diff
    FROM inv i
    INNER JOIN inv_data id ON i.PROD_CD = id.Prod_cd
    WHERE id.in_stock <> 0
    ORDER BY ABS(id.in_stock - ISNULL((SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = i.PROD_CD), 0)) DESC
    """
)
print("=== Largest inv_data vs binfile mismatches ===")
for row in cur.fetchall():
    print(f"{row[0].strip():20} inv={float(row[1]):8.0f} bin={float(row[2]):8.0f} diff={float(row[3]):8.0f}")

cur.execute(
    """
    SELECT COUNT(*) FROM inv i
    INNER JOIN inv_data id ON i.PROD_CD = id.Prod_cd
    WHERE id.in_stock <> ISNULL((SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = i.PROD_CD), 0)
    """
)
print("\nTotal mismatches:", cur.fetchone()[0])

# inv_data columns
cur.execute(
    """
    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'inv_data' ORDER BY ORDINAL_POSITION
    """
)
print("\ninv_data columns:", [r[0] for r in cur.fetchall()])

conn.close()
