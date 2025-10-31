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

# List all tables in the database
cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
tables = cursor.fetchall()
print('Tables in omsdata_FDC:')
for table in tables:
    print(table[0])

conn.close()
