"""Build the yeti schema-dictionary payload (collapsible DBs / tables / columns / joins)."""
from __future__ import annotations

from typing import Any

from py.ai.database.known_joins import (
    CORE_OMS_TABLES,
    CROSS_DB_JOINS,
    JOIN_KEYS,
    MYSQL_JOINS,
    OMS_JOINS,
    connections_for_table,
)
from py.ai.database.schema_scanner import load_catalog


def _col_map(spec: dict[str, Any]) -> dict[str, str]:
    types = spec.get('column_types') or {}
    desc = spec.get('columns') or {}
    out = {}
    for name in desc:
        out[name] = {
            'type': types.get(name) or '',
            'description': '' if desc.get(name) == name else (desc.get(name) or ''),
            'pk': name in (spec.get('primary_keys') or [])
            or name.lower() in {str(p).lower() for p in (spec.get('primary_keys') or [])},
        }
    return out


def _fk_connections(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for fk in spec.get('foreign_keys') or []:
        out.append(
            {
                'kind': 'fk',
                'column': fk.get('column'),
                'other_table': fk.get('ref_table'),
                'other_column': fk.get('ref_column'),
                'note': 'Declared foreign key',
            }
        )
    return out


def _known_connections(table_name: str, engine: str) -> list[dict[str, Any]]:
    out = []
    if engine == 'mysql':
        for row in MYSQL_JOINS:
            if row['left'].lower() == table_name.lower() or row['right'].lower() == table_name.lower():
                other = row['right'] if row['left'].lower() == table_name.lower() else row['left']
                col = row['left_col'] if row['left'].lower() == table_name.lower() else row['right_col']
                other_col = row['right_col'] if row['left'].lower() == table_name.lower() else row['left_col']
                out.append(
                    {
                        'kind': 'known',
                        'column': col,
                        'other_table': other,
                        'other_column': other_col,
                        'note': row.get('note') or '',
                        'source': row.get('source') or '',
                    }
                )
        for row in CROSS_DB_JOINS:
            if row['right'].lower() == table_name.lower():
                out.append(
                    {
                        'kind': 'cross-db',
                        'column': row['right_col'],
                        'other_table': f"{row.get('left_db')}.{row['left']}",
                        'other_column': row['left_col'],
                        'note': row.get('note') or '',
                        'source': row.get('source') or '',
                    }
                )
        return out
    for row in connections_for_table(table_name, include_cross=True):
        if row.get('kind') == 'cross-db':
            if row['left'].lower() == table_name.lower():
                out.append(
                    {
                        'kind': 'cross-db',
                        'column': row['left_col'],
                        'other_table': f"{row.get('right_db')}.{row['right']}",
                        'other_column': row['right_col'],
                        'note': row.get('note') or '',
                        'source': row.get('source') or '',
                    }
                )
            else:
                out.append(
                    {
                        'kind': 'cross-db',
                        'column': row['right_col'],
                        'other_table': f"{row.get('left_db')}.{row['left']}",
                        'other_column': row['left_col'],
                        'note': row.get('note') or '',
                        'source': row.get('source') or '',
                    }
                )
            continue
        other = row['right'] if row['left'].lower() == table_name.lower() else row['left']
        col = row['left_col'] if row['left'].lower() == table_name.lower() else row['right_col']
        other_col = row['right_col'] if row['left'].lower() == table_name.lower() else row['left_col']
        out.append(
            {
                'kind': 'known',
                'column': col,
                'other_table': other,
                'other_column': other_col,
                'note': row.get('note') or '',
                'source': row.get('source') or '',
            }
        )
    return out


def compact_catalog(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog = catalog or load_catalog()
    dbs_in = catalog.get('databases')
    if not isinstance(dbs_in, dict) or not dbs_in:
        dbs_in = {
            catalog.get('database') or 'omsdata_FDC': {
                'label': 'FDC',
                'name': catalog.get('database') or 'omsdata_FDC',
                'engine': 'sqlserver',
                'tables': catalog.get('tables') or {},
            }
        }
    core = {n.lower() for n in CORE_OMS_TABLES}
    databases = []
    for db_name, spec in dbs_in.items():
        engine = spec.get('engine') or 'sqlserver'
        tables_out = []
        raw_tables = spec.get('tables') or {}
        for tkey, tspec in sorted(raw_tables.items(), key=lambda kv: (kv[0] not in core, kv[0])):
            name = tspec.get('name') or tkey
            tables_out.append(
                {
                    'key': tkey,
                    'name': name,
                    'core': tkey in core and engine == 'sqlserver',
                    'description': tspec.get('description') or '',
                    'column_count': len(tspec.get('columns') or {}),
                    'primary_keys': tspec.get('primary_keys') or [],
                    'indexes': (tspec.get('indexes') or [])[:16],
                    'columns': _col_map(tspec),
                    'connections': _fk_connections(tspec) + _known_connections(tkey, engine),
                    'samples': (tspec.get('samples') or [])[:2] if tkey in core or tkey in ('products',) else [],
                }
            )
        databases.append(
            {
                'id': db_name,
                'label': spec.get('label') or db_name,
                'engine': engine,
                'table_count': len(tables_out),
                'error': spec.get('error'),
                'tables': tables_out,
            }
        )
    return {
        'ok': True,
        'generated_at': catalog.get('generated_at'),
        'error': catalog.get('error'),
        'databases': databases,
        'known_joins': {
            'oms': OMS_JOINS,
            'mysql': MYSQL_JOINS,
            'cross_db': CROSS_DB_JOINS,
            'join_keys': list(JOIN_KEYS),
        },
        'overrides_file': 'py/ai/database/schema_overrides.json',
        'core_tables': list(CORE_OMS_TABLES),
    }
