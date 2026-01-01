#!/usr/bin/env python
import sys
sys.path.insert(0, r'c:\FDC_FLASK_APP')

from py.utilities.setting import Config
import pyodbc

config = Config.SQL_SERVER_CONFIG

try:
    # Connect to FDC
    conn_fdc = pyodbc.connect(
        f'DRIVER={config["driver"]};SERVER={config["server"]};DATABASE={config["database_fdc"]};UID={config["username"]};PWD={config["password"]}'
    )
    
    print("=" * 80)
    print("PLOG TABLE DATA FOR F24237")
    print("=" * 80)
    
    # Query plog table for F24237
    cursor = conn_fdc.cursor()
    cursor.execute("SELECT TOP 5 * FROM plog WHERE pur_num = 'F24237'")
    
    # Get column names
    columns = [column[0] for column in cursor.description]
    print("\nPLOG Table Headers:")
    for i, col in enumerate(columns):
        print(f"  {i}: {col}")
    
    rows = cursor.fetchall()
    print(f"\nTotal records found: {len(rows)}")
    print("\nSample data:")
    for idx, row in enumerate(rows):
        print(f"\nRow {idx + 1}:")
        row_dict = dict(zip(columns, row))
        for key, val in row_dict.items():
            print(f"  {key}: {val}")
    
    # Get first SKU for inv table check
    if rows:
        sku = rows[0][6]  # Column 6 is PROD_CD
        print("\n" + "=" * 80)
        print(f"INV TABLE DATA FOR SKU: {sku}")
        print("=" * 80)
        
        cursor.execute("""
            SELECT PROD_CD, UNIT_COLOR, DESCRIP, SALES_COST, RETAIL_PRS, ALT_CD
            FROM dbo.inv 
            WHERE PROD_CD = ?
        """, sku)
        
        inv_columns = [column[0] for column in cursor.description]
        print("\nINV Table Headers:")
        for i, col in enumerate(inv_columns):
            print(f"  {i}: {col}")
        
        inv_rows = cursor.fetchall()
        print(f"\nTotal records found: {len(inv_rows)}")
        print("\nSample data:")
        for idx, row in enumerate(inv_rows):
            print(f"\nRow {idx + 1}:")
            row_dict = dict(zip(inv_columns, row))
            for key, val in row_dict.items():
                print(f"  {key}: {val}")
    
    cursor.close()
    conn_fdc.close()
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
