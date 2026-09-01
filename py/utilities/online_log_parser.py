"""Parse online session log lines into structured data for the log viewer."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_PST = ZoneInfo('America/Los_Angeles')

# 2026-07-07 15:21:58,576 - INFO - yeti -- 在线5分钟，from 15:16:06 07/07/26 to 15:21:55 07/07/26
_LINE_RE = re.compile(
    r'^(?P<logged_at>[\d-]+ [\d:,]+)\s+-\s+\w+\s+-\s+'
    r'(?P<username>[^-]+?)\s+--\s+在线(?P<duration_text>[^，]+)，'
    r'from\s+(?P<start_stamp>[\d:/\s]+)\s+to\s+(?P<end_stamp>[\d:/\s]+)\s*$'
)

_STAMP_FMT = '%H:%M:%S %m/%d/%y'


def _parse_stamp(stamp: str) -> datetime | None:
    stamp = (stamp or '').strip()
    if not stamp:
        return None
    try:
        naive = datetime.strptime(stamp, _STAMP_FMT)
        return naive.replace(tzinfo=_PST)
    except ValueError:
        return None


def _parse_duration_seconds(text: str) -> int:
    text = (text or '').strip()
    if not text:
        return 0
    hours = minutes = seconds = 0
    m = re.search(r'(\d+)\s*小时', text)
    if m:
        hours = int(m.group(1))
    m = re.search(r'(\d+)\s*分钟', text)
    if m:
        minutes = int(m.group(1))
    m = re.search(r'(\d+)\s*秒', text)
    if m:
        seconds = int(m.group(1))
    if hours or minutes or seconds:
        return hours * 3600 + minutes * 60 + seconds
    m = re.search(r'(\d+)\s*h', text, re.I)
    if m:
        hours = int(m.group(1))
    m = re.search(r'(\d+)\s*m\b', text, re.I)
    if m:
        minutes = int(m.group(1))
    m = re.search(r'(\d+)\s*s\b', text, re.I)
    if m:
        seconds = int(m.group(1))
    return hours * 3600 + minutes * 60 + seconds


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours and minutes:
        return f'{hours}小时{minutes}分钟'
    if hours:
        return f'{hours}小时'
    if minutes:
        return f'{minutes}分钟'
    return f'{sec}秒'


def _fmt_time(dt: datetime) -> str:
    local = dt.astimezone(_PST) if dt.tzinfo else dt.replace(tzinfo=_PST)
    return local.strftime('%H:%M:%S')


def _fmt_date(dt: datetime) -> str:
    local = dt.astimezone(_PST) if dt.tzinfo else dt.replace(tzinfo=_PST)
    return local.strftime('%Y-%m-%d')


def _fmt_datetime(dt: datetime) -> str:
    local = dt.astimezone(_PST) if dt.tzinfo else dt.replace(tzinfo=_PST)
    return local.strftime('%m/%d %H:%M:%S')


def _parse_date_str(value: str | None) -> str | None:
    value = (value or '').strip()
    if not value:
        return None
    try:
        datetime.strptime(value, '%Y-%m-%d')
        return value
    except ValueError:
        return None


def normalize_date_range(
    date_from: str | None,
    date_to: str | None,
    *,
    default: str | None = None,
    max_days: int = 90,
) -> tuple[str, str]:
    fallback = default or today_pst()
    start = _parse_date_str(date_from) or fallback
    end = _parse_date_str(date_to) or start
    if start > end:
        start, end = end, start

    start_dt = datetime.strptime(start, '%Y-%m-%d')
    end_dt = datetime.strptime(end, '%Y-%m-%d')
    if (end_dt - start_dt).days + 1 > max_days:
        end_dt = start_dt + timedelta(days=max_days - 1)
        end = end_dt.strftime('%Y-%m-%d')

    return start, end


def _in_date_range(day: str, date_from: str | None, date_to: str | None) -> bool:
    if not date_from and not date_to:
        return True
    start, end = normalize_date_range(date_from, date_to)
    return start <= day <= end


def username_key(username: str) -> str:
    return (username or '').strip().lower()


def pick_display_username(preferred: str, candidate: str) -> str:
    pref = (preferred or '').strip()
    cand = (candidate or '').strip()
    if not pref:
        return cand
    if not cand:
        return pref
    if pref.lower() != cand.lower():
        return pref

    def casing_score(name: str) -> tuple[int, int]:
        return (sum(1 for c in name if c.isupper()), len(name))

    return cand if casing_score(cand) > casing_score(pref) else pref


def _session_username_key(session: dict[str, Any]) -> str:
    return session.get('username_key') or username_key(session.get('username', ''))


def _user_color(username: str) -> str:
    h = sum(ord(c) for c in username_key(username)) % 360
    return f'hsl({h}, 58%, 46%)'


def parse_online_log_line(line: str) -> dict[str, Any] | None:
    line = (line or '').strip()
    if not line:
        return None
    m = _LINE_RE.match(line)
    if not m:
        return None

    start = _parse_stamp(m.group('start_stamp'))
    end = _parse_stamp(m.group('end_stamp'))
    if not start or not end:
        return None

    duration_sec = _parse_duration_seconds(m.group('duration_text'))
    if duration_sec <= 0 and end > start:
        duration_sec = int((end - start).total_seconds())

    username = m.group('username').strip()
    return {
        'username': username,
        'username_key': username_key(username),
        'start': start,
        'end': end,
        'start_iso': start.isoformat(),
        'end_iso': end.isoformat(),
        'date': _fmt_date(start),
        'start_time': _fmt_time(start),
        'end_time': _fmt_time(end),
        'duration_sec': duration_sec,
        'duration_display': _fmt_duration(duration_sec),
        'raw_line': line,
        'color': _user_color(username),
    }


def load_online_sessions(
    log_path: str,
    *,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    username: str | None = None,
    search_term: str = '',
    max_lines: int = 50_000,
) -> list[dict[str, Any]]:
    if not log_path or not os.path.exists(log_path):
        return []

    if date and not date_from and not date_to:
        date_from = date_to = date
    elif date_from or date_to:
        date_from, date_to = normalize_date_range(date_from, date_to)
    else:
        date_from = date_to = None

    sessions: list[dict[str, Any]] = []
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if search_term and search_term.lower() not in line.lower():
                    continue
                parsed = parse_online_log_line(line)
                if not parsed:
                    continue
                if date_from and not _in_date_range(parsed['date'], date_from, date_to):
                    continue
                if username and parsed['username'].lower() != username.lower():
                    continue
                sessions.append(parsed)
                if len(sessions) >= max_lines:
                    break
    except OSError:
        return []

    sessions.sort(key=lambda s: s['start'])
    return sessions


def merge_sessions(
    sessions: list[dict[str, Any]],
    *,
    gap_sec: int = 180,
) -> list[dict[str, Any]]:
    """Merge adjacent sessions for the same user when the gap is small."""
    if not sessions:
        return []

    by_user: dict[str, list[dict[str, Any]]] = {}
    display_names: dict[str, str] = {}
    for s in sessions:
        key = _session_username_key(s)
        by_user.setdefault(key, []).append(s)
        display_names[key] = pick_display_username(display_names.get(key, ''), s['username'])

    merged: list[dict[str, Any]] = []
    for key, user_sessions in by_user.items():
        user_sessions.sort(key=lambda s: s['start'])
        block = dict(user_sessions[0])
        block['username'] = display_names[key]
        block['username_key'] = key
        block['fragment_count'] = 1
        block['fragments'] = [user_sessions[0]]

        for nxt in user_sessions[1:]:
            gap = (nxt['start'] - block['end']).total_seconds()
            if gap <= gap_sec:
                block['end'] = max(block['end'], nxt['end'])
                block['end_iso'] = block['end'].isoformat()
                block['end_time'] = _fmt_time(block['end'])
                block['duration_sec'] = int((block['end'] - block['start']).total_seconds())
                block['duration_display'] = _fmt_duration(block['duration_sec'])
                block['fragment_count'] += 1
                block['fragments'].append(nxt)
            else:
                merged.append(block)
                block = dict(nxt)
                block['username'] = display_names[key]
                block['username_key'] = key
                block['fragment_count'] = 1
                block['fragments'] = [nxt]

        merged.append(block)

    merged.sort(key=lambda s: s['start'], reverse=True)
    return merged


def filter_by_min_duration(
    sessions: list[dict[str, Any]],
    min_sec: int,
) -> list[dict[str, Any]]:
    if min_sec <= 0:
        return sessions
    return [s for s in sessions if s.get('duration_sec', 0) >= min_sec]


def summarize_users(
    sessions: list[dict[str, Any]],
    *,
    multi_day: bool = False,
) -> list[dict[str, Any]]:
    fmt = _fmt_datetime if multi_day else _fmt_time
    totals: dict[str, dict[str, Any]] = {}
    for s in sessions:
        key = _session_username_key(s)
        row = totals.get(key)
        if row is None:
            row = {
                'username': s['username'],
                'color': s.get('color') or _user_color(key),
                'total_sec': 0,
                'session_count': 0,
                'first_start': s['start'],
                'last_end': s['end'],
            }
            totals[key] = row
        else:
            row['username'] = pick_display_username(row['username'], s['username'])
        row['total_sec'] += s.get('duration_sec', 0)
        row['session_count'] += 1
        if s['start'] < row['first_start']:
            row['first_start'] = s['start']
        if s['end'] > row['last_end']:
            row['last_end'] = s['end']

    summary = []
    for row in totals.values():
        summary.append({
            'username': row['username'],
            'color': row['color'],
            'total_sec': row['total_sec'],
            'total_display': _fmt_duration(row['total_sec']),
            'session_count': row['session_count'],
            'first_start': fmt(row['first_start']),
            'last_end': fmt(row['last_end']),
        })

    summary.sort(key=lambda x: (-x['total_sec'], x['username'].lower()))
    return summary


def available_dates(log_path: str, limit: int = 60) -> list[str]:
    if not log_path or not os.path.exists(log_path):
        return []

    seen: set[str] = set()
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        return []

    for line in reversed(lines):
        parsed = parse_online_log_line(line)
        if not parsed:
            continue
        seen.add(parsed['date'])

    dates = sorted(seen, reverse=True)
    return dates[:limit]


def available_date_bounds(log_path: str) -> tuple[str | None, str | None]:
    if not log_path or not os.path.exists(log_path):
        return None, None

    earliest: str | None = None
    latest: str | None = None
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                parsed = parse_online_log_line(line)
                if not parsed:
                    continue
                day = parsed['date']
                if earliest is None or day < earliest:
                    earliest = day
                if latest is None or day > latest:
                    latest = day
    except OSError:
        return None, None

    return earliest, latest


def clamp_date_range_to_bounds(
    date_from: str,
    date_to: str,
    *,
    min_date: str | None,
    max_date: str | None,
) -> tuple[str, str]:
    start, end = date_from, date_to
    if min_date:
        if start < min_date:
            start = min_date
        if end < min_date:
            end = min_date
    if max_date:
        if start > max_date:
            start = max_date
        if end > max_date:
            end = max_date
    if start > end:
        start, end = end, start
    return start, end


def available_usernames(
    log_path: str,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[str]:
    if not log_path or not os.path.exists(log_path):
        return []

    if date and not date_from and not date_to:
        date_from = date_to = date
    elif date_from or date_to:
        date_from, date_to = normalize_date_range(date_from, date_to)

    names: dict[str, str] = {}
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                parsed = parse_online_log_line(line)
                if not parsed:
                    continue
                if date_from and not _in_date_range(parsed['date'], date_from, date_to):
                    continue
                key = _session_username_key(parsed)
                names[key] = pick_display_username(names.get(key, ''), parsed['username'])
    except OSError:
        return []

    return sorted(names.values(), key=str.lower)


def _timeline_axis_ticks(
    range_start: datetime,
    range_end: datetime,
    *,
    day_count: int,
) -> list[dict[str, Any]]:
    span_sec = max(1.0, (range_end - range_start).total_seconds())
    ticks: list[dict[str, Any]] = []

    def add_tick(dt: datetime, label: str) -> None:
        if dt < range_start or dt > range_end:
            return
        offset = (dt - range_start).total_seconds()
        ticks.append({
            'left_pct': round(100 * offset / span_sec, 4),
            'label': label,
        })

    if day_count <= 1:
        for hour in range(0, 25, 3):
            add_tick(range_start + timedelta(hours=hour), f'{hour:02d}:00')
    elif day_count <= 14:
        cur = range_start
        while cur < range_end:
            add_tick(cur, cur.strftime('%m/%d'))
            cur += timedelta(days=1)
    elif day_count <= 45:
        cur = range_start
        while cur < range_end:
            add_tick(cur, cur.strftime('%m/%d'))
            cur += timedelta(days=3)
    else:
        cur = range_start
        while cur < range_end:
            add_tick(cur, cur.strftime('%m/%d'))
            cur += timedelta(days=7)

    if not ticks:
        add_tick(range_start, range_start.strftime('%m/%d'))
        add_tick(range_end - timedelta(seconds=1), range_end.strftime('%m/%d'))
    return ticks


def _timeline_day_markers(
    range_start: datetime,
    range_end: datetime,
    span_sec: float,
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    cur = range_start + timedelta(days=1)
    while cur < range_end:
        offset = (cur - range_start).total_seconds()
        markers.append({
            'left_pct': round(100 * offset / span_sec, 4),
            'label': cur.strftime('%m/%d'),
        })
        cur += timedelta(days=1)
    return markers


def timeline_layout_combined(
    sessions: list[dict[str, Any]],
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    range_start = datetime.strptime(date_from, '%Y-%m-%d').replace(tzinfo=_PST)
    range_end = datetime.strptime(date_to, '%Y-%m-%d').replace(tzinfo=_PST) + timedelta(days=1)
    span_sec = max(1.0, (range_end - range_start).total_seconds())
    day_count = (datetime.strptime(date_to, '%Y-%m-%d') - datetime.strptime(date_from, '%Y-%m-%d')).days + 1
    multi_day = day_count > 1
    label_fmt = _fmt_datetime if multi_day else _fmt_time
    min_bar_pct = 0.06 if day_count > 14 else (0.08 if multi_day else 0.15)

    rows: dict[str, list[dict[str, Any]]] = {}
    display_names: dict[str, str] = {}
    for s in sessions:
        start = max(s['start'], range_start)
        end = min(s['end'], range_end)
        if end <= start:
            continue
        key = _session_username_key(s)
        display_names[key] = pick_display_username(display_names.get(key, ''), s['username'])
        offset = (start - range_start).total_seconds()
        width = (end - start).total_seconds()
        bar = {
            'left_pct': round(100 * offset / span_sec, 4),
            'width_pct': max(min_bar_pct, round(100 * width / span_sec, 4)),
            'start_label': label_fmt(start),
            'end_label': label_fmt(end),
            'duration_display': _fmt_duration(int(width)),
            'fragment_count': s.get('fragment_count', 1),
            'color': s.get('color') or _user_color(key),
        }
        rows.setdefault(key, []).append(bar)

    timeline_rows = []
    for key in sorted(rows.keys(), key=str.lower):
        username = display_names[key]
        timeline_rows.append({
            'username': username,
            'color': rows[key][0]['color'] if rows[key] else _user_color(key),
            'bars': sorted(rows[key], key=lambda b: b['left_pct']),
        })

    return {
        'date_from': date_from,
        'date_to': date_to,
        'multi_day': multi_day,
        'day_count': day_count,
        'ticks': _timeline_axis_ticks(range_start, range_end, day_count=day_count),
        'day_markers': _timeline_day_markers(range_start, range_end, span_sec) if multi_day else [],
        'rows': timeline_rows,
    }


def timeline_layout_range(
    sessions: list[dict[str, Any]],
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    return timeline_layout_combined(sessions, date_from=date_from, date_to=date_to)


def enrich_session_displays(
    sessions: list[dict[str, Any]],
    *,
    multi_day: bool,
) -> list[dict[str, Any]]:
    fmt = _fmt_datetime if multi_day else _fmt_time
    for s in sessions:
        s['start_display'] = fmt(s['start'])
        s['end_display'] = fmt(s['end'])
    return sessions


def today_pst() -> str:
    return datetime.now(_PST).strftime('%Y-%m-%d')
