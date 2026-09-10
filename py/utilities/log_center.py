"""Log center: system / error / user / online / visitor viewers."""
from __future__ import annotations

import json
import os
import time

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from py.utilities.logging_setup import format_logs, is_log_record_start, read_log_page
from py.utilities.setting import login_required

log_center_bp = Blueprint('log_center', __name__, url_prefix='/log')


def _denied():
    from py.utilities.log_viewer_permissions import enforce_log_viewer_access

    return enforce_log_viewer_access(session.get('username'))


def _cfg_path(key, default_name):
    return current_app.config.get(key) or os.path.join(
        current_app.config.get('LOG_DIR') or os.path.join(current_app.root_path, 'output', 'logs'),
        default_name,
    )


def _render_file_log(
    *,
    path,
    active_log,
    log_title,
    log_endpoint,
    search_endpoint,
    empty_message,
    live=False,
    group_records=False,
    per_page=100,
    skip_line=None,
):
    denied = _denied()
    if denied:
        return denied
    search_term = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int) or 1
    log_lines, page, total_pages, total = read_log_page(
        path,
        page=page,
        per_page=per_page,
        search_term=search_term,
        group_records=group_records,
        skip_line=skip_line,
    )
    stream_url = url_for('log_center.stream_log', kind=active_log) if live else ''
    return render_template(
        'log_viewer.html',
        log_lines=log_lines,
        page=page,
        total_pages=total_pages,
        current_user=session.get('username'),
        search_term=search_term,
        active_log=active_log,
        log_title=log_title,
        log_endpoint=log_endpoint,
        search_endpoint=search_endpoint,
        empty_message=empty_message,
        stream_url=stream_url,
        live_enabled=bool(live) and not search_term,
        total_count=total,
        group_records=group_records,
    )


@log_center_bp.route('')
@log_center_bp.route('/')
@login_required
def logs_redirect():
    denied = _denied()
    if denied:
        return denied
    return redirect(url_for('log_center.display_user'))


@log_center_bp.route('/system')
@login_required
def display_system():
    return _render_file_log(
        path=_cfg_path('SYSTEM_LOG_FILE', 'system.log'),
        active_log='system',
        log_title='⚙️ System Log',
        log_endpoint='log_center.display_system',
        search_endpoint='log_center.search_system',
        empty_message='系统日志为空或搜索无结果',
        live=True,
    )


@log_center_bp.route('/error')
@login_required
def display_error():
    return _render_file_log(
        path=_cfg_path('ERROR_LOG_FILE', 'error.log'),
        active_log='error',
        log_title='🚨 Error Log',
        log_endpoint='log_center.display_error',
        search_endpoint='log_center.search_error',
        empty_message='错误日志为空或搜索无结果',
        live=True,
        group_records=True,
        per_page=25,
    )


@log_center_bp.route('/user')
@login_required
def display_user():
    from py.utilities.user_log_parser import is_user_nav_noise

    return _render_file_log(
        path=_cfg_path('USER_LOG_FILE', 'user.log'),
        active_log='user',
        log_title='👤 User Activity',
        log_endpoint='log_center.display_user',
        search_endpoint='log_center.search_user',
        empty_message='用户活动日志为空或搜索无结果（仅显示搜 SKU、查订单等操作）',
        live=True,
        skip_line=is_user_nav_noise,
    )


@log_center_bp.route('/visitor')
@login_required
def display_visitor():
    return _render_file_log(
        path=_cfg_path('VISITOR_LOG_FILE', 'visitor.log'),
        active_log='visitor',
        log_title='👁 Visitor Log',
        log_endpoint='log_center.display_visitor',
        search_endpoint='log_center.search_visitor',
        empty_message='访客日志为空或搜索无结果',
        live=True,
    )


