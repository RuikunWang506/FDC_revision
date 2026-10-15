"""Validate AI-generated SQL before any database execution."""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

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

_PARSE_DIALECT = 'tsql'

# Fallback allowlist for callers that do not supply one. The real rule is
# "the agent may only query what it was actually shown", which only the
# caller knows, so run_ai_query passes the retriever's own table set in.
# This list exists so a direct call is not unbounded by default.
_DEFAULT_ALLOWED = frozenset(
    {'ord_log', 'orders', 'inv', 'inv_data', 'customer', 'invoice', 'plog'}
)


def _max_rows() -> int:
    try:
        return max(1, min(int(os.getenv('FDC_AI_SQL_MAX_ROWS', '500') or 500), 1000))
    except ValueError:
        return 500


def _hard_cap() -> int:
    return 1000


def _allowed_tables() -> frozenset[str]:
    raw = os.getenv('FDC_AI_SQL_ALLOWED_TABLES', '') or ''
    names = {part.strip().lower() for part in raw.split(',') if part.strip()}
    return frozenset(names) if names else _DEFAULT_ALLOWED


def _enforce_allowlist() -> bool:
    """Whether a table outside the allowlist blocks execution.

    Off by default, on purpose. A new blocking rule that has never been
    measured is a good way to break working queries for reasons nobody can
    see. With this off the verdict is still computed and returned on every
    call, so the agent's log accumulates exactly the evidence needed to
    decide whether turning it on is safe -- and what the allowlist should
    actually contain.
    """
    raw = (os.getenv('FDC_AI_SQL_ENFORCE_ALLOWLIST', '') or '').strip().lower()
    return raw in {'1', 'true', 'yes', 'on'}


def _strip_comments(sql: str) -> str:
    return _COMMENT.sub(' ', sql or '')


def _strip_strings(sql: str) -> str:
    return _STRING.sub("''", sql or '')


def _mask_literals(sql: str) -> str:
    """Blank out string literals, preserving length.

    Length matters: the scanner below reports offsets that are used to
    splice the *original* text. _strip_strings collapses each literal to
    '' and shifts everything after it, so it cannot be used here.
    """
    return _STRING.sub(lambda match: ' ' * len(match.group(0)), sql or '')


def _outer_selects(sql: str) -> list[re.Match]:
    """SELECT keywords sitting at parenthesis depth 0.

    A SELECT inside parentheses is a subquery or a CTE body; it bounds its
    own result set and nothing else. Only depth-0 SELECTs determine how
    many rows the statement as a whole returns, and in a UNION there is
    more than one of them.
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
    """True when every top-level SELECT carries a row cap. Fails closed."""
    selects = _outer_selects(sql)
    if not selects:
        return False
    return all(_SELECT_TOP.match(sql, match.end()) for match in selects)


def referenced_tables(sql: str) -> set[str]:
    """Base tables the statement reads, lowercased.

    Aliases, CTE names and subquery aliases are excluded: `FROM orders o`
    contributes `orders` and not `o`, and `WITH recent AS (...) SELECT *
    FROM recent` contributes whatever `recent` was built from, not
    `recent` itself. That distinction is the whole reason this needs a
    parser -- in the raw text an alias and a table name are both just a
    word, and no amount of regex can tell them apart.

    Raises when the statement cannot be parsed. Callers decide what that
    means; it is never "no tables".
    """
    tree = sqlglot.parse_one(sql, dialect=_PARSE_DIALECT)
    if tree is None:
        raise ValueError('statement produced no parse tree')

    cte_names = {
        (cte.alias_or_name or '').lower() for cte in tree.find_all(exp.CTE)
    }

    found: set[str] = set()
    for node in tree.find_all(exp.Table):
        # node.name is the table itself; the alias lives on node.alias.
        # A reference to a CTE also appears as a Table node, which is why
        # the CTE names are subtracted rather than ignored.
        name = (node.name or '').lower()
        if name and name not in cte_names:
            found.add(name)
    return found


def _inject_top(sql: str, n: int) -> str:
    """Guarantee every top-level SELECT is row-limited.

    `n` is a ceiling, not a target. An existing smaller TOP is kept, a
    larger one is clamped, and a trailing LIMIT is read as a request and
    then removed. The model can ask for fewer rows; it cannot raise the
    limit the caller configured.

    TOP must follow DISTINCT, not precede it -- `SELECT TOP 10 DISTINCT x`
    is a syntax error.
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


def validate_sql(
    sql: str,
    *,
    default_limit: int | None = None,
    allowed_tables: Iterable[str] | None = None,
    enforce_allowlist: bool | None = None,
) -> dict[str, Any]:
    """Check and rewrite generated SQL. Returns a dict; never raises.

    The allowlist verdict is always computed and always returned, whether
    or not it is enforced. `enforce_allowlist` overrides the environment,
    which is what the tests use; production reads the env var so the rule
    can be measured in the log before it starts blocking anything.
    """
    raw = (sql or '').strip()
    audit: dict[str, Any] = {
        'tables': [],
        'blocked_tables': [],
        'allowlist_enforced': (
            _enforce_allowlist() if enforce_allowlist is None else bool(enforce_allowlist)
        ),
        'parse_error': None,
    }
    enforce = audit['allowlist_enforced']

    def _reject(message: str) -> dict[str, Any]:
        return {'ok': False, 'error': message, 'sql': raw, **audit}

    if not raw:
        return {'ok': False, 'error': 'Empty SQL', 'sql': '', **audit}
    cleaned = _strip_comments(raw).strip()
    if cleaned.endswith(';'):
        cleaned = cleaned[:-1].strip()
    inspect = _strip_strings(cleaned)
    if ';' in inspect:
        return _reject('Multiple SQL statements are not allowed')
    if re.search(r'\bGO\b', inspect, re.I):
        return _reject('Batch separator GO is not allowed')
    body = cleaned
    if _FORBIDDEN.search(inspect):
        return _reject('SQL contains a forbidden keyword')
    lead = inspect.lstrip().upper()
    if not (lead.startswith('SELECT') or lead.startswith('WITH')):
        return _reject('Only SELECT / WITH queries are allowed')

    # --- table allowlist -------------------------------------------------
    # The one check that cannot be done on the raw text: deciding whether a
    # word is a table, an alias or a CTE needs the parse tree.
    allowed = (
        frozenset(str(t).strip().lower() for t in allowed_tables)
        if allowed_tables is not None
        else _allowed_tables()
    )
    try:
        tables = sorted(referenced_tables(body))
    except Exception as exc:  # sqlglot raises several error types
        audit['parse_error'] = str(exc)
    else:
        audit['tables'] = tables
        audit['blocked_tables'] = sorted(set(tables) - allowed)

    if enforce:
        if audit['parse_error'] is not None:
            return _reject(
                'SQL could not be parsed, so its tables cannot be checked: '
                + audit['parse_error']
            )
        if audit['blocked_tables']:
            return _reject(
                'Query references tables outside the allowlist: '
                + ', '.join(audit['blocked_tables'])
            )

    cap = default_limit if default_limit is not None else _max_rows()
    cap = max(1, min(int(cap), _hard_cap()))
    safe = _inject_top(body, cap).strip()
    inspect2 = _strip_strings(_strip_comments(safe))
    if _FORBIDDEN.search(inspect2):
        return _reject('SQL contains a forbidden keyword after rewrite')
    if not _has_outer_cap(safe):
        return _reject('Every top-level SELECT must be row-limited')
    return {'ok': True, 'sql': safe, 'error': None, 'max_rows': cap, **audit}
