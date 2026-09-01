"""Online presence API — heartbeat + online user list."""
from __future__ import annotations

import secrets

from flask import Blueprint, jsonify, session

from py.utilities.online_presence import (
    heartbeat,
    list_online_users,
    mark_offline,
    mark_online,
    online_count,
)
from py.utilities.setting import login_required

presence_bp = Blueprint('presence', __name__)


def _presence_session_id() -> str:
    sid = session.get('presence_session_id')
    if not sid:
        sid = secrets.token_hex(12)
        session['presence_session_id'] = sid
    return sid


def touch_from_request() -> bool:
    """Extend online duration from a real page/API request (not heartbeat)."""
    if not _staff_ok():
        return False
    username = _presence_username()
    if not username:
        return False
    return mark_online(_presence_session_id(), username, wake_ai=True)


def _presence_username() -> str:
    try:
        from py.utilities.master_mode import real_username

        return real_username()
    except Exception:
        return session.get('username') or ''


def _staff_ok() -> bool:
    try:
        from py.utilities.master_mode import is_master_operator

        if is_master_operator():
            return True
    except Exception:
        pass
    try:
        from py.other.clientview import user_hides_staff_nav_links

        if user_hides_staff_nav_links(session.get('username')):
            return False
    except Exception:
        pass
    return bool(session.get('username'))


@presence_bp.route('/api/presence/heartbeat', methods=['POST'])
@login_required
def presence_heartbeat():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    username = _presence_username()
    if not username:
        return jsonify({'ok': False, 'error': 'not authenticated'}), 401
    sid = _presence_session_id()
    result = heartbeat(sid, username)
    result['online_count'] = online_count()
    return jsonify(result)


@presence_bp.route('/api/presence/online')
@login_required
def presence_online():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    users = list_online_users()
    return jsonify({'ok': True, 'count': len(users), 'users': users})


@presence_bp.route('/api/presence/offline', methods=['POST'])
@login_required
def presence_offline():
    sid = session.get('presence_session_id')
    if sid:
        mark_offline(sid)
        session.pop('presence_session_id', None)
    return jsonify({'ok': True})
