import pyodbc
from py.utilities.setting import Config

sql_config = Config.SQL_SERVER_CONFIG

# Query for order 33615 in oms_EF database (order 35622 in UI corresponds to 33615 in DB)
conn = pyodbc.connect(
    f'DRIVER={sql_config["driver"]};'
    f'SERVER={sql_config["server"]};'
    f'DATABASE=oms_EF;'
    f'UID={sql_config["username"]};'
    f'PWD={sql_config["password"]}'
)

cursor = conn.cursor()

# First, let's find the relationship between order and customer
print("=" * 80)
print("ORDERS TABLE - Order 33615:")
print("=" * 80)
cursor.execute("SELECT * FROM orders WHERE ord_num = 33615")
row = cursor.fetchone()
if row:
    cols = [column[0] for column in cursor.description]
    for col, val in zip(cols, row):
        print(f"{col}: {val}")
    print(f"\nFound customer ID: {row[cols.index('cus_id')]}")
    customer_id = row[cols.index('cus_id')]
else:
    print("Order 33615 not found!")
    customer_id = '8175'  # Use provided customer ID

print("\n" + "=" * 80)
print("INVOICE TABLE - All Columns/Headers:")
print("=" * 80)

# Get invoice table structure
cursor.execute("SELECT TOP 0 * FROM invoice")
cols = [column[0] for column in cursor.description]
print("INVOICE TABLE HEADERS:")
for i, col in enumerate(cols, 1):
    print(f"{i}. {col}")

# Now search for invoices related to customer 8175
print("\n" + "=" * 80)
print(f"INVOICES for Customer {customer_id}:")
print("=" * 80)

cursor.execute(f"SELECT TOP 5 * FROM invoice WHERE cus_id = ?", (customer_id,))
invoices = cursor.fetchall()

if invoices:
    print(f"Found {len(invoices)} invoice(s) for customer {customer_id}:")
    for invoice in invoices:
        print("\n" + "-" * 80)
        for col, val in zip(cols, invoice):
            print(f"{col}: {val}")
else:
    print(f"No invoices found for customer {customer_id}")

# Check if there's a relationship between orders and invoices
print("\n" + "=" * 80)
print("CHECKING ORDER-INVOICE RELATIONSHIP:")
print("=" * 80)

# Try to find invoice related to order 33615
cursor.execute("""
    SELECT i.*, o.ord_num 
    FROM invoice i
    INNER JOIN orders o ON i.cus_id = o.cus_id
    WHERE o.ord_num = 33615
""")

order_invoices = cursor.fetchall()
if order_invoices:
    print(f"Found {len(order_invoices)} invoice(s) related to order 33615:")
    for inv in order_invoices[:3]:  # Show first 3
        print(f"\nInvoice Number: {inv[0]}, Customer: {inv[2]}, Amount: {inv[4]}")
else:
    print("No invoices found directly linked to order 33615")

conn.close()
