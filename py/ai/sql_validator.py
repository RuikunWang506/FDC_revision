"""Validate AI-generated SQL before any database execution."""
from __future__ import annotations

import os
import re
from typing import Any

_FORBIDDEN = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|EXEC|EXECUTE|'
    r'MERGE|BACKUP|RESTORE|SHUTDOWN|WAITFOR|OPENQUERY|OPENROWSET|OPENDATASOURCE|'
    r'BULK\s+INSERT|SELECT\s+INTO)\b|'
    r'\bxp_\w+',
    re.I,
)
_COMMENT = re.compile(r'/\*.*?\*/|--.*?$|#.*?$', re.I | re.M | re.S)
_STRING = re.compile(r"('(?:''|[^'])*')|(\"(?:\\.|[^\"])*\")")
_TOP = re.compile(r'\bTOP\s+\(\s*(\d+)\s*\)|\bTOP\s+(\d+)', re.I)
_LIMIT = re.compile(r'\bLIMIT\s+(\d+)\s*;?\s*$', re.I)


def _max_rows() -> int:
    try:
        return max(1, min(int(os.getenv('FDC_AI_SQL_MAX_ROWS', '500') or 500), 1000))
    except ValueError:
        return 500


def _hard_cap() -> int:
    return 1000


def _strip_comments(sql: str) -> str:
    return _COMMENT.sub(' ', sql or '')


def _strip_strings(sql: str) -> str:
    return _STRING.sub("''", sql or '')


def _inject_top(sql: str, n: int) -> str:
    if _TOP.search(sql):
        def _cap(match: re.Match) -> str:
            cur = int(match.group(1) or match.group(2) or n)
            return f'TOP {min(cur, _hard_cap())}'

        return _TOP.sub(_cap, sql, count=1)
    limited = _LIMIT.search(sql)
    if limited:
        n = min(int(limited.group(1)), _hard_cap())
        sql = _LIMIT.sub('', sql).rstrip()
    matches = list(re.finditer(r'\bSELECT\b', sql, re.I))
    if not matches:
        return sql
    lead = sql.lstrip().upper()
    target = matches[-1] if lead.startswith('WITH') else matches[0]
    tail = sql[target.end() :]
    dist = re.match(r'\s*DISTINCT\b', tail, re.I)
    insert_at = target.end() + (dist.end() if dist else 0)
    return sql[:insert_at] + f' TOP {n} ' + sql[insert_at:]


def validate_sql(sql: str, *, default_limit: int | None = None) -> dict[str, Any]:
    raw = (sql or '').strip()
    if not raw:
        return {'ok': False, 'error': 'Empty SQL', 'sql': ''}
    cleaned = _strip_comments(raw).strip()
    if cleaned.endswith(';'):
        cleaned = cleaned[:-1].strip()
    inspect = _strip_strings(cleaned)
    if ';' in inspect:
        return {'ok': False, 'error': 'Multiple SQL statements are not allowed', 'sql': raw}
    if re.search(r'\bGO\b', inspect, re.I):
        return {'ok': False, 'error': 'Batch separator GO is not allowed', 'sql': raw}
    body = cleaned
    if _FORBIDDEN.search(inspect):
        return {'ok': False, 'error': 'SQL contains a forbidden keyword', 'sql': raw}
    lead = inspect.lstrip().upper()
    if not (lead.startswith('SELECT') or lead.startswith('WITH')):
        return {'ok': False, 'error': 'Only SELECT / WITH queries are allowed', 'sql': raw}
    cap = default_limit if default_limit is not None else _max_rows()
    cap = max(1, min(int(cap), _hard_cap()))
    safe = _inject_top(body, cap).strip()
    inspect2 = _strip_strings(_strip_comments(safe))
    if _FORBIDDEN.search(inspect2):
        return {'ok': False, 'error': 'SQL contains a forbidden keyword after rewrite', 'sql': raw}
    if not _TOP.search(safe):
        return {'ok': False, 'error': 'Query must use TOP or LIMIT', 'sql': raw}
    return {'ok': True, 'sql': safe, 'error': None, 'max_rows': cap}
