"""Pick a small set of tables from schema_catalog.json for one question."""
from __future__ import annotations

import json
import re
from typing import Any

_MAX_TABLES = 6
_CORE = ('ord_log', 'orders', 'inv', 'inv_data', 'customer', 'plog')
_NOISE_PREFIX = ('qm_', 'tmp_', 'temp_', 'bak_', 'old_', 'test_')
_NOISE_SUFFIX = ('_tmp', '_temp', '_bak', '_old', '_test')


def _is_noise_table(name: str) -> bool:
    low = (name or '').lower()
    return low.startswith(_NOISE_PREFIX) or low.endswith(_NOISE_SUFFIX)


def _tokens(text: str) -> list[str]:
    raw = (text or '').lower()
    parts = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', raw)
    keep = []
    for part in parts:
        if part.isdigit() and len(part) <= 4:
            continue
        if len(part) > 1 or part in ('po', 'sku'):
            keep.append(part)
    return keep


def retrieve_schema(question: str, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    from py.ai.database.schema_scanner import load_catalog, load_overrides

    catalog = catalog or load_catalog()
    from py.ai.database.schema_scanner import tables_for_db

    tables: dict[str, Any] = tables_for_db(catalog)
    overrides = load_overrides()
    aliases = overrides.get('_table_aliases') if isinstance(overrides.get('_table_aliases'), dict) else {}
    tokens = _tokens(question)
    scores: dict[str, int] = {name: 0 for name in tables}

    for alias, names in aliases.items():
        alias_l = str(alias).lower()
        if alias_l in (question or '').lower() or alias_l in tokens:
            for name in names or []:
                key = str(name).lower()
                if key in scores:
                    scores[key] += 12

    for name, spec in tables.items():
        if _is_noise_table(name) or _is_noise_table(spec.get('name') or ''):
            continue
        blob = ' '.join(
            [
                name,
                spec.get('name') or '',
                spec.get('description') or '',
                ' '.join(spec.get('aliases') or []),
                ' '.join(spec.get('columns') or {}),
                ' '.join(str(v) for v in (spec.get('columns') or {}).values()),
            ]
        ).lower()
        hit = 0
        if name in (question or '').lower() or (spec.get('name') or '').lower() in (question or '').lower():
            hit += 15
        for tok in tokens:
            if tok == name or tok in blob:
                hit += 2 if tok in name else 1
        scores[name] = scores.get(name, 0) + hit

    q = (question or '').lower()
    sales_q = any(word in q for word in ('销量', 'sold', '销售额', 'sales'))
    stock_q = any(word in q for word in ('库存', 'stock', 'onhand', 'on-hand', 'on hand', 'avail'))
    if sales_q and not stock_q:
        for drop in ('inv_data', 'inv'):
            if drop in scores:
                scores[drop] = min(int(scores[drop]), 2)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    chosen = [name for name, score in ranked if score >= 4][:_MAX_TABLES]
    if not chosen:
        chosen = [name for name in _CORE if name in tables][:_MAX_TABLES]

    slim = {}
    for name in chosen:
        spec = tables[name]
        slim[spec.get('name') or name] = {
            'description': spec.get('description') or '',
            'columns': spec.get('columns') or {},
            'column_types': spec.get('column_types') or {},
            'primary_keys': spec.get('primary_keys') or [],
            'foreign_keys': spec.get('foreign_keys') or [],
            'indexes': (spec.get('indexes') or [])[:12],
            'samples': (spec.get('samples') or [])[:3],
        }
    return {
        'tables': slim,
        'table_names': list(slim.keys()),
        'database': catalog.get('database'),
    }


def schema_json_for_prompt(retrieved: dict[str, Any]) -> str:
    from py.ai.database.known_joins import joins_for_tables

    compact = {}
    for name, spec in (retrieved.get('tables') or {}).items():
        sample = None
        rows = spec.get('samples') or []
        if rows:
            row = dict(list(rows[0].items())[:8])
            sample = [row]
        compact[name] = {
            'description': spec.get('description') or '',
            'columns': spec.get('columns') or {},
            'primary_keys': spec.get('primary_keys') or [],
            'indexes': (spec.get('indexes') or [])[:8],
            'sample_row': sample,
        }
    payload = {
        'tables': compact,
        'known_joins': joins_for_tables(list(compact.keys())),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)
