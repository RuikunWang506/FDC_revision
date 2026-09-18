"""
Optional Slack alerts: Incoming Webhooks (channels) and bot-token DMs.

Webhook helpers need Config.SLACK_WEBHOOK_URL_* .
DM helpers need Config.SLACK_TOKEN (bot token with users:read, im:write, chat:write).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from py.utilities.setting import Config

log = logging.getLogger(__name__)


def send_slack_webhook_optional(webhook_url, text):
    """
    POST plain text to a Slack Incoming Webhook URL. No-op when URL falsy or invalid.
    Does not raise (logs on failure).
    """
    url = (webhook_url or "").strip()
    if not url.startswith("http"):
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning("Slack webhook post failed: %s", e)
        return False


def notify_v7_list_creator_optional(text_body):
    """
    Send to SLACK_WEBHOOK_URL_V7_LIST_CREATOR if set (otherwise no-op).
    """
    webhook = getattr(Config, "SLACK_WEBHOOK_URL_V7_LIST_CREATOR", None)
    return send_slack_webhook_optional(webhook, text_body)


def notify_mail_sent_optional(text_body):
    """
    Announce compose/send results to Slack.
    Uses SLACK_WEBHOOK_URL_MAIL_SENT (env or Config), else same webhook as V7 List Creator.
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL_MAIL_SENT", "").strip()
    if not webhook:
        webhook = getattr(Config, "SLACK_WEBHOOK_URL_MAIL_SENT", None) or ""
    webhook = (webhook or "").strip()
    if not webhook:
        webhook = getattr(Config, "SLACK_WEBHOOK_URL_V7_LIST_CREATOR", None) or ""
    return send_slack_webhook_optional(webhook, text_body)


def public_origin():
    """Base URL without trailing slash (for links shown in Slack)."""
    try:
        return str(getattr(Config, "PUBLIC_APP_ORIGIN", "") or "").strip().rstrip("/")
    except Exception:
        return ""


def _slack_bot_token() -> str:
    return (getattr(Config, "SLACK_TOKEN", None) or os.getenv("SLACK_TOKEN") or "").strip()


def _slack_client():
    token = _slack_bot_token()
    if not token:
        return None
    try:
        from slack_sdk import WebClient
    except Exception as err:
        log.warning("slack_sdk unavailable: %s", err)
        return None
    return WebClient(token=token)


