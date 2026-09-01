"""Flask API: POST /api/ai/query — natural language to read-only SQL."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from py.utilities.setting import login_required

ai_query_bp = Blueprint('ai_sql', __name__)


def _staff_ok() -> bool:
    try:
        from py.other.clientview import user_hides_staff_nav_links

        if user_hides_staff_nav_links(session.get('username')):
            return False
    except Exception:
        pass
    return bool(session.get('username'))


def _is_yeti() -> bool:
    return (session.get('username') or '').strip().lower() == 'yeti'


@ai_query_bp.record_once
def _on_register(state) -> None:
    from py.ai.database.schema_scanner import ensure_catalog_async

    ensure_catalog_async()


@ai_query_bp.route('/api/ai/query', methods=['POST'])
@login_required
def ai_query():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    payload = request.get_json(silent=True) or {}
    question = (payload.get('question') or payload.get('message') or '').strip()
    if not question:
        return jsonify({'ok': False, 'error': 'question is required'}), 400
    from py.utilities.logging_setup import log_user_action
    from py.ai.ai_sql_agent import run_ai_query

    username = str(session.get('username') or '')
    log_user_action(f'AI SQL query: {question[:160]}', username)
    result = run_ai_query(question, username=username)
    return jsonify(
        {
            'ok': bool(result.get('ok')),
            'answer': result.get('answer') or '',
            'sql': result.get('sql') or '',
            'data': result.get('data') or [],
            'tables_used': result.get('tables_used') or [],
            'reason': result.get('reason') or '',
            'error': result.get('error'),
            'row_count': result.get('row_count') or 0,
        }
    )


@ai_query_bp.route('/api/ai/schema')
@login_required
def ai_schema_status():
    if not _staff_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    from py.ai.database.schema_scanner import load_catalog

    refresh = str(request.args.get('refresh') or '').lower() in ('1', 'true', 'yes')
    catalog = load_catalog(refresh=refresh)
    tables = catalog.get('tables') or {}
    return jsonify(
        {
            'ok': True,
            'database': catalog.get('database'),
            'generated_at': catalog.get('generated_at'),
            'table_count': len(tables),
            'tables': sorted((spec.get('name') or name) for name, spec in tables.items()),
            'error': catalog.get('error'),
        }
    )


@ai_query_bp.route('/user_settings/schema-dictionary')
@login_required
def schema_dictionary_page():
    from flask import flash, redirect, render_template, url_for

    if not _is_yeti():
        flash('Access denied. Only yeti can open the database dictionary.', 'warning')
        return redirect(url_for('auth.user_settings'))
    return render_template(
        'schema_dictionary.html',
        current_user=session.get('username'),
    )


@ai_query_bp.route('/api/ai/schema-dictionary')
@login_required
def schema_dictionary_api():
    if not _is_yeti():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    from py.ai.database.dictionary import compact_catalog
    from py.ai.database.schema_scanner import load_catalog

    refresh = str(request.args.get('refresh') or '').lower() in ('1', 'true', 'yes')
    catalog = load_catalog(refresh=refresh)
    payload = compact_catalog(catalog)
    payload['ok'] = True
    return jsonify(payload)
