"""Parse user activity log lines and attach them to online sessions."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from py.utilities.online_log_parser import _fmt_datetime, _fmt_time

_PST = ZoneInfo('America/Los_Angeles')

_TS_RE = re.compile(
    r'^(?P<logged_at>[\d-]+ [\d:,]+)\s+-\s+\w+\s+-\s+(?P<body>.+)$'
)
_USER_RE = re.compile(r'\*\*\*(?P<user>[^*]+)\*\*\*')
_ACTION_RE = re.compile(r'__@@@(?P<action>.+?)@@@__\s*(?P<details>.*)$')
_ACTION_RE_ALT = re.compile(r'__@@@(?P<action>.+?)@@@\s*(?P<details>.*)$')

# Page hits / API polls — not real staff actions.
_SKIP_ACTIONS = frozenset(
    {
        'opened page',
        'opened home menu',
        'called api',
        'submitted',
        'not found',
        'denied',
        'client error',
        'bad method',
        'rate limited',
        'accessed',
    }
)

_NAV_MARKERS = (
    '__@@@opened page@@@',
    '__@@@opened home menu@@@',
    '__@@@called api@@@',
    '__@@@submitted@@@',
    '__@@@not found@@@',
    '__@@@denied@@@',
    '__@@@client error@@@',
    '__@@@bad method@@@',
    '__@@@rate limited@@@',
)


def is_user_nav_noise(line: str) -> bool:
    """True for page-visit / generic navigation lines that should not appear in User Activity."""
    low = (line or '').lower()
    return any(m in low for m in _NAV_MARKERS)


def _parse_logged_at(value: str) -> datetime | None:
    value = (value or '').strip()
    if not value:
        return None
    for fmt, size in (('%Y-%m-%d %H:%M:%S,%f', 23), ('%Y-%m-%d %H:%M:%S', 19)):
        try:
            naive = datetime.strptime(value[:size], fmt)
            return naive.replace(tzinfo=_PST)
        except ValueError:
            continue
    return None


def _clean_details(text: str) -> str:
    text = (text or '').strip()
    text = re.sub(r'\s+ip=\S+', '', text)
    text = re.sub(r'\s+from_home=\S+', '', text)
    text = re.sub(r'\s+status=\S+', '', text)
    text = re.sub(r'\s+endpoint=\S+', '', text)
    text = re.sub(r'\s+q=\S+', '', text)
    return ' '.join(text.split())


def parse_user_log_line(line: str) -> dict[str, Any] | None:
    line = (line or '').strip()
    if not line or is_user_nav_noise(line):
        return None

    match = _TS_RE.match(line)
    if not match:
        return None

    timestamp = _parse_logged_at(match.group('logged_at'))
    if not timestamp:
        return None

    body = (match.group('body') or '').strip()
    user_m = _USER_RE.search(body)
    username = (user_m.group('user') if user_m else '').strip()
    if not username:
        return None

    action = ''
    details = ''
    action_m = _ACTION_RE.search(body) or _ACTION_RE_ALT.search(body)
    if action_m:
        action = (action_m.group('action') or '').strip()
        details = _clean_details(action_m.group('details') or '')
    else:
        rest = _USER_RE.sub('', body, count=1).strip(' -:')
        if rest.lower().startswith('has logged in'):
            action = 'logged in'
            details = ''
        else:
            action = _clean_details(rest) or 'action'
            details = ''

    if action.lower() in _SKIP_ACTIONS:
        return None

    summary = action if not details else f'{action} — {details.strip()}'
    return {
        'username': username,
        'timestamp': timestamp,
        'time_display': _fmt_time(timestamp),
        'datetime_display': _fmt_datetime(timestamp),
        'date': timestamp.astimezone(_PST).strftime('%Y-%m-%d'),
        'action_label': action,
        'details': details,
        'summary': summary,
        'is_page_access': False,
        'raw_line': line,
    }


def load_user_activities(
    log_path: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    username: str | None = None,
    max_lines: int = 100_000,
) -> list[dict[str, Any]]:
    if not log_path or not os.path.exists(log_path):
        return []

    activities: list[dict[str, Any]] = []
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                parsed = parse_user_log_line(line)
                if not parsed:
                    continue
                if date_from and parsed['date'] < date_from:
                    continue
                if date_to and parsed['date'] > date_to:
                    continue
                if username and parsed['username'].lower() != username.lower():
                    continue
                activities.append(parsed)
                if len(activities) >= max_lines:
                    break
    except OSError:
        return []

    activities.sort(key=lambda item: item['timestamp'])
    return activities


def _activity_in_window(activity: dict[str, Any], start: datetime, end: datetime) -> bool:
    ts = activity['timestamp']
    # SKU searches are logged in the view; presence is touched just after, so allow a tiny skew.
    slack = 2.0
    return (start.timestamp() - slack) <= ts.timestamp() <= (end.timestamp() + slack)


def _activity_key(activity: dict[str, Any]) -> tuple[str, str, str]:
    ts = activity['timestamp'].isoformat()
    return (activity.get('username', ''), ts, activity.get('summary', ''))


def _activities_for_window(
    activities: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    return sorted(
        (a for a in activities if _activity_in_window(a, start, end)),
        key=lambda item: item['timestamp'],
    )


def attach_activities_to_sessions(
    sessions: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    *,
    multi_day: bool = False,
) -> list[dict[str, Any]]:
    if not sessions:
        return sessions

    by_user: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        by_user.setdefault(activity['username'].lower(), []).append(activity)

    time_fmt = _fmt_datetime if multi_day else _fmt_time

    for session in sessions:
        user_activities = by_user.get(session['username'].lower(), [])
        window_activities = _activities_for_window(
            user_activities,
            session['start'],
            session['end'],
        )
        session['activities'] = window_activities
        session['activity_count'] = len(window_activities)

        fragments = session.get('fragments') or []
        if len(fragments) > 1:
            fragment_details: list[dict[str, Any]] = []
            matched_keys: set[tuple[str, str, str]] = set()
            for index, fragment in enumerate(fragments, start=1):
                frag_activities = _activities_for_window(
                    user_activities,
                    fragment['start'],
                    fragment['end'],
                )
                for activity in frag_activities:
                    matched_keys.add(_activity_key(activity))
                fragment_details.append({
                    'index': index,
                    'start_display': time_fmt(fragment['start']),
                    'end_display': time_fmt(fragment['end']),
                    'duration_display': fragment.get('duration_display', ''),
                    'activities': frag_activities,
                    'activity_count': len(frag_activities),
                })
            session['fragment_details'] = fragment_details
            session['gap_activities'] = [
                activity for activity in window_activities
                if _activity_key(activity) not in matched_keys
            ]
        else:
            session['fragment_details'] = []
            session['gap_activities'] = []

    return sessions


def _synthetic_session_from_cluster(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    from py.utilities.online_log_parser import (
        _fmt_date,
        _fmt_duration,
        _fmt_time,
        _user_color,
        username_key,
    )

    first = cluster[0]
    last = cluster[-1]
    start = first['timestamp']
    end = last['timestamp']
    username = first['username']
    duration_sec = max(0, int((end - start).total_seconds()))
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
        'raw_line': '',
        'color': _user_color(username),
        'is_open': False,
        'from_activity': True,
        'fragment_count': 1,
        'fragments': [],
    }


def cover_sessions_with_activities(
    sessions: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    *,
    gap_sec: int = 180,
) -> list[dict[str, Any]]:
    """Expand sessions to cover nearby SKU searches, and add sessions for unmatched actions."""
    from datetime import timedelta

    from py.utilities.online_log_parser import refresh_session_duration

    if not activities:
        return sessions

    sessions = sorted(sessions, key=lambda item: (item['username'].lower(), item['start']))
    gap = timedelta(seconds=max(0, int(gap_sec)))
    used: set[tuple[str, str, str]] = set()
    by_user: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        by_user.setdefault(activity['username'].lower(), []).append(activity)
    for acts in by_user.values():
        acts.sort(key=lambda item: item['timestamp'])

    for session in sessions:
        acts = by_user.get(session['username'].lower(), [])
        changed = True
        while changed:
            changed = False
            window_start = session['start'] - gap
            window_end = session['end'] + gap
            for activity in acts:
                key = _activity_key(activity)
                if key in used:
                    continue
                ts = activity['timestamp']
                if window_start <= ts <= window_end:
                    used.add(key)
                    if ts < session['start']:
                        session['start'] = ts
                        changed = True
                    if ts > session['end']:
                        session['end'] = ts
                        changed = True
        refresh_session_duration(session)

    leftovers = [a for a in activities if _activity_key(a) not in used]
    leftover_by_user: dict[str, list[dict[str, Any]]] = {}
    for activity in leftovers:
        leftover_by_user.setdefault(activity['username'].lower(), []).append(activity)

    synthetics: list[dict[str, Any]] = []
    for acts in leftover_by_user.values():
        acts.sort(key=lambda item: item['timestamp'])
        cluster = [acts[0]]
        for nxt in acts[1:]:
            prev = cluster[-1]['timestamp']
            if (nxt['timestamp'] - prev).total_seconds() <= gap_sec:
                cluster.append(nxt)
            else:
                synthetics.append(_synthetic_session_from_cluster(cluster))
                cluster = [nxt]
        synthetics.append(_synthetic_session_from_cluster(cluster))

    return sessions + synthetics
