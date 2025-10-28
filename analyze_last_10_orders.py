import pyodbc
import pandas as pd
from py.utilities.setting import Config

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

database_name = 'oms_EF'

conn = pyodbc.connect(
    f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
)

# Get all columns from orders table for the last 10 orders
query = """
    SELECT TOP 10 *
    FROM orders
    WHERE ord_num >= 1935 AND ord_num <= 1944
    ORDER BY ord_num DESC
"""

try:
    df = pd.read_sql_query(query, conn)
    
    print(f"\n{'='*100}")
    print(f"ANALYZING LAST 10 ORDERS (1935-1944)")
    print(f"{'='*100}\n")
    
    # Analyze each column
    print(f"Found {len(df)} orders\n")
    print(f"{'='*100}")
    print(f"COLUMN ANALYSIS - Looking for columns with useful data")
    print(f"{'='*100}\n")
    
    useful_columns = []
    empty_columns = []
    
    for col in df.columns:
        # Count non-null and non-empty values
        non_null_count = df[col].notna().sum()
        non_empty_count = 0
        
        for val in df[col]:
            if pd.notna(val):
                if isinstance(val, str):
                    if val.strip():
                        non_empty_count += 1
                else:
                    non_empty_count += 1
        
        # Get unique non-empty values
        unique_vals = df[col].dropna().unique()
        if len(unique_vals) > 0:
            # Filter out empty strings
            unique_vals = [v for v in unique_vals if (isinstance(v, str) and v.strip()) or not isinstance(v, str)]
        
        status = ""
        if non_empty_count == 0:
            status = "❌ EMPTY"
            empty_columns.append(col)
        elif non_empty_count > 0:
            status = f"✓ HAS DATA ({non_empty_count}/{len(df)} orders)"
            useful_columns.append({
                'column': col,
                'count': non_empty_count,
                'unique_count': len(unique_vals),
                'sample_values': list(unique_vals[:3])
            })
        
        if non_empty_count > 0:
            print(f"{col:30s} {status}")
            if len(unique_vals) <= 10:
                print(f"{'':30s} Values: {list(unique_vals)}")
            else:
                print(f"{'':30s} Sample: {list(unique_vals[:5])}...")
            print()
    
    print(f"\n{'='*100}")
    print(f"COLUMNS CURRENTLY USED IN ORDER LOOKUP")
    print(f"{'='*100}\n")
    
    used_columns = [
        'ORD_NUM', 'CUS_ID', 'ORD_DT', 'SHIP_DT', 'CAN_DT', 'PO_NUM',
        'ORD_AMT', 'PAID_AMT', 'TAXABLE_AMT', 'INVS_TAX', 'HANDL_FEE',
        'SHIP_CHG', 'DISCOUNT', 'MISC_CHG', 'SALES_NUM', 'SALES_NUM2',
        'TERM_DESC', 'CLOSE_CD', 'ORD_TYPE', 'CUS_SHIP_CHG', 'DISC_AMT',
        'STATUS', 'SHIP_DESC', 'TRK_NUM', 'SP_ADR', 'SP_ADR_2', 'SP_ADR_3',
        'SP_ADR_CT', 'SP_ADR_ST', 'SP_ADR_ZP', 'SP_ADR_CN', 'SP_CUS_NM',
        'ATTN', 'EMAIL_ADR'
    ]
    
    for col in used_columns:
        print(f"✓ {col}")
    
    print(f"\n{'='*100}")
    print(f"POTENTIALLY USEFUL COLUMNS NOT CURRENTLY DISPLAYED")
    print(f"{'='*100}\n")
    
    for item in useful_columns:
        if item['column'] not in used_columns:
            print(f"\n{item['column']:30s} - Found in {item['count']}/{len(df)} orders")
            print(f"{'':30s}   Unique values: {item['unique_count']}")
            print(f"{'':30s}   Sample: {item['sample_values']}")
    
    print(f"\n{'='*100}")
    print(f"EMPTY/UNUSED COLUMNS (All zeros or nulls)")
    print(f"{'='*100}\n")
    
    for col in empty_columns:
        print(f"❌ {col}")
    
    conn.close()
    
except Exception as e:
    print(f"Error: {str(e)}")
    import traceback
    traceback.print_exc()
    conn.close()
