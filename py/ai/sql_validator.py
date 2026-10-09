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
_LIMIT = re.compile(r'\bLIMIT\s+(\d+)\s*;?\s*$', re.I)

# TOP immediately following SELECT (and an optional DISTINCT / ALL).
# Anchored deliberately: a TOP anywhere else in the statement belongs to a
# subquery and says nothing about how many rows come back.
_SELECT_TOP = re.compile(
    r'\s*(?:DISTINCT\s+|ALL\s+)?TOP\s*\(\s*(\d+)\s*\)|'
    r'\s*(?:DISTINCT\s+|ALL\s+)?TOP\s+(\d+)',
    re.I,
)
_QUALIFIER = re.compile(r'\s*(?:DISTINCT|ALL)\b', re.I)
_TOP_AFTER_QUALIFIER = re.compile(r'\s*TOP\s*\(\s*(\d+)\s*\)|\s*TOP\s+(\d+)', re.I)


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


def _mask_literals(sql: str) -> str:
    """Blank out string literals, preserving length.

    Length matters: the scanner below reports offsets that are used to
    splice the *original* text. _strip_strings collapses each literal to
    '' and shifts everything after it, so it cannot be used here.

    Blanking means a literal's contents can never be read as structure --
    a WHERE clause containing 'select (from) orders' is data.
    """
    return _STRING.sub(lambda match: ' ' * len(match.group(0)), sql or '')


def _outer_selects(sql: str) -> list[re.Match]:
    """SELECT keywords sitting at parenthesis depth 0.

    A SELECT inside parentheses is a subquery or a CTE body; it bounds its
    own result set and nothing else. Only depth-0 SELECTs determine how
    many rows the statement as a whole returns, and in a UNION there is
    more than one of them.

    This is the distinction a plain `TOP` keyword search cannot make, and
    the reason it is not a row-cap check.
    """
    masked = _mask_literals(sql)
    depth = 0
    found: list[re.Match] = []
    for match in re.finditer(r'[()]|\bSELECT\b', masked, re.I):
        token = match.group(0)
        if token == '(':
            depth += 1
        elif token == ')':
            depth = max(0, depth - 1)
        elif depth == 0:
            found.append(match)
    return found


def _has_outer_cap(sql: str) -> bool:
    """True when every top-level SELECT carries a row cap.

    Used as the post-rewrite guard: if the rewrite could not bound the
    statement, nothing executes. Fails closed on SQL with no SELECT at all.
    """
    selects = _outer_selects(sql)
    if not selects:
        return False
    return all(_SELECT_TOP.match(sql, match.end()) for match in selects)


def _inject_top(sql: str, n: int) -> str:
    """Guarantee every top-level SELECT is row-limited.

    `n` is a ceiling, not a target. An existing smaller TOP is kept, a
    larger one is clamped, and a trailing LIMIT is read as a request and
    then removed. The model can ask for fewer rows; it cannot raise the
    limit the caller configured.

    Only depth-0 SELECTs are rewritten. TOP must follow DISTINCT, not
    precede it -- `SELECT TOP 10 DISTINCT x` is a syntax error.
    """
    text = sql or ''
    cap = max(1, min(int(n), _hard_cap()))

    # A trailing LIMIT is MySQL/Postgres dialect leaking in. Honor the
    # number as an upper bound request, then drop the clause: SQL Server
    # has no LIMIT and would reject the statement outright.
    limited = _LIMIT.search(_mask_literals(text))
    if limited:
        cap = max(1, min(int(limited.group(1)), cap))
        text = text[:limited.start()].rstrip() + text[limited.end():]

    selects = _outer_selects(text)
    if not selects:
        return text

    # Right to left, so that each splice leaves the earlier offsets valid.
    for match in reversed(selects):
        qualifier = _QUALIFIER.match(text, match.end())
        insert_at = qualifier.end() if qualifier else match.end()

        existing = _TOP_AFTER_QUALIFIER.match(text, insert_at)
        if existing:
            current = int(existing.group(1) or existing.group(2))
            text = (
                text[:insert_at]
                + f' TOP {min(current, cap)}'
                + text[existing.end():]
            )
        else:
            text = text[:insert_at] + f' TOP {cap}' + text[insert_at:]

    return text


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
    if not _has_outer_cap(safe):
        return {
            'ok': False,
            'error': 'Every top-level SELECT must be row-limited',
            'sql': raw,
        }
    return {'ok': True, 'sql': safe, 'error': None, 'max_rows': cap}
