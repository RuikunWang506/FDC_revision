"""Scan SQL Server + MySQL metadata into schema_catalog.json. Never dump full tables."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_ROOT = Path(__file__).resolve().parents[3]
_OUTPUT = _ROOT / 'output' / 'ai_schema' / 'schema_catalog.json'
_OVERRIDES = Path(__file__).resolve().parent / 'schema_overrides.json'
_SAMPLE_TABLE_CAP = 40
_SAMPLE_ROWS = 4
_PRIORITY = (
    'inv',
    'inv_data',
    'ord_log',
    'orders',
    'customer',
    'invoice',
    'invt_log',
    'plog',
    'bin_pur',
    'pur_ord',
)
_MYSQL_SAMPLE = ('products', 'users', 'help_articles', 'fdc_ai_chats')


def catalog_path() -> Path:
    return _OUTPUT


def load_overrides() -> dict[str, Any]:
    try:
        return json.loads(_OVERRIDES.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _connect_sql(database_name: str):
    from py.utilities.database_utils import get_sql_server_connection_readonly

    conn = get_sql_server_connection_readonly(database_name)
    try:
        conn.timeout = 30
    except Exception:
        pass
    return conn


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'isoformat'):
        return value.isoformat(sep=' ', timespec='seconds')
    if isinstance(value, bytes):
        return value[:40].hex()
    return str(value)


def _apply_overrides(tables: dict[str, Any], overrides: dict[str, Any], *, mysql: bool = False) -> dict[str, Any]:
    source = overrides.get('_mysql') if mysql else overrides
    if not isinstance(source, dict):
        source = {}
    aliases = {} if mysql else (overrides.get('_table_aliases') if isinstance(overrides.get('_table_aliases'), dict) else {})
    for key, value in source.items():
        if str(key).startswith('_'):
            continue
        if '.' in key:
            table, column = key.split('.', 1)
            spec = tables.get(table.lower()) or tables.get(table)
            if not spec:
                continue
            cols = spec.setdefault('columns', {})
            if column in cols or column.lower() in {c.lower() for c in cols}:
                match = next((c for c in cols if c.lower() == column.lower()), column)
                cols[match] = str(value)
        else:
            spec = tables.get(key.lower()) or tables.get(key)
            if spec is not None:
                spec['description'] = str(value)
    for spec in tables.values():
        spec.setdefault('description', spec.get('description') or '')
        spec['aliases'] = []
    lower_map = {name.lower(): name for name in tables}
    for alias, names in aliases.items():
        for name in names or []:
            real = lower_map.get(str(name).lower())
            if real:
                tables[real].setdefault('aliases', []).append(str(alias))
    return tables


def _empty_table(tname: str) -> dict[str, Any]:
    return {
        'name': tname,
        'description': '',
        'columns': {},
        'column_types': {},
        'primary_keys': [],
        'foreign_keys': [],
        'indexes': [],
        'samples': [],
    }


def _sample_sqlserver(cur, tables: dict[str, Any], cap: int) -> None:
    try:
        cur.execute('SET LOCK_TIMEOUT 3000')
    except Exception:
        pass
    skip_types = ('image', 'varbinary', 'binary', 'xml', 'text', 'ntext', 'timestamp')
    want = [t.lower() for t in _PRIORITY if t.lower() in tables]
    extra = [n for n in tables if n not in want][: max(0, cap - len(want))]
    for tkey in want + extra:
        spec = tables[tkey]
        real = spec['name']
        if not all(ch.isalnum() or ch == '_' for ch in real):
            continue
        pick = []
        for cname, ctype in (spec.get('column_types') or {}).items():
            low = str(ctype).lower()
            if any(tok in low for tok in skip_types):
                continue
            if not all(ch.isalnum() or ch == '_' for ch in cname):
                continue
            pick.append(cname)
            if len(pick) >= 20:
                break
        if not pick:
            continue
        quoted = ', '.join(f'[{c}]' for c in pick)
        try:
            cur.execute(f'SELECT TOP {_SAMPLE_ROWS} {quoted} FROM dbo.[{real}] WITH (NOLOCK)')
            cols = [str(c[0]) for c in (cur.description or [])]
            rows = []
            for raw in cur.fetchmany(_SAMPLE_ROWS):
                rows.append({cols[i]: _json_safe(raw[i]) for i in range(len(cols))})
            spec['samples'] = rows
        except Exception:
            spec['samples'] = []


def scan_sqlserver_database(database_name: str, *, sample_rows: bool = True) -> dict[str, Any]:
    overrides = load_overrides()
    conn = _connect_sql(database_name)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS c
            JOIN INFORMATION_SCHEMA.TABLES t
              ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
            WHERE c.TABLE_SCHEMA = 'dbo'
              AND t.TABLE_TYPE = 'BASE TABLE'
            ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
            """
        )
        tables: dict[str, Any] = {}
        for table, column, dtype, nullable in cur.fetchall():
            tname = str(table).strip()
            cname = str(column).strip()
            if not tname or tname.startswith('sys') or tname in ('dtproperties',):
                continue
            spec = tables.setdefault(tname.lower(), _empty_table(tname))
            spec['columns'][cname] = cname
            spec['column_types'][cname] = f'{dtype}{" null" if str(nullable).upper() == "YES" else ""}'

        cur.execute(
            """
            SELECT ku.TABLE_NAME, ku.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS ku
              ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
             AND tc.TABLE_NAME = ku.TABLE_NAME
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
              AND tc.TABLE_SCHEMA = 'dbo'
            """
        )
        for table, column in cur.fetchall():
            spec = tables.get(str(table).strip().lower())
            if spec and column:
                spec['primary_keys'].append(str(column).strip())

        cur.execute(
            """
            SELECT
                OBJECT_NAME(fk.parent_object_id),
                COL_NAME(fc.parent_object_id, fc.parent_column_id),
                OBJECT_NAME(fk.referenced_object_id),
                COL_NAME(fc.referenced_object_id, fc.referenced_column_id)
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fc ON fk.object_id = fc.constraint_object_id
            """
        )
        for child, child_col, parent, parent_col in cur.fetchall():
            spec = tables.get(str(child or '').strip().lower())
            if spec:
                spec['foreign_keys'].append(
                    {
                        'column': str(child_col or '').strip(),
                        'ref_table': str(parent or '').strip(),
                        'ref_column': str(parent_col or '').strip(),
                    }
                )

        cur.execute(
            """
            SELECT t.name, i.name, c.name
            FROM sys.tables t
            JOIN sys.indexes i ON t.object_id = i.object_id
            JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE i.is_hypothetical = 0 AND i.name IS NOT NULL
            ORDER BY t.name, i.name, ic.key_ordinal
            """
        )
        idx_cols: dict[tuple[str, str], list[str]] = {}
        for table, index_name, column in cur.fetchall():
            spec = tables.get(str(table or '').strip().lower())
            if not spec or not index_name:
                continue
            key = (str(table or '').strip().lower(), str(index_name))
            idx_cols.setdefault(key, []).append(str(column or '').strip())
        for (tkey, iname), cols in idx_cols.items():
            spec = tables.get(tkey)
            if spec:
                spec['indexes'].append(f"{iname} ({', '.join(c for c in cols if c)})")

        if sample_rows:
            _sample_sqlserver(cur, tables, _SAMPLE_TABLE_CAP)

        tables = _apply_overrides(tables, overrides, mysql=False)
        return {
            'name': database_name,
            'engine': 'sqlserver',
            'table_count': len(tables),
            'tables': tables,
            'error': None,
        }
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def scan_mysql_database() -> dict[str, Any]:
    from py.utilities.database_utils import get_mysql_connection
    from py.utilities.setting import Config

    schema = Config.DB_CONFIG.get('database') or 'onlinedb'
    overrides = load_overrides()
    conn = get_mysql_connection()
    if not conn:
        return {
            'name': schema,
            'engine': 'mysql',
            'table_count': 0,
            'tables': {},
            'error': 'MySQL unavailable',
        }
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """,
            (schema,),
        )
        tables: dict[str, Any] = {}
        for table, column, dtype, nullable, colkey in cur.fetchall():
            tname = str(table).strip()
            cname = str(column).strip()
            spec = tables.setdefault(tname.lower(), _empty_table(tname))
            spec['columns'][cname] = cname
            spec['column_types'][cname] = f'{dtype}{" null" if str(nullable).upper() == "YES" else ""}'
            if str(colkey or '').upper() == 'PRI' and cname not in spec['primary_keys']:
                spec['primary_keys'].append(cname)

        cur.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
            """,
            (schema,),
        )
        for table, column, ref_table, ref_col in cur.fetchall():
            spec = tables.get(str(table).strip().lower())
            if spec:
                spec['foreign_keys'].append(
                    {
                        'column': str(column or '').strip(),
                        'ref_table': str(ref_table or '').strip(),
                        'ref_column': str(ref_col or '').strip(),
                    }
                )

        cur.execute(
            """
            SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """,
            (schema,),
        )
        idx_cols: dict[tuple[str, str], list[str]] = {}
        for table, index_name, column in cur.fetchall():
            spec = tables.get(str(table or '').strip().lower())
            if not spec or not index_name:
                continue
            key = (str(table or '').strip().lower(), str(index_name))
            idx_cols.setdefault(key, []).append(str(column or '').strip())
        for (tkey, iname), cols in idx_cols.items():
            spec = tables.get(tkey)
            if spec:
                spec['indexes'].append(f"{iname} ({', '.join(c for c in cols if c)})")

        for tkey in [n for n in _MYSQL_SAMPLE if n in tables]:
            spec = tables[tkey]
            real = spec['name']
            if not all(ch.isalnum() or ch == '_' for ch in real):
                continue
            pick = [c for c in (spec.get('columns') or {}) if all(ch.isalnum() or ch == '_' for ch in c)][:12]
            if not pick:
                continue
            quoted = ', '.join(f'`{c}`' for c in pick)
            try:
                cur.execute(f'SELECT {quoted} FROM `{real}` LIMIT {_SAMPLE_ROWS}')
                cols = [str(c[0]) for c in (cur.description or [])]
                rows = []
                for raw in cur.fetchmany(_SAMPLE_ROWS):
                    rows.append({cols[i]: _json_safe(raw[i]) for i in range(len(cols))})
                spec['samples'] = rows
            except Exception:
                spec['samples'] = []

        tables = _apply_overrides(tables, overrides, mysql=True)
        return {
            'name': schema,
            'engine': 'mysql',
            'table_count': len(tables),
            'tables': tables,
            'error': None,
        }
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            if conn.is_connected():
                conn.close()
        except Exception:
            pass


