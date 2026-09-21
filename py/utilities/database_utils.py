import os
import threading
import time

import mysql.connector
from mysql.connector import Error, pooling
from mysql.connector.errors import PoolError
import pyodbc

from py.utilities.setting import Config

_mysql_pool = None
_pool_lock = threading.Lock()
_pool_generation = 0
_pool_resets = 0
_last_reset_at = 0.0
_RESET_COOLDOWN_SEC = 30
# Pooled slots are for short web requests. Long jobs (DAM) must use unpooled.
_POOLED_MAX_HOLD_SEC = 300
_checkout_lock = threading.Lock()
_checkouts = {}
_watchdog_started = False
_watchdog_stop = threading.Event()


def _log(level, message):
    try:
        from py.utilities.logging_setup import log_event

        log_event(level, message)
    except Exception:
        print(message)


def _mysql_pool_size() -> int:
    raw = (os.getenv("MYSQL_POOL_SIZE") or "8").strip()
    try:
        return max(1, min(int(raw), 32))
    except ValueError:
        return 8


def _get_mysql_pool():
    global _mysql_pool, _pool_generation
    with _pool_lock:
        if _mysql_pool is None:
            cfg = dict(Config.DB_CONFIG)
            _mysql_pool = pooling.MySQLConnectionPool(
                pool_name=f"fdc_mysql_{_pool_generation}",
                pool_size=_mysql_pool_size(),
                pool_reset_session=True,
                connection_timeout=8,
                **cfg,
            )
        return _mysql_pool


def mysql_pool_stats() -> dict:
    """Snapshot for /health and logs. checked_out is this process, not MySQL server."""
    now = time.time()
    with _checkout_lock:
        held = [
            {
                "age_sec": round(now - meta["t"], 1),
                "thread": meta["thread"],
            }
            for meta in _checkouts.values()
        ]
    return {
        "size": _mysql_pool_size(),
        "generation": _pool_generation,
        "resets": _pool_resets,
        "checked_out": len(held),
        "oldest_hold_sec": max((h["age_sec"] for h in held), default=0),
        "holds": held,
    }


def reset_mysql_pool(reason: str = "manual") -> bool:
    """Drop the in-process pool so the next checkout builds a fresh one.

    Checked-out connections from the old pool are not reused. Callers that still
    hold them should close(); stale ones are force-returned by cleanup_mysql_pool.
    """
    global _mysql_pool, _pool_generation, _pool_resets, _last_reset_at
    now = time.time()
    with _pool_lock:
        if now - _last_reset_at < _RESET_COOLDOWN_SEC:
            return False
        _mysql_pool = None
        _pool_generation += 1
        _pool_resets += 1
        _last_reset_at = now
        generation = _pool_generation
        resets = _pool_resets
    _log("warning", f"MySQL pool reset generation={generation} resets={resets} reason={reason}")
    return True


def _track_checkout(conn):
    key = id(conn)
    meta = {
        "conn": conn,
        "t": time.time(),
        "thread": threading.current_thread().name,
    }
    with _checkout_lock:
        _checkouts[key] = meta
    if getattr(conn, "_fdc_close_wrapped", False):
        return conn
    orig_close = conn.close

    def _wrapped_close(*args, **kwargs):
        with _checkout_lock:
            _checkouts.pop(key, None)
        try:
            conn._fdc_close_wrapped = False
        except Exception:
            pass
        return orig_close(*args, **kwargs)

    conn.close = _wrapped_close
    conn._fdc_close_wrapped = True
    return conn


def _force_release(meta, *, why: str) -> None:
    conn = meta.get("conn")
    age = time.time() - meta.get("t", time.time())
    thread = meta.get("thread") or "?"
    _log(
        "warning",
        f"MySQL pool force-release age={age:.1f}s thread={thread} why={why}",
    )
    if conn is not None:
        with _checkout_lock:
            _checkouts.pop(id(conn), None)
        try:
            conn.close()
        except Exception as e:
            _log("warning", f"MySQL pool force-release close failed: {e}")


def cleanup_mysql_pool(*, max_hold_sec: int | None = None) -> dict:
    """Return leaked pooled connections and rebuild the pool if it is stuck."""
    limit = _POOLED_MAX_HOLD_SEC if max_hold_sec is None else max(1, int(max_hold_sec))
    now = time.time()
    stale = []
    with _checkout_lock:
        for meta in list(_checkouts.values()):
            if now - meta.get("t", now) >= limit:
                stale.append(meta)
    for meta in stale:
        _force_release(meta, why=f"held>{limit}s")

    stats = mysql_pool_stats()
    if stats["checked_out"] >= stats["size"]:
        reset_mysql_pool("watchdog_exhausted")
        stats = mysql_pool_stats()
    return stats


def start_mysql_pool_watchdog(*, interval_sec: int = 30) -> None:
    """Background cleanup so a leaked slot cannot require an app restart."""
    global _watchdog_started
    wait = max(10, int(interval_sec or 30))
    with _pool_lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    def _loop():
        while not _watchdog_stop.wait(wait):
            try:
                cleanup_mysql_pool()
            except Exception as e:
                _log("warning", f"MySQL pool watchdog error: {e}")

    threading.Thread(target=_loop, daemon=True, name="fdc-mysql-pool-cleanup").start()
    _log("info", f"MySQL pool watchdog started interval={wait}s max_hold={_POOLED_MAX_HOLD_SEC}s")


def get_mysql_connection(retries=3):
    """Pooled MySQL connection. close() returns it to the pool.

    Pool checkout is retried briefly — the 8-slot pool is often busy, not down.
    After retries fail, the pool is rebuilt once so a leak does not stick until
    process restart. Long jobs should use get_mysql_connection_unpooled().
    """
    last_error = None
    attempts = max(1, int(retries or 1))
    for attempt in range(attempts):
        try:
            connection = _get_mysql_pool().get_connection()
            if connection is not None and connection.is_connected():
                return _track_checkout(connection)
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
        if reset_mysql_pool("exhausted"):
            try:
                connection = _get_mysql_pool().get_connection()
                if connection is not None and connection.is_connected():
                    return _track_checkout(connection)
            except Exception as e:
                print(f"Error connecting to MySQL pool after reset: {e}")
    return None


def close_mysql_connection(connection):
    """Return a pooled/unpooled connection without calling is_connected().

    mysql.connector pooled wrappers set _cnx=None after close(); a later
    is_connected() lookup then raises AttributeError and can replace a
    successful response with HTTP 500.
    """
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        pass


def get_mysql_connection_unpooled():
    """Short-lived connection that does not consume the app pool.

    Login and long DAM scans use this so they cannot starve request handlers.
    """
    cfg = dict(Config.DB_CONFIG)
    try:
        connection = mysql.connector.connect(connection_timeout=8, **cfg)
        if connection is not None and connection.is_connected():
            return connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    except Error as e:
        print(f"Error connecting to MySQL (unpooled): {e}")
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
