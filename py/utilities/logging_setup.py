import html
import logging
import os
import re
from collections import deque
from logging.handlers import RotatingFileHandler

from py.utilities.setting import Config

_MAX_LOG_BYTES = 50 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_HTTP_ACCESS_MARK = ' - - ['
_RECORD_START = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}')
_USER_STAR = re.compile(r'\*\*\*.+\*\*\*')
_USER_ACTIVITY_MARKERS = (
    '[clientview]',
    '[newpage]',
    'search sku',
    '@@@search',
    '__@@@',
    '___@@@',
)


def _is_user_activity(message: str) -> bool:
    """Staff/client actions (SKU search, page use) belong in user.log, not system.log."""
    text = message or ''
    low = text.lower()
    if any(m in low for m in _USER_ACTIVITY_MARKERS):
        return True
    if _USER_STAR.search(text) and ('@@@' in text or '__' in text):
        return True
    return False


class MaxLevelFilter(logging.Filter):
    """Pass records strictly below max_level (e.g. INFO only, not WARNING+)."""

    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record):
        return record.levelno < self.max_level


class IgnoreHTTPRequestFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return not (' - - [' in message and ('"GET /' in message or '"POST /' in message))


class RerouteUserActivityFilter(logging.Filter):
    """Drop user-activity lines from system.log and write them to user.log instead."""

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if not _is_user_activity(message):
            return True
        try:
            logging.getLogger('user_logger').info(message)
        except Exception:
            pass
        return False


def _ensure_log_dir(path):
    log_dir = os.path.dirname(path) or '.'
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)


