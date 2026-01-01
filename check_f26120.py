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
    print("CHECKING PLOG FOR F26120")
    print("=" * 80)
    
    cursor = conn_fdc.cursor()
    
    # Check if F26120 exists in plog
    cursor.execute("SELECT COUNT(*) FROM plog WHERE pur_num = 'F26120'")
    count = cursor.fetchone()[0]
    print(f"\nTotal records in plog for F26120: {count}")
    
    if count > 0:
        # Get sample records
        cursor.execute("SELECT TOP 5 * FROM plog WHERE pur_num = 'F26120'")
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        
        print(f"\nColumns: {columns}")
        print(f"\nSample records:")
        for idx, row in enumerate(rows):
            row_dict = dict(zip(columns, row))
            print(f"\nRow {idx + 1}:")
            for key, val in row_dict.items():
                print(f"  {key}: {val}")
    else:
        # Check if there's similar PO numbers
        cursor.execute("""
            SELECT DISTINCT TOP 10 pur_num 
            FROM plog 
            WHERE pur_num LIKE 'F261%'
            ORDER BY pur_num DESC
        """)
        similar = cursor.fetchall()
        print(f"\nSimilar container numbers found:")
        for row in similar:
            print(f"  {row[0]}")
    
    # Check if E_FAS database has it
    print("\n" + "=" * 80)
    print("CHECKING E_FAS FOR F26120")
    print("=" * 80)
    
    conn_oms_ef = pyodbc.connect(
        f'DRIVER={config["driver"]};SERVER={config["server"]};DATABASE={config["database_oms_ef"]};UID={config["username"]};PWD={config["password"]}'
    )
    
    cursor2 = conn_oms_ef.cursor()
    cursor2.execute("SELECT COUNT(*) FROM plog WHERE pur_num = 'F26120'")
    count2 = cursor2.fetchone()[0]
    print(f"\nTotal records in E_FAS plog for F26120: {count2}")
    
    if count2 > 0:
        cursor2.execute("SELECT TOP 5 * FROM plog WHERE pur_num = 'F26120'")
        columns = [column[0] for column in cursor2.description]
        rows = cursor2.fetchall()
        
        print(f"\nColumns: {columns}")
        print(f"\nSample records:")
        for idx, row in enumerate(rows):
            row_dict = dict(zip(columns, row))
            print(f"\nRow {idx + 1}:")
            for key, val in row_dict.items():
                print(f"  {key}: {val}")
    
    cursor.close()
    cursor2.close()
    conn_fdc.close()
    conn_oms_ef.close()
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
