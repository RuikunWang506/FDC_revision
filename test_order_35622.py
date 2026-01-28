import pyodbc
import time
from py.utilities.setting import Config

sql_config = Config.SQL_SERVER_CONFIG

order_number = "35622"  # 5 digits, so should use omsdata_FDC database

# Determine database
if len(order_number) == 5:
    database_name = 'omsdata_FDC'
elif len(order_number) == 4:
    database_name = 'oms_EF'

print(f"Testing order {order_number} in database: {database_name}")
print("=" * 80)

start_time = time.time()

try:
    conn = pyodbc.connect(
        f'DRIVER={sql_config["driver"]};'
        f'SERVER={sql_config["server"]};'
        f'DATABASE={database_name};'
        f'UID={sql_config["username"]};'
        f'PWD={sql_config["password"]}',
        timeout=30
    )
    
    cursor = conn.cursor()
    
    print(f"Connection established in {time.time() - start_time:.2f} seconds")
    
    # Test basic order query first
    query_start = time.time()
    cursor.execute("SELECT TOP 1 * FROM orders WHERE ord_num = ?", (order_number,))
    row = cursor.fetchone()
    print(f"Basic order query: {time.time() - query_start:.2f} seconds")
    
    if row:
        cols = [column[0] for column in cursor.description]
        print(f"Available columns: {cols}")
        
        # Find customer ID column (might be CUS_ID or cus_id)
        cus_id_col = None
        for col in cols:
            if col.upper() == 'CUS_ID':
                cus_id_col = col
                break
        
        if cus_id_col:
            cus_id_idx = cols.index(cus_id_col)
            customer_id = row[cus_id_idx]
            print(f"Found order {order_number}, Customer ID: {customer_id}")
        else:
            print("Could not find CUS_ID column")
            print("First few values:", row[:5])
            conn.close()
            exit()
    else:
        print(f"Order {order_number} NOT FOUND in {database_name}")
        conn.close()
        exit()
    
    # Test invoice query
    print("\nTesting invoice query...")
    query_start = time.time()
    cursor.execute("""
        SELECT TOP 5 INVS_NUM, INVS_CD, 
               CAST(INVS_NUM AS VARCHAR) + '-' + CAST(INVS_CD AS VARCHAR) AS invoice_number
        FROM invoice 
        WHERE cus_id = ?
    """, (customer_id,))
    invoices = cursor.fetchall()
    print(f"Invoice query: {time.time() - query_start:.2f} seconds")
    print(f"Found {len(invoices)} invoices for customer {customer_id}")
    for inv in invoices:
        print(f"  Invoice: {inv[2]}")
    
    # Test the full complex query
    print("\nTesting full order query with invoice join (FIXED - TOP 1)...")
    query_start = time.time()
    
    full_query = """
        WITH OrderNum AS (
            SELECT ? AS ord_num
        ),
        OrderInfo AS (
            SELECT O.ORD_NUM, O.CUS_ID, O.COMM_LN, O.UT_DESC, O.PROD_CD, O.ORDER_QTY, O.UNIT_PRS, O.PC_UNIT, C.CUS_NM
            FROM DBO.ORD_LOG O
            INNER JOIN DBO.CUSTOMER C ON O.CUS_ID = C.CUS_ID
            WHERE O.ORD_NUM = (SELECT ord_num FROM OrderNum)
        ),
        CustomerInfo AS (
            SELECT 
                CUS_ID,
                PRIORITY,
                PHONE,
                SHP_CITY,
                SHP_STATE,
                SHP_ZIP
            FROM dbo.CUSTOMER
            WHERE CUS_ID IN (SELECT DISTINCT CUS_ID FROM OrderInfo)
        ),
        InvoiceInfo AS (
            SELECT TOP 1
                inv.CUS_ID,
                CAST(inv.INVS_NUM AS VARCHAR) + '-' + CAST(inv.INVS_CD AS VARCHAR) AS invoice_number
            FROM dbo.invoice inv
            WHERE inv.CUS_ID IN (SELECT DISTINCT CUS_ID FROM OrderInfo)
            ORDER BY inv.INVS_DT DESC, inv.INVS_NUM DESC, inv.INVS_CD DESC
        )
        SELECT DISTINCT TOP 5
            O.ORD_NUM AS order_num, 
            O.CUS_NM AS Customer, 
            O.CUS_ID AS customer_id,
            ISNULL(INV.invoice_number, 'N/A') AS invoice_num
        FROM OrderInfo O
        LEFT JOIN CustomerInfo CI ON O.CUS_ID = CI.CUS_ID
        LEFT JOIN InvoiceInfo INV ON O.CUS_ID = INV.CUS_ID
    """
    
    cursor.execute(full_query, (order_number,))
    results = cursor.fetchall()
    print(f"Full query: {time.time() - query_start:.2f} seconds")
    print(f"Results: {len(results)} rows (should be less duplicates now)")
    
    for row in results:
        print(f"Order: {row[0]}, Customer: {row[1]}, Cust ID: {row[2]}, Invoice: {row[3]}")
    
    cursor.close()
    conn.close()
    
    print(f"\nTotal time: {time.time() - start_time:.2f} seconds")
    print("SUCCESS - No timeout issues detected")
    
except Exception as e:
    print(f"\nERROR after {time.time() - start_time:.2f} seconds:")
    print(f"Error type: {type(e).__name__}")
    print(f"Error message: {str(e)}")
