"""Log center: system / error / user / online / visitor viewers."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

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

_ONLINE_LINE = re.compile(
    r'^(?P<stamp>.+?)\s+-\s+\w+\s+-\s+(?P<user>.+?)\s+--\s+在线(?P<dur>[^，,]+)'
    r'[，,]\s*from\s+(?P<start>.+?)\s+to\s+(?P<end>.+)\s*$'
)
_DUR_PARTS = re.compile(r'(?:(?P<h>\d+)\s*小时)?(?:(?P<m>\d+)\s*分钟)?(?:(?P<s>\d+)\s*秒)?')
_USER_COLORS = (
    '#0d6efd',
    '#198754',
    '#dc3545',
    '#6f42c1',
    '#fd7e14',
    '#20c997',
    '#d63384',
    '#0dcaf0',
)


def _user_color(name: str) -> str:
    h = 0
    for ch in (name or '').lower():
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _USER_COLORS[h % len(_USER_COLORS)]


def _duration_seconds(text: str) -> int:
    m = _DUR_PARTS.search(text or '')
    if not m:
        return 0
    hours = int(m.group('h') or 0)
    minutes = int(m.group('m') or 0)
    seconds = int(m.group('s') or 0)
    return hours * 3600 + minutes * 60 + seconds


def _fmt_seconds(total: int) -> str:
    total = max(0, int(total or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours and minutes:
        return f'{hours}小时{minutes}分钟'
    if hours:
        return f'{hours}小时'
    if minutes:
        return f'{minutes}分钟'
    return f'{secs}秒'


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
    return _render_file_log(
        path=_cfg_path('USER_LOG_FILE', 'user.log'),
        active_log='user',
        log_title='👤 User Activity',
        log_endpoint='log_center.display_user',
        search_endpoint='log_center.search_user',
        empty_message='用户活动日志为空或搜索无结果',
        live=True,
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


def _parse_online_sessions(path, search_term='', date_from='', date_to=''):
    sessions = []
    if not os.path.isfile(path):
        return sessions
    needle = (search_term or '').strip().lower()
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            raw = line.rstrip('\n')
            if needle and needle not in raw.lower():
                continue
            m = _ONLINE_LINE.match(raw)
            if not m:
                continue
            stamp = (m.group('stamp') or '').strip()
            day = ''
            try:
                day = datetime.strptime(stamp[:19], '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d')
            except ValueError:
                pass
            if date_from and day and day < date_from:
                continue
            if date_to and day and day > date_to:
                continue
            sessions.append(
                {
                    'username': m.group('user').strip(),
                    'duration': m.group('dur').strip(),
                    'duration_sec': _duration_seconds(m.group('dur')),
                    'start': m.group('start').strip(),
                    'end': m.group('end').strip(),
                    'stamp': stamp,
                    'day': day,
                    'raw': raw,
                    'color': _user_color(m.group('user').strip()),
                }
            )
    sessions.reverse()
    return sessions


@log_center_bp.route('/online')
@login_required
def display_online():
    denied = _denied()
    if denied:
        return denied
    search_term = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    selected_user = request.args.get('user', '').strip()
    path = _cfg_path('ONLINE_LOG_FILE', 'online.log')
    sessions = _parse_online_sessions(path, search_term=search_term, date_from=date_from, date_to=date_to)
    usernames = sorted({s['username'] for s in sessions}, key=str.lower)
    if selected_user:
        sessions = [s for s in sessions if s['username'].lower() == selected_user.lower()]
    per_page = 100
    total = len(sessions)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = request.args.get('page', 1, type=int) or 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_sessions = sessions[start : start + per_page]
    summaries = {}
    for s in sessions:
        row = summaries.setdefault(
            s['username'],
            {'username': s['username'], 'session_count': 0, 'total_sec': 0, 'color': s.get('color')},
        )
        row['session_count'] += 1
        row['total_sec'] += int(s.get('duration_sec') or 0)
    user_summary = sorted(summaries.values(), key=lambda r: (-r['total_sec'], -r['session_count'], r['username'].lower()))
    for row in user_summary:
        row['total_display'] = _fmt_seconds(row['total_sec'])
    return render_template(
        'log_online.html',
        sessions=page_sessions,
        user_summary=user_summary,
        usernames=usernames,
        selected_user=selected_user,
        date_from=date_from,
        date_to=date_to,
        today_pst=datetime.now().strftime('%Y-%m-%d'),
        page=page,
        total_pages=total_pages,
        search_term=search_term,
        active_log='online',
        current_user=session.get('username'),
        total_count=total,
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
    return redirect(url_for('log_center.display_online', search=search_term))


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
