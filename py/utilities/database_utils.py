import os
import time

import mysql.connector
from mysql.connector import Error, pooling
from mysql.connector.errors import PoolError
import pyodbc

from py.utilities.setting import Config

_mysql_pool = None


def _mysql_pool_size() -> int:
    raw = (os.getenv("MYSQL_POOL_SIZE") or "8").strip()
    try:
        return max(1, min(int(raw), 32))
    except ValueError:
        return 8


def _get_mysql_pool():
    global _mysql_pool
    if _mysql_pool is None:
        cfg = dict(Config.DB_CONFIG)
        _mysql_pool = pooling.MySQLConnectionPool(
            pool_name="fdc_mysql",
            pool_size=_mysql_pool_size(),
            pool_reset_session=True,
            **cfg,
        )
    return _mysql_pool


def get_mysql_connection(retries=3):
    """Pooled MySQL connection. close() returns it to the pool.

    Pool checkout is retried briefly — the 8-slot pool is often busy, not down.
    Other MySQL errors still return None immediately. Do not open a direct
    connection as a fallback; that bypasses the pool cap during exhaustion.
    """
    last_error = None
    attempts = max(1, int(retries or 1))
    for attempt in range(attempts):
        try:
            connection = _get_mysql_pool().get_connection()
            if connection is not None and connection.is_connected():
                return connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        except PoolError as e:
            last_error = e
        except Error as e:
            print(f"Error connecting to MySQL pool: {e}")
            return None
        if attempt + 1 < attempts:
            time.sleep(0.08 * (2 ** attempt))
    if last_error is not None:
        print(f"Error connecting to MySQL pool: {last_error}")
    return None


def get_db_connection():
    """Alias used by auth and log viewer."""
    return get_mysql_connection()


def _sql_server_conn_str(database_name: str, *, readonly: bool = False) -> str:
    cfg = Config.SQL_SERVER_CONFIG
    parts = [
        f"DRIVER={cfg['driver']}",
        f"SERVER={cfg['server']}",
        f"DATABASE={database_name}",
        f"UID={cfg['username']}",
        f"PWD={cfg['password']}",
    ]
    if readonly:
        parts.append("ApplicationIntent=ReadOnly")
    return ";".join(parts)


def get_sql_server_connection_fdc():
    return pyodbc.connect(_sql_server_conn_str(Config.SQL_SERVER_CONFIG["database_fdc"]))


def get_sql_server_connection_oms_ef():
    return pyodbc.connect(_sql_server_conn_str(Config.SQL_SERVER_CONFIG["database_oms_ef"]))


def get_sql_server_connection(database_name):
    return pyodbc.connect(_sql_server_conn_str(database_name))


def get_sql_server_connection_readonly(database_name):
    return pyodbc.connect(_sql_server_conn_str(database_name, readonly=True))