def _db_entry(label: str, scanned: dict[str, Any]) -> dict[str, Any]:
    return {
        'label': label,
        'name': scanned.get('name'),
        'engine': scanned.get('engine'),
        'table_count': scanned.get('table_count') or 0,
        'tables': scanned.get('tables') or {},
        'error': scanned.get('error'),
    }


def tables_for_db(catalog: dict[str, Any], db_key: str | None = None) -> dict[str, Any]:
    dbs = catalog.get('databases')
    if isinstance(dbs, dict) and dbs:
        if db_key:
            return dict((dbs.get(db_key) or {}).get('tables') or {})
        for key in ('omsdata_FDC', catalog.get('database') or ''):
            if key and key in dbs:
                return dict((dbs[key].get('tables') or {}))
        first = next(iter(dbs.values()))
        return dict((first or {}).get('tables') or {})
    return dict(catalog.get('tables') or {})


def scan_schema(*, sample_rows: bool = True) -> dict[str, Any]:
    from py.utilities.setting import Config

    cfg = Config.SQL_SERVER_CONFIG
    fdc_name = os.getenv('FDC_AI_SQL_DATABASE') or cfg['database_fdc']
    ef_name = cfg['database_oms_ef']
    errors: list[str] = []
    databases: dict[str, Any] = {}

    try:
        fdc = scan_sqlserver_database(fdc_name, sample_rows=sample_rows)
        databases[fdc_name] = _db_entry('FDC', fdc)
        if fdc.get('error'):
            errors.append(f'{fdc_name}: {fdc["error"]}')
    except Exception as exc:
        errors.append(f'{fdc_name}: {exc}')
        databases[fdc_name] = _db_entry('FDC', {'name': fdc_name, 'engine': 'sqlserver', 'tables': {}, 'error': str(exc)})

    try:
        ef = scan_sqlserver_database(ef_name, sample_rows=False)
        databases[ef_name] = _db_entry('E_FAS / EF', ef)
        if ef.get('error'):
            errors.append(f'{ef_name}: {ef["error"]}')
    except Exception as exc:
        errors.append(f'{ef_name}: {exc}')
        databases[ef_name] = _db_entry('E_FAS / EF', {'name': ef_name, 'engine': 'sqlserver', 'tables': {}, 'error': str(exc)})

    try:
        mysql = scan_mysql_database()
        databases[mysql.get('name') or 'onlinedb'] = _db_entry('MySQL app (onlinedb)', mysql)
        if mysql.get('error'):
            errors.append(f'onlinedb: {mysql["error"]}')
    except Exception as exc:
        errors.append(f'onlinedb: {exc}')
        databases['onlinedb'] = _db_entry('MySQL app (onlinedb)', {'name': 'onlinedb', 'engine': 'mysql', 'tables': {}, 'error': str(exc)})

    fdc_tables = (databases.get(fdc_name) or {}).get('tables') or {}
    from py.ai.database.known_joins import CROSS_DB_JOINS, MYSQL_JOINS, OMS_JOINS

    catalog = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'database': fdc_name,
        'table_count': len(fdc_tables),
        'tables': fdc_tables,
        'databases': databases,
        'known_joins': {'oms': OMS_JOINS, 'mysql': MYSQL_JOINS, 'cross_db': CROSS_DB_JOINS},
        'error': '; '.join(errors) if errors else None,
    }
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return catalog