def _clear_handlers(logger):
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def _rotating_handler(path, level, formatter, filters=None):
    _ensure_log_dir(path)
    handler = RotatingFileHandler(
        path,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    for filt in filters or ():
        handler.addFilter(filt)
    return handler


def log_event(level, message):
    """Write a timestamped log line. HTTP access → visitor; warn/error → error; else system."""
    try:
        lvl = (level or 'info').lower()
        text = str(message or '')
        if lvl in ('error', 'warning', 'warn', 'critical'):
            logger = logging.getLogger('error_logger')
            if lvl == 'error' or lvl == 'critical':
                logger.error(text)
            else:
                logger.warning(text)
            return
        if _HTTP_ACCESS_MARK in text and ('"GET /' in text or '"POST /' in text or '"HEAD /' in text):
            logging.getLogger('visitor_logger').info(text)
            return
        if _is_user_activity(text):
            logging.getLogger('user_logger').info(text)
            return
        logging.getLogger('changes_logger').info(text)
    except Exception:
        pass


def log_user_action(message, username=''):
    """Authenticated user activity (navigation, login, form submit)."""
    try:
        prefix = f'***{username}*** ' if username and f'***{username}***' not in str(message) else ''
        logging.getLogger('user_logger').info(f'{prefix}{message}' if prefix else message)
    except Exception:
        pass


def log_visitor_action(message):
    try:
        logging.getLogger('visitor_logger').info(message)
    except Exception:
        pass


def configure_logging(app):
    log_dir = getattr(Config, 'LOG_DIR', None) or os.path.join(app.root_path, 'output', 'logs')
    os.makedirs(log_dir, exist_ok=True)

    system_log = getattr(Config, 'SYSTEM_LOG_FILE', None) or os.path.join(log_dir, 'system.log')
    error_log = getattr(Config, 'ERROR_LOG_FILE', None) or os.path.join(log_dir, 'error.log')
    user_log = getattr(Config, 'USER_LOG_FILE', None) or os.path.join(log_dir, 'user.log')
    visitor_log = getattr(Config, 'VISITOR_LOG_FILE', None) or os.path.join(log_dir, 'visitor.log')
    online_log = getattr(Config, 'ONLINE_LOG_FILE', None) or os.path.join(log_dir, 'online.log')

    app.config['LOG_DIR'] = log_dir
    app.config['SYSTEM_LOG_FILE'] = system_log
    app.config['ERROR_LOG_FILE'] = error_log
    app.config['USER_LOG_FILE'] = user_log
    app.config['VISITOR_LOG_FILE'] = visitor_log
    app.config['ONLINE_LOG_FILE'] = online_log
    app.config['LOG_FILE'] = system_log

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    http_filter = IgnoreHTTPRequestFilter()
    user_activity_filter = RerouteUserActivityFilter()

    root_logger = logging.getLogger()
    _clear_handlers(root_logger)

    logger = app.logger
    _clear_handlers(logger)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    system_handler = _rotating_handler(
        system_log,
        logging.INFO,
        formatter,
        (http_filter, user_activity_filter, MaxLevelFilter(logging.WARNING)),
    )
    error_handler = _rotating_handler(error_log, logging.WARNING, formatter, (http_filter,))
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(http_filter)

    werkzeug_log = logging.getLogger('werkzeug')
    werkzeug_log.setLevel(logging.ERROR)

    def _named_logger(name, path, level=logging.INFO, extra_filters=None):
        named = logging.getLogger(name)
        named.setLevel(level)
        named.propagate = False
        _clear_handlers(named)
        named.addHandler(_rotating_handler(path, level, formatter, extra_filters or ()))
        return named

    _named_logger('user_logger', user_log)
    _named_logger('visitor_logger', visitor_log)
    _named_logger('online_logger', online_log)
    _named_logger('error_logger', error_log, level=logging.WARNING)

    logger.addHandler(system_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)

    changes_logger = _named_logger(
        'changes_logger',
        system_log,
        extra_filters=(http_filter, user_activity_filter, MaxLevelFilter(logging.WARNING)),
    )
    changes_logger.addHandler(_rotating_handler(error_log, logging.WARNING, formatter, (http_filter,)))

    try:
        from py.utilities.online_presence import configure_online_logger

        configure_online_logger(online_log)
    except Exception:
        pass


def is_log_record_start(line):
    """True when a line begins a new logger record (timestamp prefix)."""
    return bool(_RECORD_START.match(line or ''))


def _classify_log(text):
    head = (text or '').split('\n', 1)[0]
    if '🔴' in head or 'SERVER_ERROR' in head or 'CLIENT_ERROR' in head or 'FAILED_LOGIN' in head:
        return 'error'
    if '🟢' in head or 'SUCCESS' in head:
        return 'success'
    if '🟡' in head or 'OTHER' in head or 'WARNING' in head:
        return 'warning'
    if 'ERROR' in head or 'CRITICAL' in head:
        return 'error'
    if 'INFO' in head:
        return 'info'
    return ''


def _highlight_log_html(text):
    highlighted = html.escape(text or '', quote=False)
    highlighted = re.sub(
        r'\*\*\*(.*?)\*\*\*',
        lambda m: f'<span class="highlight-blue">{m.group(1).upper()}</span>',
        highlighted,
    )
    highlighted = re.sub(
        r'@@@(.*?)@@@',
        r'<span class="highlight-purple">\1</span>',
        highlighted,
    )
    highlighted = re.sub(
        r'__(.*?)__\s*(.*)',
        lambda m: f'__{m.group(1)}__ <span class="highlight-red">{m.group(2).upper()}</span>',
        highlighted,
        count=1,
    )
    highlighted = re.sub(
        r'\b(\d{1,3}(?:\.\d{1,3}){3})\b',
        r'<span class="highlight-orange">\1</span>',
        highlighted,
    )
    return highlighted


def format_logs(lines, *, as_chunks=False):
    formatted = []
    for line in lines:
        raw = line if isinstance(line, str) else str(line)
        raw = raw.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')
        if not as_chunks:
            raw = raw.strip()
        log_class = _classify_log(raw)
        if as_chunks:
            log_class = f'{log_class} log-chunk'.strip()
        formatted.append((log_class, _highlight_log_html(raw)))
    return formatted


def iter_log_records(lines):
    """Group raw lines into complete records (header + traceback)."""
    current = []
    for line in lines:
        raw = (line if isinstance(line, str) else str(line)).rstrip('\n')
        if is_log_record_start(raw) and current:
            rec = '\n'.join(current).rstrip()
            if rec:
                yield rec
            current = [raw]
        else:
            current.append(raw)
    if current:
        rec = '\n'.join(current).rstrip()
        if rec:
            yield rec


def _count_lines(path):
    if not os.path.isfile(path):
        return 0
    total = 0
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            total += chunk.count(b'\n')
    return total


def _read_last_n_lines(path, n):
    if n <= 0 or not os.path.isfile(path):
        return []
    with open(path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size == 0:
            return []
        block = 8192
        data = b''
        pos = size
        while pos > 0 and data.count(b'\n') <= n:
            read_size = min(block, pos)
            pos -= read_size
            fh.seek(pos)
            data = fh.read(read_size) + data
        lines = data.splitlines()
        decoded = [ln.decode('utf-8', errors='replace') for ln in lines[-n:]]
        return decoded


def read_log_page(path, page=1, per_page=100, search_term='', group_records=False):
    """Newest-first page of formatted lines (or complete error chunks) without loading the whole file."""
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 100), 500))
    needle = (search_term or '').strip().lower()

    if not os.path.isfile(path):
        return [], 1, 1, 0

    if group_records:
        keep = page * per_page
        buf = deque(maxlen=keep)
        total = 0
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for rec in iter_log_records(fh):
                if needle and needle not in rec.lower():
                    continue
                total += 1
                buf.append(rec)
        window = list(buf)
    elif needle:
        keep = page * per_page
        buf = deque(maxlen=keep)
        total = 0
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if needle in line.lower():
                    total += 1
                    buf.append(line.rstrip('\n'))
        window = list(buf)
    else:
        total = _count_lines(path)
        window = _read_last_n_lines(path, page * per_page)

    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = min(page, total_pages)
    skip_newest = (page - 1) * per_page
    if skip_newest:
        window = window[:-skip_newest] if skip_newest < len(window) else []
    page_lines = window[-per_page:] if window else []
    newest_first = list(reversed(page_lines))
    return format_logs(newest_first, as_chunks=group_records), page, total_pages, total
