"""Persist FDC AI chats in MySQL for 7 days."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from py.utilities.database_utils import get_mysql_connection

_TTL_DAYS = 7
_MAX_MESSAGES = 200
_TITLE_LEN = 60


def _conn():
    return get_mysql_connection()


def _iso(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M')
    text = str(value)
    return text[:16] if len(text) >= 16 else text


def _clean_messages(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8', 'replace')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get('role') or '')
        content = str(item.get('content') or '').strip()
        if role not in ('user', 'assistant') or not content:
            continue
        out.append({'role': role, 'content': content})
        if len(out) >= _MAX_MESSAGES:
            break
    return out


def _title_from_messages(messages: list[dict[str, str]], fallback: str = '') -> str:
    for item in messages:
        if item.get('role') == 'user' and item.get('content'):
            text = ' '.join(str(item['content']).split())
            if len(text) > _TITLE_LEN:
                return text[: _TITLE_LEN - 1].rstrip() + '…'
            return text
    text = ' '.join(str(fallback or '').split())
    if not text:
        return 'New chat'
    if len(text) > _TITLE_LEN:
        return text[: _TITLE_LEN - 1].rstrip() + '…'
    return text


def ensure_chat_schema() -> bool:
    connection = _conn()
    if not connection:
        return False
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS fdc_ai_chats (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                username VARCHAR(80) NOT NULL,
                title VARCHAR(180) NOT NULL DEFAULT '',
                messages_json MEDIUMTEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_fdc_ai_user_updated (username, updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            f"""
            DELETE FROM fdc_ai_chats
            WHERE updated_at < (NOW() - INTERVAL {_TTL_DAYS} DAY)
            """
        )
        connection.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if connection and connection.is_connected():
                connection.close()
        except Exception:
            pass


def new_conversation_id() -> str:
    return uuid.uuid4().hex


def get_conversation(username: str, conv_id: str) -> dict[str, Any] | None:
    username = (username or '').strip()
    conv_id = (conv_id or '').strip()
    if not username or not conv_id:
        return None
    ensure_chat_schema()
    connection = _conn()
    if not connection:
        return None
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, title, messages_json, updated_at
            FROM fdc_ai_chats
            WHERE id = %s AND username = %s
              AND updated_at >= (NOW() - INTERVAL %s DAY)
            LIMIT 1
            """,
            (conv_id, username, _TTL_DAYS),
        )
        row = cursor.fetchone()
        if not row:
            return None
        messages = _clean_messages(row[2])
        return {
            'id': str(row[0]),
            'title': str(row[1] or '') or _title_from_messages(messages),
            'messages': messages,
            'updated_at': _iso(row[3]),
        }
    except Exception:
        return None
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if connection and connection.is_connected():
                connection.close()
        except Exception:
            pass


def list_conversations(username: str, limit: int = 40) -> list[dict[str, Any]]:
    username = (username or '').strip()
    if not username:
        return []
    ensure_chat_schema()
    connection = _conn()
    if not connection:
        return []
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, title, messages_json, updated_at
            FROM fdc_ai_chats
            WHERE username = %s
              AND updated_at >= (NOW() - INTERVAL %s DAY)
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (username, _TTL_DAYS, max(1, min(int(limit), 80))),
        )
        rows = []
        for row in cursor.fetchall() or []:
            messages = _clean_messages(row[2])
            if not messages:
                continue
            preview = ''
            for item in reversed(messages):
                if item.get('content'):
                    preview = ' '.join(str(item['content']).split())[:80]
                    break
            rows.append(
                {
                    'id': str(row[0]),
                    'title': str(row[1] or '') or _title_from_messages(messages),
                    'preview': preview,
                    'updated_at': _iso(row[3]),
                    'message_count': len(messages),
                }
            )
        return rows
    except Exception:
        return []
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if connection and connection.is_connected():
                connection.close()
        except Exception:
            pass


def save_conversation(
    username: str,
    conv_id: str,
    messages: list[dict[str, str]],
    title: str = '',
) -> dict[str, Any] | None:
    username = (username or '').strip()
    conv_id = (conv_id or '').strip()
    cleaned = _clean_messages(messages)[-_MAX_MESSAGES:]
    if not username or not conv_id or not cleaned:
        return None
    ensure_chat_schema()
    title = (title or '').strip() or _title_from_messages(cleaned)
    connection = _conn()
    if not connection:
        return None
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO fdc_ai_chats (id, username, title, messages_json)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                messages_json = VALUES(messages_json),
                updated_at = CURRENT_TIMESTAMP
            """,
            (conv_id, username, title[:180], json.dumps(cleaned, ensure_ascii=False)),
        )
        connection.commit()
        return {
            'id': conv_id,
            'title': title[:180],
            'messages': cleaned,
        }
    except Exception:
        return None
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if connection and connection.is_connected():
                connection.close()
        except Exception:
            pass


def delete_conversation(username: str, conv_id: str) -> bool:
    username = (username or '').strip()
    conv_id = (conv_id or '').strip()
    if not username or not conv_id:
        return False
    ensure_chat_schema()
    connection = _conn()
    if not connection:
        return False
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            'DELETE FROM fdc_ai_chats WHERE id = %s AND username = %s',
            (conv_id, username),
        )
        connection.commit()
        return cursor.rowcount > 0
    except Exception:
        return False
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if connection and connection.is_connected():
                connection.close()
        except Exception:
            pass
