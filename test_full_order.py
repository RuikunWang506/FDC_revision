import pyodbc
import pandas as pd
from py.utilities.setting import Config

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

test_order = "1942"
database_name = 'oms_EF'

conn = pyodbc.connect(
    f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
)

# Get ALL order info including cancel date
query = """
    SELECT TOP 1
        ord_num,
        ord_dt,
        ship_dt,
        can_dt,
        CUS_ID,
        PO_NUM,
        SALES_NUM,
        ORD_AMT,
        PAID_AMT,
        STATUS,
        SHIP_DESC,
        TRK_NUM,
        SP_ADR_CT,
        SP_ADR_ST,
        SP_ADR_ZP,
        ATTN,
        EMAIL_ADR,
        CLOSE_CD,
        ORD_TYPE
    FROM orders
    WHERE ord_num = ?
"""

try:
    df = pd.read_sql_query(query, conn, params=(test_order,))
    
    if df.empty:
        print(f"No data found for order {test_order}")
    else:
        print(f"\n{'='*80}")
        print(f"Full Order Information for Order: {test_order}")
        print(f"{'='*80}\n")
        
        for col in df.columns:
            value = df.iloc[0][col]
            print(f"{col:20s}: {value} (type: {type(value).__name__})")
        
        # Check cancel date specifically
        can_dt = df.iloc[0]['can_dt']
        print(f"\n{'='*80}")
        print(f"Cancel Date Analysis:")
        print(f"{'='*80}")
        print(f"Raw value: {can_dt}")
        print(f"Type: {type(can_dt)}")
        print(f"Is null: {pd.isna(can_dt)}")
        if pd.notna(can_dt):
            print(f"Integer value: {int(can_dt)}")
            if can_dt > 0:
                from datetime import datetime, timedelta
                # Convert from days since reference date
                base_date = datetime(1900, 1, 1)
                days_offset = int(can_dt) - 81088
                cancel_date = base_date + timedelta(days=days_offset)
                print(f"Calculated date: {cancel_date.strftime('%m/%d/%Y')}")
    
    conn.close()
    
except Exception as e:
    print(f"Error: {str(e)}")
    import traceback
    traceback.print_exc()
    conn.close()
