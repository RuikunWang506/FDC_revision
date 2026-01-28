import pyodbc
from py.utilities.setting import Config

sql_config = Config.SQL_SERVER_CONFIG

order_number = "35622"
database_name = 'omsdata_FDC'

print(f"Checking if orders table has direct INVS_NUM for order {order_number}")
print("=" * 80)

conn = pyodbc.connect(
    f'DRIVER={sql_config["driver"]};'
    f'SERVER={sql_config["server"]};'
    f'DATABASE={database_name};'
    f'UID={sql_config["username"]};'
    f'PWD={sql_config["password"]}'
)

cursor = conn.cursor()

# Check orders table for INVS_NUM column
cursor.execute("""
    SELECT ord_num, cus_id, INVS_NUM, PO_NUM, ORD_AMT, SALES_NUM
    FROM orders 
    WHERE ord_num = ?
""", (order_number,))

row = cursor.fetchone()
if row:
    print(f"Order Number: {row[0]}")
    print(f"Customer ID: {row[1]}")
    print(f"INVS_NUM (Invoice #): {row[2]}")
    print(f"PO Number: {row[3]}")
    print(f"Order Amount: {row[4]}")
    print(f"Salesman: {row[5]}")
    
    invs_num = row[2]
    
    # Now check if this invoice number exists in the invoice table
    print("\n" + "=" * 80)
    print(f"Checking invoice table for INVS_NUM = {invs_num}")
    print("=" * 80)
    
    cursor.execute("""
        SELECT INVS_NUM, INVS_CD, CUS_ID, INVS_AMT, INVS_DT
        FROM invoice
        WHERE INVS_NUM = ? AND CUS_ID = ?
    """, (invs_num, row[1]))
    
    invoices = cursor.fetchall()
    print(f"Found {len(invoices)} invoice record(s):")
    for inv in invoices:
        print(f"  INVS_NUM: {inv[0]}, INVS_CD: {inv[1]}, Customer: {inv[2]}, Amount: {inv[3]}, Date: {inv[4]}")
else:
    print(f"Order {order_number} not found!")

conn.close()
