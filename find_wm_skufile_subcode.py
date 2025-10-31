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

# Print all column names and values for N1810 BK in wm_skufile
cursor.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'wm_skufile'")
columns = [col[0] for col in cursor.fetchall()]
print('wm_skufile columns:')
print(columns)

cursor.execute("SELECT * FROM wm_skufile WHERE prod_cd = ?", ('N1810 BK',))
rows = cursor.fetchall()
for row in rows:
    print(dict(zip(columns, row)))

conn.close()
