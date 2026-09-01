"""In-memory online presence + session-length logging.

Online time is driven by real Flask requests, not the JS heartbeat.
An idle tab that only pings /api/presence/heartbeat will time out.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_PST = ZoneInfo('America/Los_Angeles')
_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}
# Idle tabs keep sending heartbeats; only real requests refresh last_seen.
_IDLE_TIMEOUT_SEC = int(os.getenv('FDC_ONLINE_TIMEOUT_SEC', '180'))
_online_logger: logging.Logger | None = None


def _now() -> datetime:
    return datetime.now(_PST)


def _fmt_stamp(dt: datetime) -> str:
    local = dt.astimezone(_PST) if getattr(dt, 'tzinfo', None) else dt.replace(tzinfo=_PST)
    return local.strftime('%H:%M:%S %m/%d/%y')


def _fmt_duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours and minutes:
        return f'{hours}小时{minutes}分钟'
    if hours:
        return f'{hours}小时'
    if minutes:
        return f'{minutes}分钟'
    return f'{secs}秒'


def configure_online_logger(log_path: str) -> None:
    global _online_logger
    log_dir = os.path.dirname(log_path) or '.'
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger('online_logger')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _online_logger = logger


def _write_session_log(username: str, started: datetime, ended: datetime) -> None:
    duration = _fmt_duration(started, ended)
    line = (
        f'{username} -- 在线{duration}，'
        f'from {_fmt_stamp(started)} to {_fmt_stamp(ended)}'
    )
    logger = _online_logger or logging.getLogger('online_logger')
    if logger.handlers:
        logger.info(line)
        return
    log_path = os.path.join('output', 'logs', 'online.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f'{ended.strftime("%Y-%m-%d %H:%M:%S")},{0:03d} - INFO - {line}\n')


def _end_session_locked(session_id: str, *, use_last_seen: bool = False) -> None:
    entry = _SESSIONS.pop(session_id, None)
    if not entry:
        return
    started = entry.get('started_at')
    username = entry.get('username') or 'unknown'
    if not isinstance(started, datetime):
        return
    ended = entry.get('last_seen') if use_last_seen else _now()
    if not isinstance(ended, datetime):
        ended = _now()
    _write_session_log(username, started, ended)


def sweep_stale_sessions() -> None:
    cutoff = _now().timestamp() - _IDLE_TIMEOUT_SEC
    with _LOCK:
        stale = [
            sid
            for sid, entry in _SESSIONS.items()
            if float(entry.get('last_seen_ts', 0)) < cutoff
        ]
        for sid in stale:
            _end_session_locked(sid, use_last_seen=True)


def mark_online(session_id: str, username: str, *, wake_ai: bool = True) -> bool:
    if not session_id or not username:
        return False
    sweep_stale_sessions()
    now = _now()
    is_new = False
    with _LOCK:
        entry = _SESSIONS.get(session_id)
        if entry is None:
            _SESSIONS[session_id] = {
                'username': username,
                'started_at': now,
                'last_seen': now,
                'last_seen_ts': now.timestamp(),
            }
            is_new = True
        else:
            entry['username'] = username
            entry['last_seen'] = now
            entry['last_seen_ts'] = now.timestamp()
    if is_new and wake_ai:
        try:
            from py.utilities.fdc_ai_agent import wake_local_ai

            wake_local_ai(background=True)
        except Exception:
            pass
    return is_new


def mark_offline(session_id: str) -> None:
    if not session_id:
        return
    with _LOCK:
        _end_session_locked(session_id, use_last_seen=False)


def heartbeat(session_id: str, username: str) -> dict[str, Any]:
    """Keepalive ping only — does not extend online duration."""
    sweep_stale_sessions()
    return {'ok': True, 'session_new': False, 'idle_timeout_sec': _IDLE_TIMEOUT_SEC}


def list_online_users() -> list[dict[str, Any]]:
    sweep_stale_sessions()
    now = _now()
    by_user: dict[str, dict[str, Any]] = {}
    with _LOCK:
        for entry in _SESSIONS.values():
            raw_username = entry.get('username') or 'unknown'
            key = str(raw_username).strip().lower()
            started = entry.get('started_at')
            if not isinstance(started, datetime):
                continue
            elapsed_sec = max(0, int((now - started).total_seconds()))
            current = by_user.get(key)
            if current is None or elapsed_sec > current.get('elapsed_sec', 0):
                by_user[key] = {
                    'username': raw_username,
                    'since': _fmt_stamp(started),
                    'elapsed_sec': elapsed_sec,
                    'elapsed_display': _fmt_duration(started, now),
                    'last_seen': _fmt_stamp(entry.get('last_seen', now)),
                }
    return sorted(by_user.values(), key=lambda x: str(x['username']).lower())


def online_count() -> int:
    return len(list_online_users())