def _user_name_blob(user: Dict[str, Any]) -> str:
    profile = user.get("profile") or {}
    parts = [
        user.get("name"),
        user.get("real_name"),
        profile.get("display_name"),
        profile.get("real_name"),
        profile.get("display_name_normalized"),
        profile.get("real_name_normalized"),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def find_slack_user_id(name_query: str) -> Optional[str]:
    """
    Resolve a Slack user id by username / real name / display name (case-insensitive).
    Prefer exact match on name or display_name; otherwise unique substring match.
    """
    query = (name_query or "").strip().lower()
    if not query:
        return None
    client = _slack_client()
    if client is None:
        return None
    try:
        users: List[Dict[str, Any]] = []
        cursor = None
        while True:
            kwargs = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.users_list(**kwargs)
            users.extend(resp.get("members") or [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
    except Exception as err:
        log.warning("Slack users.list failed: %s", err)
        return None

    exact = []
    partial = []
    for user in users:
        if user.get("deleted") or user.get("is_bot") or user.get("id") == "USLACKBOT":
            continue
        profile = user.get("profile") or {}
        candidates = {
            str(user.get("name") or "").lower(),
            str(user.get("real_name") or "").lower(),
            str(profile.get("display_name") or "").lower(),
            str(profile.get("real_name") or "").lower(),
        }
        candidates.discard("")
        if query in candidates:
            exact.append(user.get("id"))
        elif query in _user_name_blob(user):
            partial.append(user.get("id"))

    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        log.warning("Slack user lookup for %r matched multiple exact users", name_query)
        return exact[0]
    unique_partial = list(dict.fromkeys(partial))
    if len(unique_partial) == 1:
        return unique_partial[0]
    if len(unique_partial) > 1:
        log.warning("Slack user lookup for %r matched multiple users", name_query)
    return None


def send_slack_dm_optional(name_query: str, text: str, *, user_id: str = "") -> bool:
    """
    Open a DM with the named Slack user and post text. No-op without token / user / text.
    Prefer user_id when known (avoids users:read). Does not raise.
    """
    body = (text or "").strip()
    if not body:
        return False
    client = _slack_client()
    if client is None:
        log.warning("Slack DM skipped: SLACK_TOKEN not set or slack_sdk missing")
        return False
    uid = (user_id or "").strip() or find_slack_user_id(name_query)
    if not uid:
        log.warning("Slack DM skipped: no user matched %r (need users:read or SLACK_DM_USER_ID)", name_query)
        return False
    try:
        opened = client.conversations_open(users=[uid])
        channel = (opened.get("channel") or {}).get("id")
        if not channel:
            log.warning("Slack conversations.open returned no channel for %s", uid)
            return False
        client.chat_postMessage(channel=channel, text=body)
        return True
    except Exception as err:
        log.warning("Slack DM failed for %r: %s", name_query or uid, err)
        return False


def notify_tristen_optional(text_body: str) -> bool:
    """
    DM Tristen only.
    Optional env:
      SLACK_DM_USER      default tristen (name lookup; needs users:read)
      SLACK_DM_USER_ID   Slack member id U… (skips name lookup)
    Bot token also needs im:write (or conversations:write) for conversations.open.
    """
    name = (
        os.getenv("SLACK_DM_USER", "").strip()
        or getattr(Config, "SLACK_DM_USER", None)
        or "tristen"
    )
    user_id = (
        os.getenv("SLACK_DM_USER_ID", "").strip()
        or getattr(Config, "SLACK_DM_USER_ID", None)
        or ""
    )
    return send_slack_dm_optional(str(name), text_body, user_id=str(user_id or ""))


def post_slack_channel_optional(channel_id: str, text: str) -> bool:
    """Post plain text to a Slack channel id (C… / G…). No-op without token/channel."""
    body = (text or "").strip()
    channel = (channel_id or "").strip()
    if not body or not channel:
        return False
    client = _slack_client()
    if client is None:
        return False
    try:
        client.chat_postMessage(channel=channel, text=body)
        return True
    except Exception as err:
        log.warning("Slack channel post failed for %s: %s", channel, err)
        return False


def notify_attendance_channel_optional(text_body: str) -> bool:
    channel = (
        os.getenv("SLACK_ATTENDANCE_CHANNEL", "").strip()
        or getattr(Config, "SLACK_ATTENDANCE_CHANNEL", None)
        or ""
    )
    return post_slack_channel_optional(str(channel or ""), text_body)


def format_phone_wifi_event(event: Dict[str, Any]) -> str:
    """Build a Slack message for a phone join/leave event."""
    action = str(event.get("action") or "").lower()
    is_join = action == "join"
    verb = "joined" if is_join else "left"
    emoji = ":large_green_circle:" if is_join else ":red_circle:"
    name = (event.get("name") or "").strip() or "Unnamed device"
    hostname = (event.get("hostname") or "").strip()
    mac = (event.get("mac") or "").strip()
    ip = (event.get("ip") or "").strip() or "—"
    when = (event.get("time") or "").strip() or "—"
    category = (event.get("owner_category") or "").strip() or "not set"
    # Body time is Wi-Fi observe time (connect first-seen / disconnect last-seen), not Slack's post clock.
    time_label = "Connected at" if is_join else "Disconnected at (last seen)"
    lines = [
        f"{emoji} *Phone {verb} company Wi-Fi*",
        f"• *Name:* {name}" + (f" (`{hostname}`)" if hostname and hostname != name else ""),
        f"• *MAC:* `{mac}`",
        f"• *IP:* `{ip}`",
        f"• *{time_label}:* {when}",
        f"• *Category:* {category}",
    ]
    origin = public_origin()
    if origin:
        lines.append(f"• <{origin}/attendance_log?tab=phones|Open Attendance Log>")
    return "\n".join(lines)


def notify_phone_wifi_events(events: List[Dict[str, Any]]) -> int:
    """
    Post phone join/leave events to the attendance Slack channel only.
    Returns number of events delivered.
    """
    sent = 0
    for event in events or []:
        text = format_phone_wifi_event(event)
        if notify_attendance_channel_optional(text):
            sent += 1
    return sent
