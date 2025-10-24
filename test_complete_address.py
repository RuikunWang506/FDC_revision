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

# Get ALL address-related fields from both orders and customer tables
query = """
    SELECT 
        o.ord_num,
        o.CUS_ID,
        -- Orders table shipping address
        o.SP_ADR as ship_address_line1,
        o.SP_ADR_2 as ship_address_line2,
        o.SP_ADR_3 as ship_address_line3,
        o.SP_ADR_CT as ship_city,
        o.SP_ADR_ST as ship_state,
        o.SP_ADR_ZP as ship_zip,
        o.SP_ADR_CN as ship_country,
        o.SP_CUS_NM as ship_customer_name,
        o.ATTN as attention,
        -- Customer table shipping address
        c.SHP_ADDRESS as cus_ship_address,
        c.SHP_ADDRESS2 as cus_ship_address2,
        c.SHP_CITY as cus_ship_city,
        c.SHP_STATE as cus_ship_state,
        c.SHP_ZIP as cus_ship_zip,
        c.SHP_COUNTRY as cus_ship_country,
        c.SHP_CUS_NM as cus_ship_name,
        c.ADDRESS as cus_address,
        c.ADDRESS2 as cus_address2,
        c.CITY as cus_city,
        c.STATE as cus_state,
        c.ZIP as cus_zip
    FROM orders o
    LEFT JOIN CUSTOMER c ON o.CUS_ID = c.CUS_ID
    WHERE o.ord_num = ?
"""

try:
    df = pd.read_sql_query(query, conn, params=(test_order,))
    
    if df.empty:
        print(f"No data found for order {test_order}")
    else:
        print(f"\n{'='*80}")
        print(f"Complete Address Information for Order: {test_order}")
        print(f"{'='*80}\n")
        
        print("ORDER TABLE SHIPPING ADDRESS:")
        print(f"  Name: {df.iloc[0]['ship_customer_name']}")
        print(f"  Address 1: {df.iloc[0]['ship_address_line1']}")
        print(f"  Address 2: {df.iloc[0]['ship_address_line2']}")
        print(f"  Address 3: {df.iloc[0]['ship_address_line3']}")
        print(f"  City: {df.iloc[0]['ship_city']}")
        print(f"  State: {df.iloc[0]['ship_state']}")
        print(f"  ZIP: {df.iloc[0]['ship_zip']}")
        print(f"  Country: {df.iloc[0]['ship_country']}")
        print(f"  Attention: {df.iloc[0]['attention']}")
        
        print("\nCUSTOMER TABLE SHIPPING ADDRESS:")
        print(f"  Name: {df.iloc[0]['cus_ship_name']}")
        print(f"  Address 1: {df.iloc[0]['cus_ship_address']}")
        print(f"  Address 2: {df.iloc[0]['cus_ship_address2']}")
        print(f"  City: {df.iloc[0]['cus_ship_city']}")
        print(f"  State: {df.iloc[0]['cus_ship_state']}")
        print(f"  ZIP: {df.iloc[0]['cus_ship_zip']}")
        print(f"  Country: {df.iloc[0]['cus_ship_country']}")
        
        print("\nCUSTOMER TABLE BILLING ADDRESS:")
        print(f"  Address 1: {df.iloc[0]['cus_address']}")
        print(f"  Address 2: {df.iloc[0]['cus_address2']}")
        print(f"  City: {df.iloc[0]['cus_city']}")
        print(f"  State: {df.iloc[0]['cus_state']}")
        print(f"  ZIP: {df.iloc[0]['cus_zip']}")
    
    conn.close()
    
except Exception as e:
    print(f"Error: {str(e)}")
    import traceback
    traceback.print_exc()
    conn.close()
