"""yetimaster-only impersonation: view the site as another account."""
from __future__ import annotations

import time
from typing import Any

from flask import session

MASTER_USERNAME = 'yetimaster'

_USERS_CACHE: list[dict[str, str]] = []
_USERS_CACHE_TS = 0.0
_USERS_CACHE_TTL = 30.0


def is_master_operator() -> bool:
    return (session.get('master_username') or '').strip().lower() == MASTER_USERNAME


def is_impersonating() -> bool:
    if not is_master_operator():
        return False
    current = (session.get('username') or '').strip().lower()
    return bool(current) and current != MASTER_USERNAME


def real_username() -> str:
    if is_master_operator():
        return (session.get('master_username') or MASTER_USERNAME).strip()
    return (session.get('username') or '').strip()


def enable_master_session() -> None:
    session['master_username'] = MASTER_USERNAME


def clear_master_session() -> None:
    session.pop('master_username', None)


def list_switchable_users() -> list[dict[str, str]]:
    global _USERS_CACHE, _USERS_CACHE_TS
    now = time.monotonic()
    if _USERS_CACHE and (now - _USERS_CACHE_TS) < _USERS_CACHE_TTL:
        return _USERS_CACHE
    from py.utilities.auth import get_db_connection

    connection = get_db_connection()
    if not connection:
        return list(_USERS_CACHE)
    rows: list[dict[str, str]] = []
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT u.username, COALESCE(ug.name, '') AS group_name, COALESCE(ug.slug, '') AS group_slug
            FROM users u
            LEFT JOIN user_groups ug ON u.user_group_id = ug.id
            ORDER BY u.username ASC
            """
        )
        for row in cursor.fetchall() or []:
            name = str(row.get('username') or '').strip()
            if not name:
                continue
            rows.append(
                {
                    'username': name,
                    'group_name': str(row.get('group_name') or '').strip() or '—',
                    'group_slug': str(row.get('group_slug') or '').strip(),
                }
            )
        cursor.close()
    except Exception:
        return list(_USERS_CACHE)
    finally:
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass
    _USERS_CACHE = rows
    _USERS_CACHE_TS = now
    return rows


def _lookup_username(target: str) -> str | None:
    want = (target or '').strip()
    if not want:
        return None
    for row in list_switchable_users():
        if row['username'].lower() == want.lower():
            return row['username']
    return None


def apply_view_as(target: str) -> tuple[bool, str]:
    """Switch session.username to target while keeping master_username."""
    if not is_master_operator():
        return False, 'Master mode only'
    actual = _lookup_username(target)
    if not actual:
        return False, 'Unknown user'
    from py.utilities.auth import get_user_permissions

    session['username'] = actual
    session['logged_in'] = True
    session['user_permissions'] = get_user_permissions(actual)
    return True, actual