@log_center_bp.route('/online')
@login_required
def display_online():
    denied = _denied()
    if denied:
        return denied
    from py.utilities.online_log_parser import (
        available_date_bounds,
        available_usernames,
        clamp_date_range_to_bounds,
        enrich_session_displays,
        filter_by_min_duration,
        load_online_sessions,
        merge_sessions,
        normalize_date_range,
        summarize_users,
        timeline_layout_range,
        today_pst,
    )
    from py.utilities.user_log_parser import (
        attach_activities_to_sessions,
        cover_sessions_with_activities,
        load_user_activities,
    )

    search_term = request.args.get('search', '').strip()
    view = request.args.get('view', 'dashboard')
    legacy_date = request.args.get('date', '').strip()
    date_from = request.args.get('date_from', '').strip() or legacy_date or today_pst()
    date_to = request.args.get('date_to', '').strip() or legacy_date or date_from
    date_from, date_to = normalize_date_range(date_from, date_to)
    selected_user = request.args.get('user', '').strip()
    min_duration = request.args.get('min_sec', 30, type=int)
    merge_gap = request.args.get('merge_gap', 180, type=int)
    merge_enabled = request.args.get('merge', '1') != '0'

    path = _cfg_path('ONLINE_LOG_FILE', 'online.log')
    log_earliest, log_latest = available_date_bounds(path)
    date_min = log_earliest
    date_max = today_pst()
    if log_latest and log_latest > date_max:
        date_max = log_latest
    date_from, date_to = clamp_date_range_to_bounds(
        date_from, date_to, min_date=date_min, max_date=date_max
    )
    multi_day = date_from != date_to

    raw_sessions = load_online_sessions(
        path,
        date_from=date_from if view != 'raw' else None,
        date_to=date_to if view != 'raw' else None,
        username=selected_user or None,
        search_term=search_term,
    )
    if view != 'raw' and date_to >= today_pst():
        from py.utilities.online_presence import export_open_sessions

        for open_session in export_open_sessions():
            if selected_user and open_session['username'].lower() != selected_user.lower():
                continue
            if date_from and open_session['date'] < date_from:
                continue
            if date_to and open_session['date'] > date_to:
                continue
            if search_term and search_term.lower() not in open_session['username'].lower():
                continue
            raw_sessions.append(open_session)

    user_log_path = _cfg_path('USER_LOG_FILE', 'user.log')
    session_activities = load_user_activities(
        user_log_path,
        date_from=date_from if view != 'raw' else None,
        date_to=date_to if view != 'raw' else None,
        username=selected_user or None,
    )
    display_sessions = (
        merge_sessions(raw_sessions, gap_sec=merge_gap)
        if merge_enabled
        else sorted(raw_sessions, key=lambda s: s['start'], reverse=True)
    )
    display_sessions = cover_sessions_with_activities(
        display_sessions,
        session_activities,
        gap_sec=merge_gap if merge_enabled else 30,
    )
    if merge_enabled:
        display_sessions = merge_sessions(display_sessions, gap_sec=merge_gap)
    else:
        display_sessions.sort(key=lambda s: s['start'], reverse=True)
    display_sessions = filter_by_min_duration(display_sessions, min_duration)
    display_sessions = enrich_session_displays(display_sessions, multi_day=multi_day)
    display_sessions = attach_activities_to_sessions(
        display_sessions,
        session_activities,
        multi_day=multi_day,
    )

    user_summary = summarize_users(display_sessions, multi_day=multi_day)
    timeline = timeline_layout_range(display_sessions, date_from=date_from, date_to=date_to)
    usernames = available_usernames(path, date_from=date_from, date_to=date_to)

    page = request.args.get('page', 1, type=int) or 1
    log_lines, current_page, total_pages, total = read_log_page(
        path, page=page, per_page=50, search_term=search_term
    )

    return render_template(
        'log_online.html',
        sessions=display_sessions,
        user_summary=user_summary,
        usernames=usernames,
        selected_user=selected_user,
        date_from=date_from,
        date_to=date_to,
        date_min=date_min,
        date_max=date_max,
        multi_day=multi_day,
        today_pst=today_pst(),
        page=current_page,
        total_pages=total_pages,
        search_term=search_term,
        active_log='online',
        current_user=session.get('username'),
        total_count=len(display_sessions),
        view=view,
        min_duration=min_duration,
        merge_gap=merge_gap,
        merge_enabled=merge_enabled,
        timeline=timeline,
        raw_session_count=len(raw_sessions),
        filtered_session_count=len(display_sessions),
        log_lines=log_lines,
        total_raw_count=total,
    )


