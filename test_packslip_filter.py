#!/usr/bin/env python3
"""
Test script to verify the packslip order filtering for ROSS/BURLINGTON customers
Tests the new get_filtered_order_summary_by_skus() function

Example:
    N1724 BK (ROSS customer)
    - Historical orders: 29784, 33149, 34577, 35744
    - Active (pending/not invoiced): 35744
    
    Expected: Only order 35744 should be returned after filter
"""

import pyodbc
from py.utilities.setting import Config

sql_config = Config.SQL_SERVER_CONFIG

# Test with N1724 BK
test_sku = "N1724 BK"
database_name = 'omsdata_FDC'

print(f"Testing packslip order filter for SKU: {test_sku}")
print("=" * 80)

try:
    conn = pyodbc.connect(
        f'DRIVER={sql_config["driver"]};'
        f'SERVER={sql_config["server"]};'
        f'DATABASE={database_name};'
        f'UID={sql_config["username"]};'
        f'PWD={sql_config["password"]}'
    )

    cursor = conn.cursor()

    # First, show ALL orders for this SKU sold to ROSS (before filter)
    print("\n1. ALL ORDERS for N1724 BK sold to ROSS (NO filter):")
    print("-" * 80)
    
    all_query = """
        SELECT
            UPPER(LTRIM(RTRIM(ol.prod_cd))) AS sku,
            LTRIM(RTRIM(c.cus_nm)) AS customer_name,
            CAST(ol.ord_num AS VARCHAR(30)) AS order_id,
            CAST(ISNULL(ol.order_qty, 0) AS INT) AS order_qty,
            o.INVS_NUM as invoice_num,
            o.can_dt as cancel_date
        FROM dbo.ord_log ol
        JOIN dbo.customer c ON ol.cus_id = c.cus_id
        JOIN dbo.orders o ON ol.ord_num = o.ord_num
        WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) = ?
          AND (
                UPPER(c.cus_nm) LIKE '%ROSS%'
                OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACTORY%'
                OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACOTORY%'
          )
        ORDER BY ol.ord_num DESC
    """
    
    cursor.execute(all_query, (test_sku,))
    all_rows = cursor.fetchall()
    
    all_order_ids = []
    for row in all_rows:
        sku, cust_name, ord_id, qty, inv_num, can_dt = row
        all_order_ids.append(ord_id)
        inv_status = f"Invoice #{inv_num}" if inv_num and int(inv_num) > 0 else "NOT INVOICED"
        can_status = f"Cancelled (date: {can_dt})" if can_dt and int(can_dt) > 0 else "Active"
        print(f"  Order: {ord_id:>6} | Qty: {qty:>4} | Invoice: {inv_status:>20} | Status: {can_status}")
    
    if not all_rows:
        print(f"  No orders found for {test_sku} sold to ROSS")
    
    # Now show FILTERED orders (pending/active only)
    print("\n2. FILTERED ORDERS for N1724 BK sold to ROSS (Pending/Active only):")
    print("-" * 80)
    
    filtered_query = """
        SELECT
            UPPER(LTRIM(RTRIM(ol.prod_cd))) AS sku,
            LTRIM(RTRIM(c.cus_nm)) AS customer_name,
            CAST(ol.ord_num AS VARCHAR(30)) AS order_id,
            CAST(ISNULL(ol.order_qty, 0) AS INT) AS order_qty,
            o.INVS_NUM as invoice_num,
            o.can_dt as cancel_date
        FROM dbo.ord_log ol
        JOIN dbo.customer c ON ol.cus_id = c.cus_id
        JOIN dbo.orders o ON ol.ord_num = o.ord_num
        WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) = ?
          AND (
                UPPER(c.cus_nm) LIKE '%ROSS%'
                OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACTORY%'
                OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACOTORY%'
          )
          AND (
                (o.INVS_NUM IS NULL OR CAST(o.INVS_NUM AS INT) = 0)
                OR (o.can_dt IS NULL OR CAST(o.can_dt AS INT) = 0)
          )
        ORDER BY ol.ord_num DESC
    """
    
    cursor.execute(filtered_query, (test_sku,))
    filtered_rows = cursor.fetchall()
    
    filtered_order_ids = []
    for row in filtered_rows:
        sku, cust_name, ord_id, qty, inv_num, can_dt = row
        filtered_order_ids.append(ord_id)
        inv_status = f"Invoice #{inv_num}" if inv_num and int(inv_num) > 0 else "NOT INVOICED"
        can_status = f"Cancelled (date: {can_dt})" if can_dt and int(can_dt) > 0 else "Active"
        print(f"  Order: {ord_id:>6} | Qty: {qty:>4} | Invoice: {inv_status:>20} | Status: {can_status}")
    
    if not filtered_rows:
        print(f"  No active/pending orders found for {test_sku} sold to ROSS")
    
    # Summary
    print("\n3. FILTER SUMMARY:")
    print("-" * 80)
    print(f"Total orders (all):      {len(all_rows)}")
    print(f"Filtered orders (active): {len(filtered_rows)}")
    print(f"All order IDs:           {', '.join(all_order_ids)}")
    print(f"Filtered order IDs:      {', '.join(filtered_order_ids)}")
    
    # Verify expected result
    print("\n4. VERIFICATION:")
    print("-" * 80)
    if "35744" in filtered_order_ids:
        print("✅ Order 35744 is included (as expected)")
    else:
        print("❌ Order 35744 is NOT included (UNEXPECTED!)")
    
    excluded = set(all_order_ids) - set(filtered_order_ids)
    if excluded:
        print(f"✅ Orders excluded by filter: {', '.join(sorted(excluded))}")
    else:
        print("⚠️  All orders passed the filter (possible issue)")

    cursor.close()
    conn.close()

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
