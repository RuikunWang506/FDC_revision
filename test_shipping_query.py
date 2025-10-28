import pyodbc
import pandas as pd
from py.utilities.setting import Config

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

# Test with a recent order number
test_order = input("Enter a test order number (4 or 5 digits): ").strip()

if len(test_order) == 5:
    database_name = 'omsdata_FDC'
elif len(test_order) == 4:
    database_name = 'oms_EF'
else:
    print("Invalid input length. Please provide a 4 or 5-digit input.")
    exit()

conn = pyodbc.connect(
    f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
)

query = """\
   WITH OrderNum AS (
        SELECT ? AS ord_num
    ),
    OrderInfo AS (
        SELECT O.ORD_NUM, O.CUS_ID, O.COMM_LN, O.UT_DESC, O.PROD_CD, O.ORDER_QTY, O.UNIT_PRS, O.PC_UNIT, C.CUS_NM
        FROM DBO.ORD_LOG O
        INNER JOIN DBO.CUSTOMER C ON O.CUS_ID = C.CUS_ID
        WHERE O.ORD_NUM = (SELECT ord_num FROM OrderNum)
    ),
    OrderDates AS (
        SELECT 
            ord_num,
            ord_dt AS order_date,
            ship_dt AS ship_date,
            can_dt AS cancel_date,
            PO_NUM,
            ORD_AMT,
            PAID_AMT,
            TAXABLE_AMT,
            INVS_TAX,
            HANDL_FEE,
            SHIP_CHG,
            DISCOUNT,
            MISC_CHG,
            SALES_NUM,
            SALES_NUM2,
            TERM_DESC,
            CLOSE_CD,
            ORD_TYPE,
            CUS_SHIP_CHG,
            DISC_AMT,
            STATUS,
            SHIP_DESC,
            TRK_NUM,
            SP_ADR_CT,
            SP_ADR_ST,
            SP_ADR_ZP,
            ATTN,
            EMAIL_ADR
        FROM dbo.orders
        WHERE ord_num = (SELECT ord_num FROM OrderNum)
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
    )
    SELECT TOP 1
        OD.STATUS AS order_status,
        CI.PRIORITY AS priority,
        OD.SHIP_DESC AS ship_method,
        OD.TRK_NUM AS tracking_number,
        COALESCE(CI.SHP_CITY, OD.SP_ADR_CT) AS ship_to_city,
        COALESCE(CI.SHP_STATE, OD.SP_ADR_ST) AS ship_to_state,
        COALESCE(CI.SHP_ZIP, OD.SP_ADR_ZP) AS ship_to_zip,
        CI.PHONE AS phone,
        OD.ATTN AS attention,
        OD.EMAIL_ADR AS email
    FROM OrderInfo O
    INNER JOIN OrderDates OD ON O.ORD_NUM = OD.ord_num
    LEFT JOIN CustomerInfo CI ON O.CUS_ID = CI.CUS_ID;
"""

try:
    df = pd.read_sql_query(query, conn, params=(test_order,))
    
    if df.empty:
        print(f"No data found for order {test_order}")
    else:
        print(f"\n{'='*80}")
        print(f"Shipping & Status Information for Order: {test_order}")
        print(f"{'='*80}\n")
        
        for col in df.columns:
            value = df.iloc[0][col]
            if pd.notna(value) and str(value).strip():
                print(f"{col:20s}: {value}")
            else:
                print(f"{col:20s}: N/A")
    
    conn.close()
    
except Exception as e:
    print(f"Error: {str(e)}")
    conn.close()
