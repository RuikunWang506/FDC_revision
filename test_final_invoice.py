import pyodbc
import time
from py.utilities.setting import Config

sql_config = Config.SQL_SERVER_CONFIG

order_number = "35622"
database_name = 'omsdata_FDC'

print(f"Testing updated query for order {order_number}")
print("=" * 80)

start_time = time.time()

conn = pyodbc.connect(
    f'DRIVER={sql_config["driver"]};'
    f'SERVER={sql_config["server"]};'
    f'DATABASE={database_name};'
    f'UID={sql_config["username"]};'
    f'PWD={sql_config["password"]}'
)

cursor = conn.cursor()

query = """
    WITH OrderNum AS (
        SELECT ? AS ord_num
    ),
    OrderInfo AS (
        SELECT O.ORD_NUM, O.CUS_ID, O.COMM_LN, O.UT_DESC, O.PROD_CD, O.ORDER_QTY, O.UNIT_PRS, O.PC_UNIT, C.CUS_NM
        FROM DBO.ORD_LOG O
        INNER JOIN DBO.CUSTOMER C ON O.CUS_ID = C.CUS_ID
        WHERE O.ORD_NUM = (SELECT ord_num FROM OrderNum)
    ),
    OrderDates AS (
        SELECT 
            ord_num,
            ord_dt AS order_date,
            ship_dt AS ship_date,
            can_dt AS cancel_date,
            PO_NUM,
            INVS_NUM,
            ORD_AMT
        FROM dbo.orders
        WHERE ord_num = (SELECT ord_num FROM OrderNum)
    ),
    CustomerInfo AS (
        SELECT 
            CUS_ID,
            PRIORITY,
            PHONE
        FROM dbo.CUSTOMER
        WHERE CUS_ID IN (SELECT DISTINCT CUS_ID FROM OrderInfo)
    )
    SELECT DISTINCT TOP 5
        O.ORD_NUM AS order_num, 
        O.CUS_NM AS Customer, 
        O.CUS_ID AS customer_id,
        ISNULL(CAST(OD.INVS_NUM AS VARCHAR), 'N/A') AS invoice_num,
        OD.PO_NUM AS po_num,
        OD.ORD_AMT AS order_amt
    FROM OrderInfo O
    INNER JOIN OrderDates OD ON O.ORD_NUM = OD.ord_num
    LEFT JOIN CustomerInfo CI ON O.CUS_ID = CI.CUS_ID
"""

cursor.execute(query, (order_number,))
results = cursor.fetchall()

print(f"Query completed in {time.time() - start_time:.2f} seconds")
print(f"Results: {len(results)} rows\n")

for row in results:
    print(f"Order: {row[0]}")
    print(f"Customer: {row[1].strip()}")
    print(f"Customer ID: {row[2].strip()}")
    print(f"Invoice #: {row[3]}")  # Should be 33615
    print(f"PO: {row[4] if row[4] else 'N/A'}")
    print(f"Amount: {row[5]}")
    print()

conn.close()

if len(results) == 1:
    print("✅ SUCCESS - Query returns single result with correct invoice number!")
else:
    print(f"⚠️ WARNING - Query returns {len(results)} rows")
