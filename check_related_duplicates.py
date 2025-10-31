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

tables = [
    ('inv_data', 'Prod_cd'),
    ('inv_detl', 'prod_cd'),
    ('wm_skufile', 'prod_cd'),
]

sku = 'N1810 BK'

for table, col in tables:
    print(f"Checking duplicates in {table}...")
    cursor.execute(f"SELECT {col}, COUNT(*) AS count FROM {table} WHERE {col} = ? GROUP BY {col} HAVING COUNT(*) > 1", (sku,))
    rows = cursor.fetchall()
    if rows:
        for row in rows:
            print(f"Duplicate found: {row}")
    else:
        print("No duplicates found.")
    print(f"All rows for {sku} in {table}:")
    cursor.execute(f"SELECT * FROM {table} WHERE {col} = ?", (sku,))
    all_rows = cursor.fetchall()
    for r in all_rows:
        print(r)
    print("-"*40)

conn.close()
