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

sku = 'N1810 BK'

# Get all rows for N1810 BK in wm_skufile, ordered by a likely date or id column
cursor.execute("SELECT * FROM wm_skufile WHERE prod_cd = ? ORDER BY prod_cd, [CREATE_DT] DESC", (sku,))
rows = cursor.fetchall()
for row in rows:
    print(row)

conn.close()
