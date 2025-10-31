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

# List all columns in the 'inv' table
cursor.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'inv'")
columns = cursor.fetchall()
print('Columns in inv table:')
for col in columns:
    print(col[0])

conn.close()