def _search_redirect(endpoint):
    denied = _denied()
    if denied:
        return denied
    search_term = request.form.get('search_term', '').strip()
    if not search_term:
        return redirect(url_for(endpoint))
    return redirect(url_for(endpoint, search=search_term, page=1))


@log_center_bp.route('/system/search', methods=['POST'])
@login_required
def search_system():
    return _search_redirect('log_center.display_system')


@log_center_bp.route('/error/search', methods=['POST'])
@login_required
def search_error():
    return _search_redirect('log_center.display_error')


@log_center_bp.route('/user/search', methods=['POST'])
@login_required
def search_user():
    return _search_redirect('log_center.display_user')


@log_center_bp.route('/visitor/search', methods=['POST'])
@login_required
def search_visitor():
    return _search_redirect('log_center.display_visitor')


@log_center_bp.route('/online/search', methods=['POST'])
@login_required
def search_online():
    denied = _denied()
    if denied:
        return denied
    search_term = request.form.get('search_term', '').strip()
    return redirect(url_for('log_center.display_online', search=search_term, view='raw'))


@log_center_bp.route('/stream')
@login_required
def stream_log():
    from py.utilities.log_viewer_permissions import user_can_view_logs

    if not user_can_view_logs(session.get('username')):
        return jsonify({'error': 'Forbidden'}), 403
    kind = (request.args.get('kind') or 'system').strip().lower()
    mapping = {
        'system': ('SYSTEM_LOG_FILE', 'system.log'),
        'error': ('ERROR_LOG_FILE', 'error.log'),
        'user': ('USER_LOG_FILE', 'user.log'),
        'visitor': ('VISITOR_LOG_FILE', 'visitor.log'),
        'online': ('ONLINE_LOG_FILE', 'online.log'),
    }
    key, default_name = mapping.get(kind, mapping['system'])
    log_path = _cfg_path(key, default_name)
    idle_sleep_s = 0.35
    keepalive_every_s = 25.0
    group_records = kind == 'error'
    flush_idle_s = 0.55
    skip_fn = None
    if kind == 'user':
        from py.utilities.user_log_parser import is_user_nav_noise

        skip_fn = is_user_nav_noise

    def generate():
        last_keepalive = time.monotonic()
        last_line_at = time.monotonic()
        pending = []
        os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)
        if not os.path.isfile(log_path):
            open(log_path, 'a', encoding='utf-8').close()

        def emit_record(text, as_chunk):
            formatted_line_tuple = format_logs([text], as_chunks=as_chunk)
            if formatted_line_tuple:
                log_cls, formatted_line = formatted_line_tuple[0]
            else:
                log_cls, formatted_line = ('error log-chunk' if as_chunk else ''), text
            chunk = json.dumps(
                {'log_class': log_cls, 'formatted_line': formatted_line},
                ensure_ascii=False,
            )
            return f'data: {chunk}\n\n'

        def flush_pending():
            if not pending:
                return None
            rec = '\n'.join(pending).rstrip()
            pending.clear()
            if not rec:
                return None
            return emit_record(rec, True)

        with open(log_path, 'r', encoding='utf-8', errors='replace') as file:
            file.seek(0, 2)
            while True:
                line = file.readline()
                if line:
                    last_keepalive = time.monotonic()
                    last_line_at = last_keepalive
                    raw = line.rstrip('\n')
                    if skip_fn and skip_fn(raw):
                        continue
                    if group_records:
                        if is_log_record_start(raw) and pending:
                            flushed = flush_pending()
                            pending.append(raw)
                            if flushed:
                                yield flushed
                        else:
                            pending.append(raw)
                        continue
                    yield emit_record(raw.strip(), False)
                    continue
                now = time.monotonic()
                if group_records and pending and (now - last_line_at) >= flush_idle_s:
                    flushed = flush_pending()
                    if flushed:
                        yield flushed
                    last_keepalive = now
                if now - last_keepalive >= keepalive_every_s:
                    yield ':\n\n'
                    last_keepalive = now
                time.sleep(idle_sleep_s)

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp
