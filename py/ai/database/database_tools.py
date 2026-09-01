"""Read-only SQL helpers. The LLM never opens a database connection."""
from __future__ import annotations

import os
from typing import Any

from py.ai.sql_validator import validate_sql

_DEFAULT_TIMEOUT = 30


def _timeout() -> int:
    try:
        return max(5, min(int(os.getenv('FDC_AI_SQL_TIMEOUT', str(_DEFAULT_TIMEOUT)) or _DEFAULT_TIMEOUT), 120))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _database_name() -> str:
    from py.utilities.setting import Config

    return os.getenv('FDC_AI_SQL_DATABASE') or Config.SQL_SERVER_CONFIG['database_fdc']


def _connect():
    from py.utilities.database_utils import get_sql_server_connection_readonly

    conn = get_sql_server_connection_readonly(_database_name())
    conn.timeout = _timeout()
    return conn


def get_database_schema() -> dict[str, Any]:
    from py.ai.database.schema_scanner import load_catalog

    return load_catalog()


def get_table_schema(table_name: str) -> dict[str, Any] | None:
    catalog = get_database_schema()
    tables = catalog.get('tables') or {}
    key = (table_name or '').strip()
    if key in tables:
        return tables[key]
    low = key.lower()
    for name, spec in tables.items():
        if name.lower() == low or name.lower().endswith('.' + low):
            return spec
    return None


def get_sample_rows(table_name: str, limit: int = 5) -> list[dict[str, Any]]:
    spec = get_table_schema(table_name)
    if spec and spec.get('samples'):
        return list(spec.get('samples') or [])[: max(1, min(int(limit), 5))]
    safe_name = re_table(table_name)
    if not safe_name:
        return []
    sql = f'SELECT TOP {max(1, min(int(limit), 5))} * FROM dbo.[{safe_name}]'
    result = execute_readonly_query(sql)
    return list(result.get('rows') or [])


def re_table(name: str) -> str:
    text = (name or '').strip().split('.')[-1]
    if not text or not all(ch.isalnum() or ch == '_' for ch in text):
        return ''
    return text


def execute_readonly_query(sql: str) -> dict[str, Any]:
    checked = validate_sql(sql)
    if not checked.get('ok'):
        return {
            'ok': False,
            'error': checked.get('error') or 'SQL rejected',
            'sql': sql,
            'rows': [],
            'columns': [],
            'row_count': 0,
        }
    safe_sql = checked['sql']
    conn = None
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute(safe_sql)
        columns = [str(col[0]) for col in (cur.description or [])]
        cap = int(checked.get('max_rows') or 500)
        fetched = cur.fetchmany(cap)
        rows = []
        for raw in fetched:
            item = {}
            for idx, col in enumerate(columns):
                value = raw[idx]
                if hasattr(value, 'isoformat'):
                    value = value.isoformat(sep=' ', timespec='seconds')
                elif isinstance(value, bytes):
                    value = value[:80].hex()
                elif value is not None and not isinstance(value, (str, int, float, bool)):
                    value = str(value)
                item[col] = value
            rows.append(item)
        return {
            'ok': True,
            'sql': safe_sql,
            'columns': columns,
            'rows': rows,
            'row_count': len(rows),
            'truncated': len(fetched) >= cap,
            'error': None,
        }
    except Exception as exc:
        return {
            'ok': False,
            'error': str(exc),
            'sql': safe_sql,
            'rows': [],
            'columns': [],
            'row_count': 0,
        }
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
