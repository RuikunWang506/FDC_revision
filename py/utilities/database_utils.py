import mysql.connector
from mysql.connector import Error
import pyodbc
from py.utilities.setting import Config

def get_mysql_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**Config.DB_CONFIG)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def get_sql_server_connection_fdc():
    """Establishes a connection to the SQL Server FDC database."""
    return pyodbc.connect(
        f'DRIVER={Config.SQL_SERVER_CONFIG["driver"]};'
        f'SERVER={Config.SQL_SERVER_CONFIG["server"]};'
        f'DATABASE={Config.SQL_SERVER_CONFIG["database_fdc"]};'
        f'UID={Config.SQL_SERVER_CONFIG["username"]};'
        f'PWD={Config.SQL_SERVER_CONFIG["password"]}'
    )

def get_sql_server_connection_oms_ef():
    """Establishes a connection to the SQL Server OMS_EF database."""
    return pyodbc.connect(
        f'DRIVER={Config.SQL_SERVER_CONFIG["driver"]};'
        f'SERVER={Config.SQL_SERVER_CONFIG["server"]};'
        f'DATABASE={Config.SQL_SERVER_CONFIG["database_oms_ef"]};'
        f'UID={Config.SQL_SERVER_CONFIG["username"]};'
        f'PWD={Config.SQL_SERVER_CONFIG["password"]}'
    )

def get_sql_server_connection(database_name):
    """Establishes a connection to the specified SQL Server database."""
    return pyodbc.connect(
        f'DRIVER={Config.SQL_SERVER_CONFIG["driver"]};'
        f'SERVER={Config.SQL_SERVER_CONFIG["server"]};'
        f'DATABASE={database_name};'
        f'UID={Config.SQL_SERVER_CONFIG["username"]};'
        f'PWD={Config.SQL_SERVER_CONFIG["password"]}'
    )