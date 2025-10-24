import pyodbc
from py.utilities.setting import Config

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

def check_table_columns(database_name, table_name):
    """Check what columns are available in a table"""
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Get column names
        query = f"SELECT TOP 1 * FROM {table_name}"
        cursor.execute(query)
        
        columns = [column[0] for column in cursor.description]
        
        print(f"\n{'='*80}")
        print(f"Database: {database_name}")
        print(f"Table: {table_name}")
        print(f"{'='*80}")
        print(f"Available columns ({len(columns)}):")
        for i, col in enumerate(columns, 1):
            print(f"{i:3d}. {col}")
        
        cursor.close()
        conn.close()
        
        return columns
        
    except Exception as e:
        print(f"Error checking {database_name}.{table_name}: {str(e)}")
        return []

# Check both databases
databases = ['omsdata_FDC', 'oms_EF']

for db in databases:
    print("\n" + "="*80)
    print(f"CHECKING DATABASE: {db}")
    print("="*80)
    
    # Check orders table
    check_table_columns(db, 'orders')
    
    # Check ORD_LOG table
    check_table_columns(db, 'ORD_LOG')
    
    # Check CUSTOMER table
    check_table_columns(db, 'CUSTOMER')