def _hydrate(data: dict[str, Any]) -> dict[str, Any]:
    overrides = load_overrides()
    dbs = data.get('databases')
    if isinstance(dbs, dict) and dbs:
        for spec in dbs.values():
            mysql = str(spec.get('engine') or '') == 'mysql'
            spec['tables'] = _apply_overrides(dict(spec.get('tables') or {}), overrides, mysql=mysql)
        data['tables'] = tables_for_db(data)
        data['table_count'] = len(data.get('tables') or {})
        return data
    tables = data.get('tables') or {}
    if isinstance(tables, dict) and tables:
        data['tables'] = _apply_overrides(tables, overrides)
    return data


def load_catalog(*, refresh: bool = False) -> dict[str, Any]:
    with _LOCK:
        if refresh or not _OUTPUT.is_file():
            try:
                return scan_schema()
            except Exception as exc:
                if _OUTPUT.is_file():
                    try:
                        data = json.loads(_OUTPUT.read_text(encoding='utf-8'))
                        data['error'] = str(exc)
                        return _hydrate(data)
                    except Exception:
                        pass
                return {'generated_at': None, 'database': 'omsdata_FDC', 'tables': {}, 'databases': {}, 'error': str(exc)}
        try:
            return _hydrate(json.loads(_OUTPUT.read_text(encoding='utf-8')))
        except Exception:
            return scan_schema()


def ensure_catalog_async(*, refresh: bool = False) -> None:
    def _run() -> None:
        try:
            need = refresh
            if not need and _OUTPUT.is_file():
                try:
                    existing = json.loads(_OUTPUT.read_text(encoding='utf-8'))
                    need = not isinstance(existing.get('databases'), dict) or len(existing.get('databases') or {}) < 3
                except Exception:
                    need = True
            elif not _OUTPUT.is_file():
                need = True
            load_catalog(refresh=need)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name='ai-schema-scan').start()
