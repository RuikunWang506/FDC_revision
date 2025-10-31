import pyodbc

sql_server_config = {
    'server': '192.168.254.204',
    'username': 'tristen',
    'password': 'Fdc2022!!',
    'driver': 'ODBC Driver 17 for SQL Server',
    'database_fdc': 'omsdata_FDC'
}

conn = pyodbc.connect(
    f"DRIVER={sql_server_config['driver']};SERVER={sql_server_config['server']};DATABASE={sql_server_config['database_fdc']};UID={sql_server_config['username']};PWD={sql_server_config['password']}"
)
cursor = conn.cursor()


# Check for exact duplicates for N1810 BK using actual column names
cursor.execute("""
    SELECT 
        PROD_CD, UNIT_COLOR, DESCRIP, SALES_COST, RETAIL_PRS, PC_CASE, CASE_WT, UT_WT, LENGTH, WIDTH, HEIGHT, CREATE_DT, UPDT_DT, SO_QTY, PO_QTY, WT_QTY, PK_QTY, WIP_QTY, RMA_QTY, COUNT(*) as duplicate_count
    FROM inv
    WHERE PROD_CD = 'N1810 BK'
    GROUP BY 
        PROD_CD, UNIT_COLOR, DESCRIP, SALES_COST, RETAIL_PRS, PC_CASE, CASE_WT, UT_WT, LENGTH, WIDTH, HEIGHT, CREATE_DT, UPDT_DT, SO_QTY, PO_QTY, WT_QTY, PK_QTY, WIP_QTY, RMA_QTY
    HAVING COUNT(*) > 1
""")
all_rows = cursor.fetchall()
for row in all_rows:
    print(row)

conn.close()