"""FDC AI assistant API — local Ollama + read-only tools."""
from __future__ import annotations

from io import BytesIO

from flask import Blueprint, jsonify, request, send_file, session

from py.utilities.fdc_ai_agent import check_llm_health, run_agent_turn, starter_suggestions
from py.utilities.fdc_ai_store import (
    delete_conversation,
    get_conversation,
    list_conversations,
    new_conversation_id,
    save_conversation,
)
from py.utilities.logging_setup import log_user_action
from py.utilities.setting import login_required

fdc_ai_bp = Blueprint('fdc_ai', __name__)


def _staff_ok() -> bool:
    try:
        from py.other.clientview import user_hides_staff_nav_links

        if user_hides_staff_nav_links(session.get('username')):
            return False
    except Exception:
        pass
    return bool(session.get('username'))


def _username() -> str:
    return str(session.get('username') or '').strip()


def _current_id() -> str:
    return str(session.get('fdc_ai_conversation_id') or '').strip()


def _set_current_id(conv_id: str | None) -> None:
    if conv_id:
        session['fdc_ai_conversation_id'] = conv_id
    else:
        session.pop('fdc_ai_conversation_id', None)


@fdc_ai_bp.route('/api/fdc-ai/health')
@login_required
def fdc_ai_health():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    health = check_llm_health()
    return jsonify(
        {
            'ok': health.get('ok', False),
            'health': health,
            'suggestions': starter_suggestions(),
            'conversation_id': _current_id() or None,
        }
    )


@fdc_ai_bp.route('/api/fdc-ai/conversations')
@login_required
def fdc_ai_conversations():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    rows = list_conversations(_username())
    return jsonify(
        {
            'ok': True,
            'conversations': rows,
            'current_id': _current_id() or None,
            'ttl_days': 7,
        }
    )


@fdc_ai_bp.route('/api/fdc-ai/conversations/current')
@login_required
def fdc_ai_conversation_current():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    conv = get_conversation(_username(), _current_id()) if _current_id() else None
    if not conv:
        _set_current_id(None)
        return jsonify({'ok': True, 'id': None, 'title': '', 'messages': []})
    return jsonify({'ok': True, **conv})


@fdc_ai_bp.route('/api/fdc-ai/conversations/<conv_id>')
@login_required
def fdc_ai_conversation_get(conv_id: str):
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    conv = get_conversation(_username(), conv_id)
    if not conv:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    _set_current_id(conv['id'])
    return jsonify({'ok': True, **conv})


@fdc_ai_bp.route('/api/fdc-ai/conversations/<conv_id>', methods=['DELETE'])
@login_required
def fdc_ai_conversation_delete(conv_id: str):
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    ok = delete_conversation(_username(), conv_id)
    if _current_id() == conv_id:
        _set_current_id(None)
    return jsonify({'ok': ok})


@fdc_ai_bp.route('/api/fdc-ai/chat', methods=['POST'])
@login_required
def fdc_ai_chat():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    payload = request.get_json(silent=True) or {}
    message = (payload.get('message') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': 'message is required'}), 400
    username = _username()
    conv_id = str(payload.get('conversation_id') or _current_id() or '').strip()
    if payload.get('reset') or payload.get('new_chat'):
        conv_id = ''
    stored = get_conversation(username, conv_id) if conv_id else None
    history = list((stored or {}).get('messages') or [])
    result = run_agent_turn(message, history=history, context={'username': username})
    reply = str(result.get('reply') or '')
    history = history + [
        {'role': 'user', 'content': message},
        {'role': 'assistant', 'content': reply},
    ]
    if not conv_id:
        conv_id = new_conversation_id()
    saved = save_conversation(username, conv_id, history, title=(stored or {}).get('title') or '')
    _set_current_id(conv_id)
    try:
        log_user_action(
            f'***{username}***__@@@FDC AI chat@@@ {message[:80]}',
            username,
        )
    except Exception:
        pass
    return jsonify(
        {
            'ok': True,
            'reply': reply,
            'tool_trace': result.get('tool_trace') or [],
            'health': result.get('health'),
            'conversation_id': conv_id,
            'title': (saved or {}).get('title') or '',
        }
    )


@fdc_ai_bp.route('/api/fdc-ai/export/ocean-styles')
@login_required
def fdc_ai_export_ocean_styles():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    from py.utilities.fdc_ai_tools import build_ocean_styles_xlsx

    payload, filename = build_ocean_styles_xlsx()
    try:
        log_user_action(
            f"***{session.get('username') or ''}***__@@@FDC AI export ocean styles@@@ {filename}",
            session.get('username') or '',
        )
    except Exception:
        pass
    return send_file(
        BytesIO(payload),
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@fdc_ai_bp.route('/api/fdc-ai/reset', methods=['POST'])
@login_required
def fdc_ai_reset():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    _set_current_id(None)
    return jsonify({'ok': True, 'conversation_id': None})
