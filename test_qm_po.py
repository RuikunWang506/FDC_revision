#!/usr/bin/env python
"""Test script to verify qm_po table and container search"""
import pyodbc
from py.utilities.setting import Config

sql_server_config = Config.SQL_SERVER_CONFIG

try:
    print("Connecting to FDC database...")
    conn_fdc = pyodbc.connect(
        f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
    )
    print("✓ Connected to FDC")
    cursor = conn_fdc.cursor()
    
    # Test 1: Check if qm_po table exists
    print("\n1. Checking qm_po table...")
    cursor.execute("SELECT COUNT(*) FROM qm_po")
    count = cursor.fetchone()[0]
    print(f"✓ qm_po has {count} rows")
    
    # Test 2: Get column names
    print("\n2. Getting column names from qm_po...")
    cursor.execute("SELECT TOP 1 * FROM qm_po")
    columns = [column[0] for column in cursor.description]
    print(f"✓ Columns: {columns[:10]}... (showing first 10)")
    
    # Test 3: Check for F24237
    print("\n3. Searching for F24237...")
    query = """
        SELECT COUNT(*)
        FROM qm_po qp
        LEFT JOIN qm_po_color qpc ON qp.id = qpc.po_id
        WHERE qp.po_num = 'F24237' OR qp.container_no = 'F24237' OR qp.po_num LIKE '%F24237%'
    """
    cursor.execute(query)
    count = cursor.fetchone()[0]
    print(f"✓ Found {count} qm_po rows for F24237")
    
    # Test 4: Get actual data
    print("\n4. Getting actual data for F24237...")
    query = """
        SELECT TOP 5
            qp.sku,
            qpc.color_code,
            qpc.qty,
            qp.order_date,
            qp.cost,
            qp.Sale_price,
            qp.po_num
        FROM qm_po qp
        LEFT JOIN qm_po_color qpc ON qp.id = qpc.po_id
        WHERE qp.po_num = 'F24237'
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    print(f"✓ Got {len(rows)} rows")
    for row in rows:
        print(f"  - SKU: {row[0]}, Color: {row[1]}, Qty: {row[2]}, Date: {row[3]}, Cost: {row[4]}, Price: {row[5]}, PO: {row[6]}")
    
    cursor.close()
    conn_fdc.close()
    print("\n✓ All tests passed!")
    
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
