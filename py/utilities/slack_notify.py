"""
Optional Slack Incoming Webhooks for V7 List Creator alerts.
Requires Config.SLACK_WEBHOOK_URL_V7_LIST_CREATOR (unless disabled).

Outbound-mail announcements use the same webhook unless SLACK_WEBHOOK_URL_MAIL_SENT is set.
"""

import logging
import os

import requests

from py.utilities.setting import Config


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
        logging.getLogger(__name__).warning("Slack webhook post failed: %s", e)
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
