#!/usr/bin/env python3
"""
Test if there are any ROSS/BURLINGTON orders in the database
"""

import pyodbc
from py.utilities.setting import Config

sql_config = Config.SQL_SERVER_CONFIG
database_name = 'omsdata_FDC'

try:
    conn = pyodbc.connect(
        f'DRIVER={sql_config["driver"]};'
        f'SERVER={sql_config["server"]};'
        f'DATABASE={database_name};'
        f'UID={sql_config["username"]};'
        f'PWD={sql_config["password"]}'
    )

    cursor = conn.cursor()

    # Test 1: Check if ROSS/BURLINGTON customers exist
    print("1. Checking for ROSS/BURLINGTON customers...")
    print("-" * 80)
    
    query = """
        SELECT DISTINCT cus_id, cus_nm
        FROM dbo.customer
        WHERE cus_nm LIKE '%ROSS%' OR cus_nm LIKE '%BURLINGTON%'
        ORDER BY cus_nm
    """
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    if rows:
        print(f"Found {len(rows)} ROSS/BURLINGTON customers:")
        for row in rows:
            print(f"  {row[0]}: {row[1]}")
    else:
        print("No ROSS/BURLINGTON customers found!")
    
    # Test 2: Check order counts for specific SKU
    test_sku = "N1724 BK"
    print(f"\n2. Checking orders for {test_sku}...")
    print("-" * 80)
    
    query = """
        SELECT COUNT(*) as total_orders,
               COUNT(DISTINCT c.cus_nm) as customer_count
        FROM dbo.ord_log ol
        JOIN dbo.customer c ON ol.cus_id = c.cus_id
        WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) = UPPER(?)
    """
    
    cursor.execute(query, (test_sku,))
    row = cursor.fetchone()
    if row:
        print(f"  Total orders: {row[0]}")
        print(f"  Customer count: {row[1]}")
    
    # Test 3: Check ROSS-specific orders for SKU
    print(f"\n3. Checking ROSS/BURLINGTON orders for {test_sku}...")
    print("-" * 80)
    
    query = """
        SELECT ol.ord_num, c.cus_nm, ol.order_qty, o.INVS_NUM, o.can_dt
        FROM dbo.ord_log ol
        JOIN dbo.customer c ON ol.cus_id = c.cus_id
        JOIN dbo.orders o ON ol.ord_num = o.ord_num
        WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) = UPPER(?)
          AND (UPPER(c.cus_nm) LIKE '%ROSS%'
               OR UPPER(c.cus_nm) LIKE '%BURLINGTON%')
        ORDER BY ol.ord_num DESC
    """
    
    cursor.execute(query, (test_sku,))
    rows = cursor.fetchall()
    
    if rows:
        print(f"Found {len(rows)} ROSS/BURLINGTON orders for {test_sku}:")
        for row in rows:
            invs_status = f"Invoice #{row[3]}" if row[3] and int(row[3]) > 0 else "NOT INVOICED"
            can_status = f"Cancelled" if row[4] and int(row[4]) > 0 else "Active"
            print(f"  Order {row[0]}: {row[1]}, Qty={row[2]}, {invs_status}, {can_status}")
    else:
        print(f"No ROSS/BURLINGTON orders found for {test_sku}")
    
    # Test 4: Check with pending/active filter
    print(f"\n4. Checking PENDING/ACTIVE ROSS/BURLINGTON orders for {test_sku}...")
    print("-" * 80)
    
    query = """
        SELECT ol.ord_num, c.cus_nm, ol.order_qty, o.INVS_NUM, o.can_dt, o.ship_dt
        FROM dbo.ord_log ol
        JOIN dbo.customer c ON ol.cus_id = c.cus_id
        JOIN dbo.orders o ON ol.ord_num = o.ord_num
        WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) = UPPER(?)
          AND (UPPER(c.cus_nm) LIKE '%ROSS%'
               OR UPPER(c.cus_nm) LIKE '%BURLINGTON%')
          AND (
                (o.INVS_NUM IS NULL OR CAST(o.INVS_NUM AS INT) = 0)
                OR (o.can_dt IS NULL OR CAST(o.can_dt AS INT) = 0)
          )
        ORDER BY ol.ord_num DESC
    """
    
    cursor.execute(query, (test_sku,))
    rows = cursor.fetchall()
    
    if rows:
        print(f"Found {len(rows)} PENDING/ACTIVE ROSS/BURLINGTON orders for {test_sku}:")
        for row in rows:
            invs_status = f"Invoice #{row[3]}" if row[3] and int(row[3]) > 0 else "NOT INVOICED"
            can_status = f"Cancelled" if row[4] and int(row[4]) > 0 else "Active"
            ship_date_val = row[5] if row[5] else "NULL"
            print(f"  Order {row[0]}: {row[1]}, Qty={row[2]}, {invs_status}, {can_status}, ship_dt={ship_date_val}")
    else:
        print(f"No PENDING/ACTIVE ROSS/BURLINGTON orders found for {test_sku}!")

    cursor.close()
    conn.close()

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
