import pyodbc
from py.utilities.setting import Config

# Test the get_latest_order_ids function
sql_server_config = Config.SQL_SERVER_CONFIG

def get_latest_order_ids():
    """Get the latest order IDs from both FDC and E-Fashion databases"""
    latest_orders = {'fdc': None, 'ef': None}
    
    databases = {
        'fdc': 'omsdata_FDC',
        'ef': 'oms_EF'
    }
    
    for db_key, database_name in databases.items():
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            
            # Get the maximum order number from the orders table
            cursor.execute("SELECT MAX(ord_num) FROM orders")
            result = cursor.fetchone()
            
            if result and result[0]:
                latest_orders[db_key] = result[0]
            
            cursor.close()
            conn.close()
            
        except Exception as e:
            print(f"Error getting latest order ID from {database_name}: {str(e)}")
            latest_orders[db_key] = "Error"
    
    return latest_orders

# Test the function
print("Testing get_latest_order_ids function...")
latest = get_latest_order_ids()
print(f"Latest FDC Order ID: {latest['fdc']}")
print(f"Latest E-Fashion Order ID: {latest['ef']}")
print("Test completed.")