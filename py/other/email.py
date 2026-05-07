"""
Flask blueprint: webmail UI over IMAP/SMTP (FatCow-compatible).
Templates live under templates/email/. Encrypted mailbox passwords in MySQL; Fernet key at EMAIL_DATA_DIR.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import base64
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr, parsedate_to_datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import quote, urlencode

import bleach
from bleach import linkify as bleach_linkify
import mailparser
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from imapclient import IMAPClient, SEEN

from mysql.connector import Error as MySQLError

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config, login_required

email_bp = Blueprint("email", __name__, url_prefix="/email")
_MAIL_SCHEMA_READY = False

# On-disk HTML / RFC822 mirrors under EMAIL_DATA_DIR: ~1 month; mail_message_meta rows pruned by inbox poll job.
MAIL_LOCAL_RETENTION_DAYS = 31
MAIL_META_RETENTION_DAYS = MAIL_LOCAL_RETENTION_DAYS
MAIL_COMBINED_INBOX_MAX_DAYS = 31
MAIL_POLL_BODY_PREFETCH_CAP = 18
MAIL_POLL_INITIAL_DELAY_S = 2.5
_EMAIL_DISK_PRUNE_MONO: float = 0.0
_EMAIL_DISK_PRUNE_INTERVAL_S = 4 * 3600
_OPEN_BOAT_LANE_TS: float = 0.0
_OPEN_BOAT_LANE_ROWS: List[Dict[str, Any]] = []
_BOAT_LANE_MAIL_SCAN_TS: float = 0.0
_BOAT_LANE_MAIL_SCAN_BLOBS: List[Dict[str, Any]] = []

# Sidebar "Allmail group": cross-mailbox IMAP merge + predicates (independent of per-inbox attachment filter).
_ALLMAIL_GROUP_IDS = frozenset(
    {"burlington", "container", "gabe", "ross", "macy", "tj"}
)
_BT_ROUTE_PICKUP_GROUPS = frozenset({"burlington", "gabe", "ross", "macy", "tj"})
_BURLINGTON_ROUTE_ID_RE = re.compile(r"\bCS\d{8,}\b", flags=re.IGNORECASE)
_BURLINGTON_PO_LIST_RE = re.compile(r"list\s+of\s+po\s*#?[’'`s]*\s*:\s*([0-9,\s]+)", flags=re.IGNORECASE)
_BURLINGTON_SHIP_START_RE = re.compile(r"ship\s+window\s+start\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", flags=re.IGNORECASE)
_BURLINGTON_SHIP_CANCEL_RE = re.compile(r"ship\s+window\s+cancel\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", flags=re.IGNORECASE)
_BURLINGTON_PICKUP_SLOT_RE = re.compile(r"\b(CS\d{8,})\b\s*-\s*([0-9]{1,2}/[0-9]{1,2})\s+at\s+([0-9]{1,2}:[0-9]{2})", flags=re.IGNORECASE)
_CONTAINER_SENDER_EMAILS = frozenset(
    {
        "op@fm-transportation.com",
        "op@fm-transportiton.com",  # common typo of transportation
        "ray@clearpointlog.com",
    }
)
# FM typo seen in the wild; still treat as Vicky / container lane.
_CONTAINER_OP_EMAIL_MARKERS = (
    "op@fm-transportation.com",
    "op@fm-transportiton.com",
)


def _container_ids_from_text(text: str) -> List[str]:
    """
    ISO 6346-style BIC: 4 letters + 7 digits (e.g. EGSU1425177).
    Allows optional space between letters and digits. Deduplicates, preserves order.
    """
    if not text:
        return []
    out: List[str] = []
    seen: set[str] = set()
    up = text.upper()
    for m in re.finditer(r"(?<![A-Z0-9])([A-Z]{4})\s*(\d{7})(?![0-9])", up):
        cid = m.group(1) + m.group(2)
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    compact = re.sub(r"[\s_]+", "", up)
    for m in re.finditer(r"(?<![A-Z0-9])([A-Z]{4}\d{7})(?![0-9])", compact):
        cid = m.group(1)
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _container_ids_from_subject_then_body(subject: str, body_scan: str) -> List[str]:
    ids = _container_ids_from_text(subject or "")
    if ids:
        return ids
    return _container_ids_from_text(body_scan or "")


def _container_origin_from_from_field(from_addr: str) -> str:
    low = (from_addr or "").lower()
    if any(m in low for m in _CONTAINER_OP_EMAIL_MARKERS):
        return "Vicky"
    if "ray@clearpointlog.com" in low:
        return "RAY"
    return ""


def _scan_text_from_raw_message(raw: bytes, *, max_chars: int = 250_000) -> str:
    """Plain + de-tagged HTML from a MIME message for container-id substring search."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return ""

    def pull_part(part) -> str:
        try:
            pl = part.get_payload(decode=True)
        except Exception:
            pl = part.get_payload()
        if pl is None:
            rawb = b""
        elif isinstance(pl, str):
            rawb = pl.encode("utf-8", errors="replace")
        else:
            rawb = pl if isinstance(pl, bytes) else bytes(pl)
        if not rawb:
            return ""
        try:
            s = rawb.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            s = rawb.decode("utf-8", errors="replace")
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/html":
            s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.IGNORECASE)
            s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.IGNORECASE)
            s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    chunks: List[str] = []
    total = 0
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            if (part.get_content_disposition() or "").lower() == "attachment":
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype not in ("text/plain", "text/html"):
                continue
            s = pull_part(part)
            if s:
                chunks.append(s)
                total += len(s)
                if total >= max_chars:
                    break
    else:
        ctype = (msg.get_content_type() or "").lower()
        if ctype in ("text/plain", "text/html") or not msg.is_multipart():
            try:
                body = msg.get_content()
            except Exception:
                body = msg.get_payload(decode=True)
            if isinstance(body, bytes):
                body = body.decode(msg.get_content_charset() or "utf-8", errors="replace")
            s = str(body or "")
            if ctype == "text/html":
                s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.IGNORECASE)
                s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.IGNORECASE)
                s = re.sub(r"<[^>]+>", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            if s:
                chunks.append(s)
    blob = "\n".join(chunks)
    return blob[:max_chars]


CONTAINER_BODY_SCAN_BUDGET = 55


def enrich_container_mail_rows(
    rows: List[Dict[str, Any]],
    *,
    body_scan_budget: int = CONTAINER_BODY_SCAN_BUDGET,
) -> None:
    """Mutates rows: container_ids, container_origin (IMAP body fetch only if subject has no ID)."""
    budget = body_scan_budget
    for m in rows:
        from_addr = str(m.get("from_addr") or "")
        subj = str(m.get("subject") or "")
        ids = _container_ids_from_text(subj)
        if not ids and budget > 0:
            budget -= 1
            aid = m.get("account_id")
            u = m.get("uid")
            if aid is not None and u is not None:
                sacc = _fetch_account_secret(int(aid))
                if sacc:
                    try:
                        raw = fetch_raw_message(sacc, int(u))
                        ids = _container_ids_from_text(_scan_text_from_raw_message(raw))
                    except Exception:
                        ids = []
        m["container_ids"] = ids
        m["container_origin"] = _container_origin_from_from_field(from_addr)


def _strip_html_to_scan_text(html: str, *, max_chars: int = 250_000) -> str:
    """Flatten cached HTML for container-id regex (same rules as MIME body scan)."""
    if not html:
        return ""
    s = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_chars]


def _serialized_container_ids_for_row(subject: str, body_html: str) -> Optional[str]:
    scan = _strip_html_to_scan_text(body_html or "")
    ids = _container_ids_from_subject_then_body(subject or "", scan)
    if not ids:
        return None
    try:
        return json.dumps(ids)[:768]
    except Exception:
        return None


def _parse_container_ids_json(blob: Optional[str]) -> List[str]:
    if not blob or not str(blob).strip():
        return []
    try:
        data = json.loads(blob)
        if isinstance(data, list):
            return [str(x).strip().upper() if isinstance(x, str) else str(x) for x in data if x]
        return []
    except Exception:
        return []


def _normalize_container_dedupe_text(s: str, *, max_len: int) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip().lower())
    return t[:max_len]


def _coalesced_account_labels(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for v in (
        row.get("account_labels"),
        row.get("account_label"),
        row.get("account_email"),
    ):
        if isinstance(v, list):
            for item in v:
                s = str(item or "").strip()
                if s and s not in out:
                    out.append(s)
        else:
            s = str(v or "").strip()
            if s and s not in out:
                out.append(s)
    return out


def _dedupe_container_combined_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Keep one copy for duplicates in combined container inbox:
    same normalized subject + same normalized body scan text.
    """
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for row in rows:
        subj_key = _normalize_container_dedupe_text(str(row.get("subject") or ""), max_len=300)
        body_scan = _strip_html_to_scan_text(str(row.get("body_html_sanitized") or ""))
        body_key = _normalize_container_dedupe_text(body_scan, max_len=4000)
        key = (subj_key, body_key)
        if key not in by_key:
            base = dict(row)
            labels = _coalesced_account_labels(base)
            base["account_labels"] = labels
            base["account_labels_display"] = " / ".join(labels)
            by_key[key] = base
            out.append(base)
            continue

        base = by_key[key]
        labels = base.get("account_labels") or []
        for lab in _coalesced_account_labels(row):
            if lab not in labels:
                labels.append(lab)
        base["account_labels"] = labels
        base["account_labels_display"] = " / ".join(labels)

        # Combined display semantics for duplicate rows across mailboxes.
        base["is_read"] = bool(base.get("is_read")) and bool(row.get("is_read"))
        base["has_attachments"] = bool(base.get("has_attachments")) or bool(row.get("has_attachments"))

        merged_ids: List[str] = []
        for src in (base.get("container_ids") or [], row.get("container_ids") or []):
            for cid in src:
                c = str(cid or "").strip().upper()
                if c and c not in merged_ids:
                    merged_ids.append(c)
        base["container_ids"] = merged_ids

    for row in out:
        row.pop("body_html_sanitized", None)
        row.pop("account_labels", None)
    return out


def list_container_group_from_cache(
    *,
    limit: int = 500,
    max_age_days: int = MAIL_COMBINED_INBOX_MAX_DAYS,
) -> List[Dict[str, Any]]:
    """
    Fast container lane: FM / ClearPoint senders from mail_message_meta (no IMAP).
    Uses stored container_ids_json when present, else parses subject only.
    Combined inbox: only rows within max_age_days (matches IMAP combined window).
    """
    conn = get_mysql_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                m.mail_account_id AS account_id,
                a.label AS account_label,
                a.email AS account_email,
                m.imap_uid AS uid,
                m.subject,
                m.from_addr,
                m.date_sent AS internal_date,
                m.is_read,
                m.has_attachments AS has_attachments,
                m.body_html_sanitized,
                m.container_ids_json
            FROM mail_message_meta m
            JOIN mail_accounts a ON a.id = m.mail_account_id
            WHERE
              (
                LOWER(COALESCE(m.from_addr, '')) LIKE %s
                OR LOWER(COALESCE(m.from_addr, '')) LIKE %s
                OR LOWER(COALESCE(m.from_addr, '')) LIKE %s
              )
              AND COALESCE(m.date_sent, m.body_cached_at, '1970-01-01')
                  >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)
            ORDER BY m.date_sent IS NULL, m.date_sent DESC, m.imap_uid DESC
            LIMIT %s
            """,
            (
                "%op@fm-transportation.com%",
                "%op@fm-transportiton.com%",
                "%ray@clearpointlog.com%",
                max(1, int(max_age_days)),
                limit,
            ),
        )
        rows = list(cur.fetchall())
        out: List[Dict[str, Any]] = []
        for row in rows:
            ha = row.get("has_attachments")
            row["has_attachments"] = bool(ha) if ha is not None else False
            row["uid"] = int(row["uid"])
            row["header_date"] = None
            row["addressed_here"] = True
            subj = row.get("subject") or ""
            cached = _parse_container_ids_json(row.get("container_ids_json"))
            if cached:
                row["container_ids"] = cached
            else:
                row["container_ids"] = _container_ids_from_text(subj)
            row["container_origin"] = _container_origin_from_from_field(str(row.get("from_addr") or ""))
            row.pop("container_ids_json", None)
            out.append(row)
        return _dedupe_container_combined_rows(out)
    except MySQLError:
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def _message_matches_mail_group(row: Dict[str, Any], group: str) -> bool:
    g = (group or "").strip().lower()
    subj = (row.get("subject") or "").lower()
    from_s = (row.get("from_addr") or "").lower()
    if g == "burlington":
        return "burlington" in subj
    if g == "gabe":
        return "gabe" in subj
    if g == "ross":
        return bool(re.search(r"(?<![a-z0-9])ross(?![a-z0-9])", subj))
    if g == "macy":
        return bool(re.search(r"(?<![a-z0-9])macy(?![a-z0-9])", subj))
    if g == "tj":
        return bool(re.search(r"(?<![a-z0-9])tj(?![a-z0-9])", subj))
    if g == "container":
        return any(addr in from_s for addr in _CONTAINER_SENDER_EMAILS)
    return False


def _normalize_bt_mode(v: str) -> str:
    s = (v or "").strip().lower()
    return "pickup" if s == "pickup" else "route"


def _message_matches_bt_mode(row: Dict[str, Any], mode: str) -> bool:
    """
    route: keep routing/non-pickup mails
    pickup: keep pickup-request mails
    """
    blob = " ".join(
        [
            str(row.get("subject") or ""),
            str(row.get("from_addr") or ""),
            str(row.get("container_origin") or ""),
        ]
    ).lower()
    is_pickup = bool(re.search(r"\bpick[\s\-]?up\b|\bpu\b|\bp/u\b", blob))
    if _normalize_bt_mode(mode) == "pickup":
        return is_pickup
    return not is_pickup


def _extract_burlington_route_fields(subject: str, body_html: str) -> Dict[str, Any]:
    text = _strip_html_to_scan_text(body_html or "")
    subject_s = str(subject or "")
    route_m = _BURLINGTON_ROUTE_ID_RE.search(subject_s) or _BURLINGTON_ROUTE_ID_RE.search(text)
    route_id = route_m.group(0).upper() if route_m else ""
    po_list: List[str] = []
    po_m = _BURLINGTON_PO_LIST_RE.search(text)
    if po_m:
        raw = po_m.group(1)
        for token in re.split(r"[,\s]+", raw):
            t = token.strip()
            if t.isdigit():
                po_list.append(t)
    # Deduplicate while preserving order
    seen_po = set()
    po_list = [p for p in po_list if not (p in seen_po or seen_po.add(p))]
    start_m = _BURLINGTON_SHIP_START_RE.search(text)
    cancel_m = _BURLINGTON_SHIP_CANCEL_RE.search(text)
    return {
        "burlington_route_id": route_id,
        "burlington_po_list": po_list,
        "burlington_ship_start": start_m.group(1) if start_m else "",
        "burlington_ship_cancel": cancel_m.group(1) if cancel_m else "",
    }


def _extract_burlington_pickup_fields(subject: str, body_html: str) -> Dict[str, Any]:
    text = _strip_html_to_scan_text(body_html or "")
    subj = str(subject or "")
    slot_m = _BURLINGTON_PICKUP_SLOT_RE.search(subj) or _BURLINGTON_PICKUP_SLOT_RE.search(text)
    route_m = _BURLINGTON_ROUTE_ID_RE.search(subj) or _BURLINGTON_ROUTE_ID_RE.search(text)
    route_id = route_m.group(0).upper() if route_m else ""
    slot_date = ""
    slot_time = ""
    if slot_m:
        route_id = slot_m.group(1).upper()
        slot_date = slot_m.group(2)
        slot_time = slot_m.group(3)
    title = route_id
    if route_id and (slot_date or slot_time):
        title = f"{route_id} - {slot_date} at {slot_time}".strip()
    return {
        "burlington_pickup_route_id": route_id,
        "burlington_pickup_slot_date": slot_date,
        "burlington_pickup_slot_time": slot_time,
        "burlington_pickup_title": title,
    }


def _enrich_burlington_route_rows(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    pairs: List[Tuple[int, int]] = []
    for m in rows:
        try:
            aid = int(m.get("account_id") or 0)
            uid_i = int(m.get("uid") or 0)
        except (TypeError, ValueError):
            continue
        if aid > 0 and uid_i > 0:
            pairs.append((aid, uid_i))
    if not pairs:
        return
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    body_map: Dict[Tuple[int, int], str] = {}
    try:
        cur = conn.cursor(dictionary=True)
        uniq_pairs = sorted(set(pairs))
        placeholders = ",".join(["(%s,%s)"] * len(uniq_pairs))
        params: List[Any] = []
        for aid, uid_i in uniq_pairs:
            params.extend([aid, uid_i])
        cur.execute(
            f"""
            SELECT mail_account_id AS aid, imap_uid AS uid, body_html_sanitized, subject
            FROM mail_message_meta
            WHERE (mail_account_id, imap_uid) IN ({placeholders})
            """,
            tuple(params),
        )
        for row in cur.fetchall():
            try:
                key = (int(row.get("aid") or 0), int(row.get("uid") or 0))
            except (TypeError, ValueError):
                continue
            body_map[key] = str(row.get("body_html_sanitized") or "")
    except MySQLError:
        return
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()

    for m in rows:
        try:
            key = (int(m.get("account_id") or 0), int(m.get("uid") or 0))
        except (TypeError, ValueError):
            continue
        parsed = _extract_burlington_route_fields(
            str(m.get("subject") or ""),
            body_map.get(key, ""),
        )
        m.update(parsed)


def _enrich_burlington_pickup_rows(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    pairs: List[Tuple[int, int]] = []
    for m in rows:
        try:
            aid = int(m.get("account_id") or 0)
            uid_i = int(m.get("uid") or 0)
        except (TypeError, ValueError):
            continue
        if aid > 0 and uid_i > 0:
            pairs.append((aid, uid_i))
    if not pairs:
        return
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    body_map: Dict[Tuple[int, int], str] = {}
    try:
        cur = conn.cursor(dictionary=True)
        uniq_pairs = sorted(set(pairs))
        placeholders = ",".join(["(%s,%s)"] * len(uniq_pairs))
        params: List[Any] = []
        for aid, uid_i in uniq_pairs:
            params.extend([aid, uid_i])
        cur.execute(
            f"""
            SELECT mail_account_id AS aid, imap_uid AS uid, body_html_sanitized
            FROM mail_message_meta
            WHERE (mail_account_id, imap_uid) IN ({placeholders})
            """,
            tuple(params),
        )
        for row in cur.fetchall():
            try:
                key = (int(row.get("aid") or 0), int(row.get("uid") or 0))
            except (TypeError, ValueError):
                continue
            body_map[key] = str(row.get("body_html_sanitized") or "")
    except MySQLError:
        return
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()

    for m in rows:
        try:
            key = (int(m.get("account_id") or 0), int(m.get("uid") or 0))
        except (TypeError, ValueError):
            continue
        parsed = _extract_burlington_pickup_fields(
            str(m.get("subject") or ""),
            body_map.get(key, ""),
        )
        m.update(parsed)


def _list_inbox_all_accounts_merged(
    *,
    per_account_limit: int = 150,
    recipient_strict: bool = True,
    max_total: int = 500,
    since_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Load recent inbox rows from every mail account and merge (newest first)."""
    merged: List[Dict[str, Any]] = []
    for pub in _fetch_accounts_public():
        aid = int(pub["id"])
        sec = _fetch_account_secret(aid)
        if not sec:
            continue
        try:
            rows = list_inbox_messages(sec, limit=per_account_limit, since_days=since_days)
        except Exception:
            continue
        if recipient_strict:
            rows = [m for m in rows if m.get("addressed_here", True)]
        for m in rows:
            m["account_id"] = aid
            m["account_label"] = pub.get("label")
            m["account_email"] = pub.get("email")
        merged.extend(rows)

    def _sort_key(r: Dict[str, Any]) -> datetime:
        return r.get("internal_date") or r.get("header_date") or datetime.min

    merged.sort(key=_sort_key, reverse=True)
    return merged[:max_total]


def _ensure_mail_schema() -> None:
    """Create mail tables if they don't exist yet."""
    global _MAIL_SCHEMA_READY
    if _MAIL_SCHEMA_READY:
        return
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_accounts (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                label VARCHAR(128) NOT NULL,
                email VARCHAR(255) NOT NULL,
                imap_host VARCHAR(255) NOT NULL DEFAULT 'imap.fatcow.com',
                imap_port INT UNSIGNED NOT NULL DEFAULT 993,
                smtp_host VARCHAR(255) NOT NULL DEFAULT 'smtp.fatcow.com',
                smtp_port INT UNSIGNED NOT NULL DEFAULT 465,
                encrypted_password VARBINARY(4096) NOT NULL,
                inbox_folder VARCHAR(191) NOT NULL DEFAULT 'INBOX',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_mail_accounts_email (email)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_message_meta (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                mail_account_id INT UNSIGNED NOT NULL,
                folder VARCHAR(191) NOT NULL DEFAULT 'INBOX',
                imap_uid BIGINT UNSIGNED NOT NULL,
                message_id VARCHAR(512) NULL,
                subject VARCHAR(1024) NULL,
                from_addr VARCHAR(512) NULL,
                date_sent DATETIME NULL,
                is_read TINYINT(1) NOT NULL DEFAULT 0,
                has_attachments TINYINT(1) NULL DEFAULT NULL,
                body_html_sanitized MEDIUMTEXT NULL,
                body_cached_at DATETIME NULL,
                container_ids_json VARCHAR(768) NULL DEFAULT NULL,
                INDEX idx_mail_msg_account_folder (mail_account_id, folder),
                INDEX idx_mail_msg_date (mail_account_id, folder, date_sent),
                UNIQUE KEY uq_mail_msg_uid (mail_account_id, folder, imap_uid),
                CONSTRAINT fk_mail_msg_account
                    FOREIGN KEY (mail_account_id) REFERENCES mail_accounts (id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_boat_date_overrides (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                container VARCHAR(64) NOT NULL,
                bol VARCHAR(128) NOT NULL DEFAULT '',
                override_date_md VARCHAR(8) NOT NULL,
                updated_by VARCHAR(128) NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_mail_boat_date_override (container, bol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.commit()
        try:
            cur.execute(
                "ALTER TABLE mail_message_meta ADD COLUMN has_attachments TINYINT(1) NULL DEFAULT NULL AFTER is_read"
            )
            conn.commit()
        except MySQLError:
            conn.rollback()
        try:
            cur.execute(
                "ALTER TABLE mail_message_meta ADD COLUMN container_ids_json VARCHAR(768) NULL DEFAULT NULL AFTER body_cached_at"
            )
            conn.commit()
        except MySQLError:
            conn.rollback()
        _MAIL_SCHEMA_READY = True
    except MySQLError:
        # Keep app responsive; normal route handlers will show DB errors if any.
        pass
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def _maybe_prune_local_email_mirrors() -> None:
    """Remove stale on-disk body/rfc822 copies; DB cache rows stay. Throttled."""
    global _EMAIL_DISK_PRUNE_MONO
    import time

    now = time.monotonic()
    if now - _EMAIL_DISK_PRUNE_MONO < _EMAIL_DISK_PRUNE_INTERVAL_S:
        return
    _EMAIL_DISK_PRUNE_MONO = now
    try:
        prune_local_email_mirror_files(retention_days=MAIL_LOCAL_RETENTION_DAYS)
    except Exception:
        pass


@email_bp.before_request
def _bootstrap_email_dependencies() -> None:
    _email_root()
    _ensure_mail_schema()
    _maybe_prune_local_email_mirrors()


def _email_root() -> Path:
    root = Path(Config.EMAIL_DATA_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fernet() -> Fernet:
    """Symmetric key for mailbox passwords; prefers MAIL_FERNET_KEY env, else EMAIL_DATA_DIR/.fernet_key."""
    env_key = (os.getenv("MAIL_FERNET_KEY") or "").strip()
    if env_key:
        return Fernet(env_key.encode())
    key_path = _email_root() / ".fernet_key"
    if key_path.exists():
        return Fernet(key_path.read_bytes().strip())
    key = Fernet.generate_key()
    key_path.write_bytes(key + b"\n")
    return Fernet(key)


def encrypt_secret(plain: str) -> bytes:
    return _fernet().encrypt(plain.encode("utf-8"))


def decrypt_secret(blob: bytes) -> str:
    try:
        if blob is None:
            raise RuntimeError("Missing encrypted password in mail_accounts")
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        elif isinstance(blob, bytearray):
            blob = bytes(blob)
        elif isinstance(blob, str):
            blob = blob.encode("utf-8")
        elif not isinstance(blob, (bytes, str)):
            raise RuntimeError(f"Unsupported encrypted password type: {type(blob).__name__}")
        return _fernet().decrypt(blob).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Invalid encrypted password. Re-save mailbox password in /email/accounts/manage or check MAIL_FERNET_KEY."
        ) from exc


def _accounts_admin_required():
    return session.get("username") == "yeti"


def accounts_admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not _accounts_admin_required():
            flash("Only yeti can manage mail accounts.")
            return redirect(url_for("home"))
        return f(*args, **kwargs)

    return wrapped


_BLEACH_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p",
    "div",
    "span",
    "br",
    "hr",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "caption",
    "colgroup",
    "col",
    "img",
    "a",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "figure",
    "figcaption",
    "svg",
    "path",
    "circle",
    "meta",
    "title",
}

_BLEACH_ATTR = {
    "*": ["class", "id"],
    "a": ["href", "title", "name", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan", "align", "valign"],
    "th": ["colspan", "rowspan", "align", "valign"],
    "table": ["border", "cellpadding", "cellspacing", "width"],
    "svg": ["width", "height", "viewBox", "xmlns"],
    "path": ["d", "fill"],
    "circle": ["cx", "cy", "r", "fill"],
}
_BLEACH_PROTOCOLS = set(bleach.sanitizer.ALLOWED_PROTOCOLS) | {"data", "cid"}


def sanitize_html_fragment(html: str) -> str:
    if not html:
        return ""
    # Remove style/script blocks entirely so CSS text doesn't leak into visible body.
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = bleach.clean(
        html,
        tags=_BLEACH_TAGS,
        attributes=_BLEACH_ATTR,
        protocols=_BLEACH_PROTOCOLS,
        strip=True,
    )
    # Some malformed bodies still leak inline CSS text like "P {margin-top:0;margin-bottom:0;}".
    cleaned = re.sub(r"\bP\s*\{\s*margin-top\s*:\s*0\s*;\s*margin-bottom\s*:\s*0\s*;\s*\}", "", cleaned, flags=re.IGNORECASE)
    cleaned = bleach_linkify(cleaned)
    return cleaned


def _decode_mime_header(val: Optional[Union[bytes, str]]) -> str:
    """Decode RFC 2047 encoded-words (e.g. =?gb2312?B?...?=) from IMAP envelope/header bytes.

    Python's decode_header() only accepts str; IMAPClient envelope fields are often bytes,
    which previously caused TypeError and fell through to a UTF-8 decode of the raw ASCII,
    leaving undecoded =?...?= text in the inbox list.
    """
    if val is None:
        return ""
    try:
        if isinstance(val, bytes):
            hdr = val.decode("ascii", errors="replace")
        else:
            hdr = str(val)
        pieces: List[str] = []
        for fragment, charset in decode_header(hdr):
            if isinstance(fragment, bytes):
                enc = (charset or "utf-8").strip() if charset else ""
                el = enc.lower()
                candidates = [charset] if charset else []
                if el in ("gb2312", "gbk", "gb18030", "hz-gb-2312", "gb_2312_80"):
                    candidates = ["gb18030", "gbk", "gb2312", "utf-8"]
                elif not candidates:
                    candidates = ["utf-8"]
                decoded = False
                for c in candidates:
                    if not c:
                        continue
                    try:
                        pieces.append(fragment.decode(c, errors="strict"))
                        decoded = True
                        break
                    except (LookupError, UnicodeDecodeError):
                        continue
                if not decoded:
                    pieces.append(fragment.decode("utf-8", errors="replace"))
            else:
                pieces.append(str(fragment))
        return "".join(pieces)
    except Exception:
        try:
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace")
            return str(val)
        except Exception:
            return ""


def _addresses_pretty(raw_addrs) -> str:
    if not raw_addrs:
        return ""
    parts: List[str] = []
    for ad in raw_addrs:
        try:
            name_b = getattr(ad, "name", None)
            mailbox_b = getattr(ad, "mailbox", None)
            host_b = getattr(ad, "host", None)
            name = _decode_mime_header(name_b) if name_b else ""
            mailbox = mailbox_b.decode("utf-8", errors="replace") if isinstance(mailbox_b, bytes) else str(mailbox_b or "")
            host = host_b.decode("utf-8", errors="replace") if isinstance(host_b, bytes) else str(host_b or "")
            addr = f"{mailbox}@{host}".strip("@")
            if name:
                parts.append(formataddr((name, addr)))
            else:
                parts.append(addr)
        except Exception:
            parts.append(str(ad))
    return ", ".join(parts)


def _normalize_dt(val: Optional[datetime]) -> Optional[datetime]:
    if val is None:
        return None
    if val.tzinfo is not None:
        return val.astimezone(timezone.utc).replace(tzinfo=None)
    return val


def _fetch_accounts_public() -> List[Dict[str, Any]]:
    conn = get_mysql_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, label, email, imap_host, imap_port, smtp_host, smtp_port, inbox_folder
            FROM mail_accounts
            ORDER BY label ASC, id ASC
            """
        )
        return list(cur.fetchall())
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def _strip_secret(acc: Dict[str, Any]) -> Dict[str, Any]:
    safe = dict(acc)
    safe.pop("encrypted_password", None)
    return safe


def _fetch_account_secret(account_id: int) -> Optional[Dict[str, Any]]:
    conn = get_mysql_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, label, email, imap_host, imap_port, smtp_host, smtp_port,
                   inbox_folder, encrypted_password
            FROM mail_accounts
            WHERE id = %s
            LIMIT 1
            """,
            (account_id,),
        )
        row = cur.fetchone()
        return row
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def _imap_connect(acc: Dict[str, Any]) -> IMAPClient:
    pwd = decrypt_secret(acc["encrypted_password"])
    client = IMAPClient(
        acc["imap_host"],
        port=int(acc["imap_port"]),
        ssl=True,
        timeout=45,
    )
    client.login(acc["email"], pwd)
    return client


def _smtp_send(acc: Dict[str, Any], *, to_addrs: List[str], subject: str, body_text: str, html_body: Optional[str], extra_headers: Optional[Dict[str, str]]):
    pwd = decrypt_secret(acc["encrypted_password"])
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = acc["email"]
    msg["To"] = ", ".join(to_addrs)
    if extra_headers:
        for k, v in extra_headers.items():
            if v:
                msg[k] = v
    msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL(acc["smtp_host"], int(acc["smtp_port"]), timeout=45) as s:
        s.login(acc["email"], pwd)
        s.sendmail(acc["email"], to_addrs, msg.as_string())


def _folder_flags_seen(flags) -> bool:
    if not flags:
        return False
    try:
        return SEEN in flags
    except Exception:
        for f in flags:
            fs = f.decode("utf-8", errors="replace") if isinstance(f, bytes) else str(f)
            if fs.endswith("Seen"):
                return True
    return False


def _imap_structure_has_attachment_disposition(obj) -> bool:
    """True if IMAP body extension includes (DISPOSITION (ATTACHMENT ...))."""
    if obj is None or isinstance(obj, (bytes, int, str, float)):
        return False
    # imapclient.BodyData subclasses tuple
    if isinstance(obj, (list, tuple)):
        if (
            len(obj) >= 2
            and isinstance(obj[0], bytes)
            and obj[0].upper() == b"DISPOSITION"
            and isinstance(obj[1], (list, tuple))
            and obj[1]
            and isinstance(obj[1][0], bytes)
            and obj[1][0].upper() == b"ATTACHMENT"
        ):
            return True
        for x in obj:
            if _imap_structure_has_attachment_disposition(x):
                return True
    return False


def _imap_structure_has_inline_disposition(obj) -> bool:
    if obj is None or isinstance(obj, (bytes, int, str, float)):
        return False
    if isinstance(obj, (list, tuple)):
        if (
            len(obj) >= 2
            and isinstance(obj[0], bytes)
            and obj[0].upper() == b"DISPOSITION"
            and isinstance(obj[1], (list, tuple))
            and obj[1]
            and isinstance(obj[1][0], bytes)
            and obj[1][0].upper() == b"INLINE"
        ):
            return True
        for x in obj:
            if _imap_structure_has_inline_disposition(x):
                return True
    return False


def _bodystructure_leaf_real_attachment(tup: tuple) -> bool:
    """Single-part BODYSTRUCTURE: exclude inline/CID-style images; keep real files."""
    if not tup or not isinstance(tup[0], bytes):
        return False
    major = tup[0].upper()
    if _imap_structure_has_attachment_disposition(tup):
        return True
    if major == b"TEXT":
        return False
    if _imap_structure_has_inline_disposition(tup):
        return False
    if major == b"IMAGE":
        return False
    if major in (b"APPLICATION", b"MESSAGE", b"AUDIO", b"VIDEO"):
        return True
    return False


def _bodystructure_has_attachments(bs) -> bool:
    """True if message has real attachments (not inline HTML images / related CID parts)."""
    if bs is None:
        return False
    try:
        if getattr(bs, "is_multipart", False):
            for p in bs[0]:
                if _bodystructure_has_attachments(p):
                    return True
            return False
        return _bodystructure_leaf_real_attachment(tuple(bs))
    except Exception:
        return False


def _sanitize_lane_param(s: str, *, max_len: int = 96) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    t = re.sub(r"[^\w\-. /]+", "", t, flags=re.UNICODE).strip()
    return t[:max_len]


def _get_open_container_bol_rows(*, include_landed: bool = False, ttl_s: float = 120.0) -> List[Dict[str, Any]]:
    """Google Sheet + OMS landed check for open-container lane. Cached briefly."""
    global _OPEN_BOAT_LANE_TS, _OPEN_BOAT_LANE_ROWS
    import time

    now = time.monotonic()
    if _OPEN_BOAT_LANE_ROWS and (now - _OPEN_BOAT_LANE_TS) < ttl_s:
        if include_landed:
            return list(_OPEN_BOAT_LANE_ROWS)
        return [r for r in _OPEN_BOAT_LANE_ROWS if str(r.get("status") or "").strip() != "✔️827✔️"]
    rows = _load_open_container_bol_rows_uncached()
    _OPEN_BOAT_LANE_TS = now
    _OPEN_BOAT_LANE_ROWS = rows
    if include_landed:
        return list(rows)
    return [r for r in rows if str(r.get("status") or "").strip() != "✔️827✔️"]


def _load_open_container_bol_rows_uncached() -> List[Dict[str, Any]]:
    """Mirror /boat data source: Sheets + bin_pur RCVD; includes landed/open statuses."""
    try:
        import pandas as pd
        from py.Toolkits.boat import GOOGLE_SHEETS_URL, get_db_connection
    except Exception:
        return []
    try:
        df = pd.read_csv(GOOGLE_SHEETS_URL)
    except Exception:
        return []
    if "FDC container" not in df.columns or "BOL NUM" not in df.columns:
        return []

    def _convert_date(date_val: Any) -> str:
        if pd.isnull(date_val) or date_val == "":
            return ""
        if isinstance(date_val, (int, float)):
            try:
                return (pd.to_datetime("1899-12-30") + pd.to_timedelta(date_val, "D")).strftime("%m/%d/%Y")
            except Exception:
                return ""
        try:
            return pd.to_datetime(date_val).strftime("%m/%d/%Y")
        except Exception:
            return str(date_val)

    cols = ["FDC container", "BOL NUM", "Memo", "Exam", "ETA", "ATA", "RCD"]
    df_cleaned = df[[c for c in cols if c in df.columns]].copy()
    if "Memo" not in df_cleaned.columns:
        df_cleaned["Memo"] = ""
    if "Exam" not in df_cleaned.columns:
        df_cleaned["Exam"] = ""

    def _combine_memo_exam(row: Any) -> str:
        memo = str(row["Memo"]).strip() if pd.notna(row["Memo"]) and str(row["Memo"]).strip() else ""
        exam = str(row["Exam"]).strip() if pd.notna(row["Exam"]) and str(row["Exam"]).strip() else ""
        if memo and exam:
            return f"{memo} - {exam}"
        return memo or exam

    df_cleaned["MEMO"] = df_cleaned.apply(_combine_memo_exam, axis=1)
    rename_map = {"FDC container": "container", "BOL NUM": "link", "ETA": "Dennis ETA", "ATA": "pod_ata_at"}
    df_cleaned = df_cleaned.rename(columns={k: v for k, v in rename_map.items() if k in df_cleaned.columns})
    for c in ("Dennis ETA", "pod_ata_at", "RCD"):
        if c in df_cleaned.columns:
            df_cleaned[c] = df_cleaned[c].apply(_convert_date)
    keep_cols = [c for c in ("container", "link", "Dennis ETA", "pod_ata_at", "RCD", "MEMO") if c in df_cleaned.columns]
    df_cleaned = df_cleaned[keep_cols]

    # Sort container IDs by numeric suffix (F261102 > F26199), not lexicographic text.
    def _container_sort_num(v: Any) -> int:
        s = str(v or "").strip().upper()
        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return -1
        return -1

    df_cleaned["_container_num"] = df_cleaned["container"].apply(_container_sort_num)
    df_cleaned["_container_txt"] = df_cleaned["container"].astype(str).str.upper()
    df_cleaned = df_cleaned.sort_values(
        by=["_container_num", "_container_txt"], ascending=[False, False]
    ).head(30)
    df_cleaned = df_cleaned.drop(columns=["_container_num", "_container_txt"], errors="ignore")
    df_cleaned["container_raw"] = df_cleaned["container"]
    df_cleaned = df_cleaned[df_cleaned["container_raw"].notna() & (df_cleaned["container_raw"].astype(str).str.strip() != "")]
    container_list = [str(x).strip() for x in df_cleaned["container_raw"].tolist() if str(x).strip()]
    if not container_list:
        return []

    rcvd_lookup: Dict[str, Any] = {}
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        placeholders = ",".join(["?" for _ in container_list])
        cur.execute(
            f"SELECT PUR_NUM, SUM(RCVD_QTY) as total_rcvd FROM dbo.bin_pur WHERE PUR_NUM IN ({placeholders}) GROUP BY PUR_NUM",
            container_list,
        )
        for row in cur.fetchall():
            rcvd_lookup[str(row[0]).strip()] = row[1]
    except Exception:
        rcvd_lookup = {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for index, row in df_cleaned.iterrows():
        cr = str(row["container_raw"]).strip()
        rcvd_qty = rcvd_lookup.get(cr)
        if rcvd_qty is not None and rcvd_qty > 0:
            df_cleaned.at[index, "Status"] = "✔️827✔️"
        elif rcvd_qty is not None and rcvd_qty == 0:
            df_cleaned.at[index, "Status"] = "🚢ocean🚢"
        else:
            df_cleaned.at[index, "Status"] = "🔄upload🔄"

    for index, row in df_cleaned.iterrows():
        if str(row.get("Status", "")).strip() == "✔️827✔️":
            continue
        internal_eta = str(row.get("Dennis ETA", "")).strip()
        internal_rcd = str(row.get("RCD", "")).strip()
        if internal_eta and not internal_rcd:
            df_cleaned.at[index, "Status"] = "🚢ocean🚢"

    df_cleaned = df_cleaned.fillna("")
    out: List[Dict[str, Any]] = []
    for _, row in df_cleaned.iterrows():
        out.append(
            {
                "container": str(row.get("container") or "").strip(),
                "bol": str(row.get("link") or "").strip(),
                "status": str(row.get("Status") or "").strip()[:80],
            }
        )
    return [r for r in out if r["container"]]


_SLASH_MD_DATE_RE = re.compile(r"(?<![\d/])(\d{1,2}/\d{1,2})(?![\d/])")


def _fdcef_container_search_url(container_id: str) -> str:
    """FDCEF opens search with container id plus two trailing spaces (matches legacy site behavior)."""
    c = (container_id or "").strip()
    if not c:
        return "https://www.fdcef.com/container_search"
    return "https://www.fdcef.com/container_search?container_id=" + quote(c + "  ", safe="")


def _token_in_text_haystack(haystack_lower: str, token: str) -> bool:
    if not token:
        return False
    tok = token.strip().lower()
    if len(tok) >= 5:
        return tok in haystack_lower
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", haystack_lower))


def _compact_upper(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").upper())


def _boat_override_key(container: str, bol: str) -> Tuple[str, str]:
    return _compact_upper(container), _compact_upper(bol)


def _fetch_boat_date_overrides() -> Dict[Tuple[str, str], str]:
    conn = get_mysql_connection()
    if not conn:
        return {}
    cur = None
    out: Dict[Tuple[str, str], str] = {}
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT container, bol, override_date_md
            FROM mail_boat_date_overrides
            """
        )
        for row in cur.fetchall():
            container = str(row.get("container") or "").strip()
            bol = str(row.get("bol") or "").strip()
            d = str(row.get("override_date_md") or "").strip()
            k = _boat_override_key(container, bol)
            if k[0] and d:
                out[k] = d
    except MySQLError:
        return {}
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
    return out


def _first_slash_md_date(text: str) -> str:
    m = _SLASH_MD_DATE_RE.search(text or "")
    return m.group(1) if m else ""


def _preferred_slash_md_date(text: str) -> str:
    """
    Prefer schedule/delivery-context dates over generic dates in signatures/disclaimers.
    Example: "changed to deliver on 05/08" should beat earlier "05/06" mentions.
    """
    blob = text or ""
    blob_l = blob.lower()
    # Try strong context patterns first.
    context_patterns = [
        r"(?:changed\s+to\s+)?deliver(?:y)?(?:\s+\w+){0,4}\s+(?:on|for|to)\s*(\d{1,2}/\d{1,2})",
        r"(?:will|can|cannot|can't|could|scheduled|rescheduled)(?:\s+\w+){0,5}\s+(?:on|for|to)\s*(\d{1,2}/\d{1,2})",
        r"(?:eta|appointment|appt)(?:\s+\w+){0,4}\s*(?:on|for|to)?\s*(\d{1,2}/\d{1,2})",
    ]
    for pat in context_patterns:
        m = re.search(pat, blob_l, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return _first_slash_md_date(blob)


def _boat_lane_mail_scan_blobs_cached(*, ttl_s: float = 120.0) -> List[Dict[str, Any]]:
    """Flatten subject + cached HTML body for FM/ClearPoint mails (boat lane hints)."""
    global _BOAT_LANE_MAIL_SCAN_TS, _BOAT_LANE_MAIL_SCAN_BLOBS
    import time

    now = time.monotonic()
    if _BOAT_LANE_MAIL_SCAN_BLOBS and (now - _BOAT_LANE_MAIL_SCAN_TS) < ttl_s:
        return _BOAT_LANE_MAIL_SCAN_BLOBS

    conn = get_mysql_connection()
    out: List[Dict[str, Any]] = []
    if not conn:
        _BOAT_LANE_MAIL_SCAN_TS = now
        _BOAT_LANE_MAIL_SCAN_BLOBS = []
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT m.subject, m.body_html_sanitized, m.date_sent, m.body_cached_at
            FROM mail_message_meta m
            JOIN mail_accounts a ON a.id = m.mail_account_id
            WHERE
              (
                LOWER(COALESCE(m.from_addr, '')) LIKE %s
                OR LOWER(COALESCE(m.from_addr, '')) LIKE %s
                OR LOWER(COALESCE(m.from_addr, '')) LIKE %s
              )
              AND COALESCE(m.date_sent, m.body_cached_at, '1970-01-01')
                  >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)
            ORDER BY m.date_sent IS NULL, m.date_sent DESC, m.imap_uid DESC
            LIMIT 1200
            """,
            (
                "%op@fm-transportation.com%",
                "%op@fm-transportiton.com%",
                "%ray@clearpointlog.com%",
                max(1, int(MAIL_COMBINED_INBOX_MAX_DAYS)),
            ),
        )
        for row in cur.fetchall():
            subj = str(row.get("subject") or "")
            body_html = str(row.get("body_html_sanitized") or "")
            flat = subj + "\n" + _strip_html_to_scan_text(body_html)
            ds = row.get("date_sent")
            if not isinstance(ds, datetime):
                ds = row.get("body_cached_at")
            sort_dt = ds if isinstance(ds, datetime) else datetime.min
            out.append({"flat": flat, "flat_lower": flat.lower(), "sort_dt": sort_dt})
    except MySQLError:
        out = []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()

    _BOAT_LANE_MAIL_SCAN_TS = now
    _BOAT_LANE_MAIL_SCAN_BLOBS = out
    return out


def _enrich_boat_lane_rows_with_mail_hints(rows: List[Dict[str, Any]]) -> None:
    """Mutates rows: fdcef_container_url, bol_has_mail, mail_body_date_hint."""
    if not rows:
        return
    blobs = _boat_lane_mail_scan_blobs_cached()
    overrides = _fetch_boat_date_overrides()
    for br in rows:
        ctr = str(br.get("container") or "").strip()
        bol = str(br.get("bol") or "").strip()
        br["fdcef_container_url"] = _fdcef_container_search_url(ctr)
        br["bol_has_mail"] = False
        br["mail_body_date_hint"] = ""
        br["mail_body_date_manual"] = False
        if not blobs:
            pass
        else:
            ctr_c = _compact_upper(ctr)
            bol_hit_any = False
            if bol:
                for em in blobs:
                    if _token_in_text_haystack(em["flat_lower"], bol):
                        bol_hit_any = True
                        break
            br["bol_has_mail"] = bol_hit_any

            related: List[Dict[str, Any]] = []
            for em in blobs:
                fl = em["flat_lower"]
                ok_bol = bol and _token_in_text_haystack(fl, bol)
                ok_ctr = ctr_c and ctr_c in _compact_upper(em["flat"])
                if ok_bol or ok_ctr:
                    related.append(em)
            related.sort(key=lambda x: x.get("sort_dt") or datetime.min, reverse=True)
            hint = ""
            for em in related:
                cand = _preferred_slash_md_date(em["flat"])
                if cand:
                    hint = cand
                    break
            br["mail_body_date_hint"] = hint

        override_md = overrides.get(_boat_override_key(ctr, bol))
        if override_md:
            br["mail_body_date_hint"] = override_md
            br["mail_body_date_manual"] = True


def _email_row_matches_container_focus(m: Dict[str, Any], fdc: str, bol: str) -> bool:
    if not (fdc or bol):
        return True
    parts = [str(m.get("subject") or ""), str(m.get("from_addr") or "")]
    for cid in m.get("container_ids") or []:
        parts.append(str(cid))
    hay = " ".join(parts).lower()
    compact = re.sub(r"\s+", "", hay)
    ok_f, ok_b = True, True
    if fdc:
        f = fdc.strip().lower()
        ff = re.sub(r"\s+", "", f)
        ok_f = ff in compact or f in hay
    if bol:
        b = bol.strip().lower()
        ok_b = b in hay
    if fdc and bol:
        return ok_f and ok_b
    if fdc:
        return ok_f
    if bol:
        return ok_b
    return True


def _inbox_query_suffix(
    *,
    q: str,
    scope: str,
    list_filter: str,
    all_in_folder: bool = False,
    mail_group: Optional[str] = None,
    fdc: Optional[str] = None,
    bol: Optional[str] = None,
    show_landed: bool = False,
    bt_mode: Optional[str] = None,
) -> str:
    params: Dict[str, str] = {}
    if q and q.strip():
        params["q"] = q.strip()
    if scope and scope != "current":
        params["scope"] = scope
    lf = (list_filter or "").strip().lower()
    if lf:
        params["filter"] = lf
    if all_in_folder:
        params["all_in_folder"] = "1"
    mg = (mail_group or "").strip().lower()
    if mg in _ALLMAIL_GROUP_IDS:
        params["mail_group"] = mg
    fs = _sanitize_lane_param(fdc or "")
    if fs:
        params["fdc"] = fs
    bs = _sanitize_lane_param(bol or "")
    if bs:
        params["bol"] = bs
    if show_landed:
        params["show_landed"] = "1"
    bm = _normalize_bt_mode(bt_mode or "")
    if bm == "pickup":
        params["bt_mode"] = "pickup"
    return "?" + urlencode(params) if params else ""


def _sidebar_poll_query_string() -> str:
    """Query string for `/inbox/poll` matching the thread-view sidebar (no search / attachment filters)."""
    d: Dict[str, str] = {"sidebar": "1"}
    if request.args.get("all_in_folder") == "1":
        d["all_in_folder"] = "1"
    mg = (request.args.get("mail_group") or "").strip().lower()
    if mg in _ALLMAIL_GROUP_IDS:
        d["mail_group"] = mg
    if mg in _BT_ROUTE_PICKUP_GROUPS:
        bm = _normalize_bt_mode(request.args.get("bt_mode") or "")
        if bm == "pickup":
            d["bt_mode"] = "pickup"
    if mg == "container":
        fdc = _sanitize_lane_param(request.args.get("fdc") or "")
        bol = _sanitize_lane_param(request.args.get("bol") or "")
        show_landed = request.args.get("show_landed") in ("1", "true", "yes")
        if fdc:
            d["fdc"] = fdc
        if bol:
            d["bol"] = bol
        if show_landed:
            d["show_landed"] = "1"
    return urlencode(d)


def _list_inbox_from_mail_meta(acc: Dict[str, Any], *, limit: int = 150) -> List[Dict[str, Any]]:
    """Fast path: last-known inbox rows from MySQL (body may be cached asynchronously)."""
    conn = get_mysql_connection()
    if not conn:
        return []
    aid = int(acc["id"])
    folder = acc.get("inbox_folder") or "INBOX"
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT imap_uid, subject, from_addr, date_sent, is_read, has_attachments
            FROM mail_message_meta
            WHERE mail_account_id = %s AND folder = %s
            ORDER BY COALESCE(date_sent, body_cached_at, CAST('1970-01-01' AS DATETIME)) DESC,
                     imap_uid DESC
            LIMIT %s
            """,
            (aid, folder, max(1, int(limit))),
        )
        raw_rows = list(cur.fetchall())
    except MySQLError:
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        try:
            uid_i = int(r["imap_uid"])
        except (TypeError, ValueError):
            continue
        ds = r.get("date_sent")
        if isinstance(ds, datetime):
            ds_norm: Optional[datetime] = _normalize_dt(ds)
        elif isinstance(ds, date):
            ds_norm = _normalize_dt(datetime.combine(ds, datetime.min.time()))
        else:
            ds_norm = None
        out.append(
            {
                "uid": uid_i,
                "subject": (r.get("subject") or "").strip() or "(no subject)",
                "from_addr": str(r.get("from_addr") or ""),
                "internal_date": ds_norm,
                "header_date": None,
                "is_read": bool(r.get("is_read")),
                "has_attachments": bool(r.get("has_attachments")),
                "message_id": "",
                "addressed_here": True,
                "account_id": aid,
                "account_label": acc.get("label"),
                "account_email": acc.get("email"),
            }
        )
    return out


def _mail_meta_needs_body(account_id: int, folder: str, uid: int) -> bool:
    conn = get_mysql_connection()
    if not conn:
        return True
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT body_html_sanitized, body_cached_at
            FROM mail_message_meta
            WHERE mail_account_id = %s AND folder = %s AND imap_uid = %s
            LIMIT 1
            """,
            (account_id, folder, int(uid)),
        )
        row = cur.fetchone()
        if not row:
            return True
        blob = row.get("body_html_sanitized")
        if blob is None or not str(blob).strip():
            return True
        return False
    except MySQLError:
        return True
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def prune_mail_message_meta_older_than(*, retention_days: int = MAIL_META_RETENTION_DAYS) -> int:
    """Drop cached metadata/bodies older than retention_days (local cache only; IMAP untouched)."""
    conn = get_mysql_connection()
    if not conn:
        return 0
    cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM mail_message_meta
            WHERE (date_sent IS NOT NULL AND date_sent < %s)
               OR (date_sent IS NULL AND body_cached_at IS NOT NULL AND body_cached_at < %s)
            """,
            (cutoff, cutoff),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    except MySQLError:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def _prefetch_mail_bodies_for_poll(messages: List[Dict[str, Any]], *, cap: int) -> int:
    """Download RFC822 + sanitize HTML for rows missing body cache (bounded per poll)."""
    done = 0
    seen: Set[Tuple[int, int]] = set()
    for m in messages:
        if done >= max(1, int(cap)):
            break
        try:
            aid = int(m.get("account_id") or 0)
            uid_i = int(m.get("uid") or 0)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or uid_i <= 0:
            continue
        pair = (aid, uid_i)
        if pair in seen:
            continue
        seen.add(pair)
        acc_sec = _fetch_account_secret(aid)
        if not acc_sec:
            continue
        folder = acc_sec.get("inbox_folder") or "INBOX"
        if not _mail_meta_needs_body(aid, folder, uid_i):
            continue
        try:
            raw = fetch_raw_message(acc_sec, uid_i, mark_seen=False)
            preview_subject, preview_html, preview_meta = parse_and_sanitize_mail(raw)
            attachments = list_attachments_meta(raw)
            dt_sent = _normalize_dt(m.get("internal_date")) or _normalize_dt(m.get("header_date"))
            upsert_message_cache(
                aid,
                folder,
                uid_i,
                message_id=str(preview_meta.get("message_id") or ""),
                subject=str(preview_meta.get("subject") or ""),
                from_addr=str(preview_meta.get("from") or ""),
                date_sent=dt_sent,
                is_read=bool(m.get("is_read")),
                body_html_sanitized=preview_html,
                has_attachments=len(attachments) > 0,
            )
            done += 1
        except Exception:
            continue
    return done


def _fetch_messages_for_mail_ui(
    account_id: int,
    acc: Dict[str, Any],
    *,
    sidebar: bool,
    suppress_flash: bool,
    database_snapshot_only: bool = False,
    q: str,
    search_scope: str,
    list_filter: str,
    show_full_folder: bool,
    mail_group: str,
    bt_mode: str,
    fdc_focus: str,
    bol_focus: str,
) -> List[Dict[str, Any]]:
    """Shared inbox / thread-sidebar message list (same rules as inbox vs view_message)."""
    if sidebar:
        q = ""
        search_scope = "current"
        list_filter = ""
    if search_scope not in {"current", "all"}:
        search_scope = "current"
    attachment_only = list_filter == "attachments"
    apply_recipient_strict = search_scope == "current" and not show_full_folder

    if database_snapshot_only and not mail_group and not (q and search_scope == "all"):
        db_msgs = _list_inbox_from_mail_meta(acc, limit=150)
        if db_msgs:
            _msgs_inner = db_msgs
            if q:
                ql = q.lower()
                _msgs_inner = [
                    m
                    for m in _msgs_inner
                    if ql in (m.get("subject") or "").lower()
                    or ql in (m.get("from_addr") or "").lower()
                    or ql in str(m.get("uid") or "")
                ]
                if not _msgs_inner and not suppress_flash:
                    flash("No matches in this inbox.")
            if attachment_only:
                _msgs_inner = [m for m in _msgs_inner if m.get("has_attachments")]
            if apply_recipient_strict:
                _msgs_inner = [m for m in _msgs_inner if m.get("addressed_here", True)]
            for m in _msgs_inner:
                m.setdefault("account_id", account_id)
                m.setdefault("account_label", acc.get("label"))
                m.setdefault("account_email", acc.get("email"))
            return _msgs_inner

    messages: List[Dict[str, Any]] = []
    try:
        _msgs_inner: List[Dict[str, Any]] = []
        if mail_group == "container":
            _msgs_inner = list_container_group_from_cache(
                limit=500, max_age_days=MAIL_COMBINED_INBOX_MAX_DAYS
            )
            if not _msgs_inner and _fetch_accounts_public():
                _msgs_inner = _list_inbox_all_accounts_merged(
                    recipient_strict=not show_full_folder,
                    since_days=MAIL_COMBINED_INBOX_MAX_DAYS,
                )
                _msgs_inner = [m for m in _msgs_inner if _message_matches_mail_group(m, "container")]
                enrich_container_mail_rows(_msgs_inner)
                _msgs_inner = _dedupe_container_combined_rows(_msgs_inner)
            if q:
                ql = q.lower()
                _msgs_inner = [
                    m
                    for m in _msgs_inner
                    if ql in (m.get("subject") or "").lower()
                    or ql in (m.get("from_addr") or "").lower()
                    or ql in str(m.get("uid") or "")
                ]
                if not _msgs_inner and not suppress_flash:
                    flash("No matches for this Allmail group.")
            if fdc_focus or bol_focus:
                _msgs_inner = [
                    m for m in _msgs_inner if _email_row_matches_container_focus(m, fdc_focus, bol_focus)
                ]
        elif mail_group:
            _msgs_inner = _list_inbox_all_accounts_merged(
                recipient_strict=not show_full_folder,
                since_days=MAIL_COMBINED_INBOX_MAX_DAYS,
            )
            _msgs_inner = [m for m in _msgs_inner if _message_matches_mail_group(m, mail_group)]
            if mail_group in _BT_ROUTE_PICKUP_GROUPS:
                _msgs_inner = [m for m in _msgs_inner if _message_matches_bt_mode(m, bt_mode)]
            if mail_group == "burlington" and _normalize_bt_mode(bt_mode) == "route":
                _enrich_burlington_route_rows(_msgs_inner)
            if mail_group == "burlington" and _normalize_bt_mode(bt_mode) == "pickup":
                _enrich_burlington_pickup_rows(_msgs_inner)
            if q:
                ql = q.lower()
                _msgs_inner = [
                    m
                    for m in _msgs_inner
                    if ql in (m.get("subject") or "").lower()
                    or ql in (m.get("from_addr") or "").lower()
                    or ql in str(m.get("uid") or "")
                ]
                if not _msgs_inner and not suppress_flash:
                    flash("No matches for this Allmail group.")
        elif q and search_scope == "all":
            _msgs_inner = search_cached_messages(
                q, account_id=None, limit=300, attachment_only=attachment_only
            )
            if not _msgs_inner and not suppress_flash:
                flash("No matches in cached metadata across all inboxes.")
        else:
            _msgs_inner = list_inbox_messages(acc)
            if q:
                ql = q.lower()
                _msgs_inner = [
                    m
                    for m in _msgs_inner
                    if ql in (m.get("subject") or "").lower()
                    or ql in (m.get("from_addr") or "").lower()
                    or ql in str(m.get("uid") or "")
                ]
                if not _msgs_inner and not suppress_flash:
                    flash("No matches in this inbox.")
            if attachment_only:
                _msgs_inner = [m for m in _msgs_inner if m.get("has_attachments")]
            if apply_recipient_strict:
                _msgs_inner = [m for m in _msgs_inner if m.get("addressed_here", True)]
            for m in _msgs_inner:
                m.setdefault("account_id", account_id)
                m.setdefault("account_label", acc.get("label"))
                m.setdefault("account_email", acc.get("email"))
        messages = _msgs_inner
    except Exception as exc:
        if not suppress_flash:
            flash(f"Inbox error: {exc}")
        messages = []
    if q and search_scope == "all":
        for m in messages:
            if not m.get("account_id"):
                m["account_id"] = account_id
            if not m.get("account_label"):
                m["account_label"] = acc.get("label")
            if not m.get("account_email"):
                m["account_email"] = acc.get("email")
    return messages


def _serialize_mail_row_for_poll(m: Dict[str, Any], fallback_account_id: int) -> Dict[str, Any]:
    internal = m.get("internal_date")
    header = m.get("header_date")

    def fmt(dt: Any) -> Optional[str]:
        if dt is None:
            return None
        if isinstance(dt, datetime):
            return dt.strftime("%Y-%m-%d %H:%M")
        return None

    try:
        uid_i = int(m["uid"]) if m.get("uid") is not None else None
    except (TypeError, ValueError):
        uid_i = None
    try:
        aid_i = int(m.get("account_id") or fallback_account_id)
    except (TypeError, ValueError):
        aid_i = fallback_account_id

    return {
        "uid": uid_i,
        "subject": str(m.get("subject") or ""),
        "from_addr": str(m.get("from_addr") or ""),
        "is_read": bool(m.get("is_read")),
        "has_attachments": bool(m.get("has_attachments")),
        "account_id": aid_i,
        "account_label": str(m.get("account_label") or ""),
        "account_email": str(m.get("account_email") or ""),
        "account_labels_display": str(m.get("account_labels_display") or ""),
        "internal_date": fmt(internal),
        "header_date": fmt(header),
        "container_ids": [str(x) for x in (m.get("container_ids") or []) if x],
        "container_origin": str(m.get("container_origin") or ""),
        "burlington_route_id": str(m.get("burlington_route_id") or ""),
        "burlington_po_list": [str(x) for x in (m.get("burlington_po_list") or []) if x],
        "burlington_ship_start": str(m.get("burlington_ship_start") or ""),
        "burlington_ship_cancel": str(m.get("burlington_ship_cancel") or ""),
        "burlington_pickup_route_id": str(m.get("burlington_pickup_route_id") or ""),
        "burlington_pickup_slot_date": str(m.get("burlington_pickup_slot_date") or ""),
        "burlington_pickup_slot_time": str(m.get("burlington_pickup_slot_time") or ""),
        "burlington_pickup_title": str(m.get("burlington_pickup_title") or ""),
    }


def _addr_pair_from_envelope_address(ad) -> Optional[str]:
    try:
        mb = getattr(ad, "mailbox", None)
        host = getattr(ad, "host", None)
        if not mb or not host:
            return None
        mbs = mb.decode("utf-8", errors="ignore").lower() if isinstance(mb, bytes) else str(mb).lower()
        hs = host.decode("utf-8", errors="ignore").lower() if isinstance(host, bytes) else str(host).lower()
        return f"{mbs}@{hs}"
    except Exception:
        return None


def _envelope_recipient_fields_contain_mailbox(env, mailbox_email: str) -> bool:
    """True if ENVELOPE To/Cc/Bcc lists this mailbox address (exact local@host match)."""
    if not env or not (mailbox_email or "").strip():
        return True
    needle = mailbox_email.strip().lower()
    any_recipient = False
    for field_name in ("to", "cc", "bcc"):
        field = getattr(env, field_name, None)
        if not field:
            continue
        for ad in field:
            pair = _addr_pair_from_envelope_address(ad)
            if not pair:
                continue
            any_recipient = True
            if pair == needle:
                return True
    if not any_recipient:
        return True
    return False


def _meta_recipient_lines_contain_mailbox(meta: Dict[str, Any], mailbox_email: str) -> bool:
    """Loose match on parsed To/Cc/Bcc display strings (for preview banner)."""
    if not (mailbox_email or "").strip():
        return True
    needle = mailbox_email.strip().lower()
    blob = " ".join(str(meta.get(k) or "") for k in ("to", "cc", "bcc")).lower()
    return needle in blob


def list_inbox_messages(
    acc: Dict[str, Any],
    *,
    limit: int = 150,
    since_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    folder = acc.get("inbox_folder") or "INBOX"
    client = _imap_connect(acc)
    try:
        client.select_folder(folder, readonly=True)
        if since_days is not None:
            since_dt = datetime.now() - timedelta(days=max(1, int(since_days)))
            since_s = since_dt.strftime("%d-%b-%Y")
            uids = client.search(["SINCE", since_s])
        else:
            uids = client.search(["ALL"])
        if not uids:
            return []
        if len(uids) > limit:
            uids = uids[-limit:]
        fetched = client.fetch(uids, ["ENVELOPE", "FLAGS", "INTERNALDATE", "BODYSTRUCTURE"])
        rows: List[Dict[str, Any]] = []
        for uid, data in fetched.items():
            env = data.get(b"ENVELOPE")
            flags = data.get(b"FLAGS")
            internal = data.get(b"INTERNALDATE")
            bs = data.get(b"BODYSTRUCTURE")
            has_att = _bodystructure_has_attachments(bs)
            subject = ""
            from_s = ""
            msg_id = ""
            date_hdr = None
            if env:
                subject = _decode_mime_header(env.subject)
                from_s = _addresses_pretty(env.from_)
                msg_id_b = getattr(env, "message_id", None)
                msg_id = _decode_mime_header(msg_id_b) if isinstance(msg_id_b, (bytes, type(None))) else str(msg_id_b or "")
                raw_date = getattr(env, "date", None)
                if isinstance(raw_date, bytes):
                    try:
                        date_hdr = parsedate_to_datetime(raw_date.decode("ascii", errors="ignore"))
                    except Exception:
                        date_hdr = None
            mb_email = (acc.get("email") or "").strip()
            addressed_here = _envelope_recipient_fields_contain_mailbox(env, mb_email) if env else True
            rows.append(
                {
                    "uid": int(uid),
                    "subject": subject or "(no subject)",
                    "from_addr": from_s or "",
                    "internal_date": _normalize_dt(internal),
                    "header_date": _normalize_dt(date_hdr),
                    "is_read": _folder_flags_seen(flags),
                    "message_id": msg_id.strip("<> ") if msg_id else "",
                    "has_attachments": has_att,
                    "addressed_here": addressed_here,
                }
            )
        rows.sort(key=lambda r: r["internal_date"] or datetime.min, reverse=True)
        return rows
    finally:
        try:
            client.logout()
        except Exception:
            pass


def backfill_recent_metadata(
    acc: Dict[str, Any],
    *,
    days: int = 31,
    max_messages: int = 4000,
) -> int:
    """
    Fetch recent inbox envelope/flags and persist metadata rows.
    Does not download full MIME body (lightweight bootstrap).
    """
    folder = acc.get("inbox_folder") or "INBOX"
    client = _imap_connect(acc)
    conn = get_mysql_connection()
    if not conn:
        return 0
    cur = None
    inserted = 0
    try:
        since_dt = datetime.now() - timedelta(days=max(1, days))
        since_s = since_dt.strftime("%d-%b-%Y")

        client.select_folder(folder, readonly=True)
        uids = client.search(["SINCE", since_s])
        if not uids:
            return 0
        if len(uids) > max_messages:
            uids = uids[-max_messages:]

        fetched = client.fetch(uids, ["ENVELOPE", "FLAGS", "INTERNALDATE", "BODYSTRUCTURE"])
        rows = []
        for uid, data in fetched.items():
            env = data.get(b"ENVELOPE")
            flags = data.get(b"FLAGS")
            internal = data.get(b"INTERNALDATE")
            bs = data.get(b"BODYSTRUCTURE")
            if not env:
                continue
            subject = (_decode_mime_header(env.subject) or "")[:1024]
            from_addr = (_addresses_pretty(env.from_) or "")[:512]
            msg_id_b = getattr(env, "message_id", None)
            message_id = (
                _decode_mime_header(msg_id_b).strip("<> ")
                if isinstance(msg_id_b, (bytes, type(None)))
                else str(msg_id_b or "").strip("<> ")
            )
            c_ids = _container_ids_from_text(subject)
            c_json = json.dumps(c_ids)[:768] if c_ids else None
            rows.append(
                (
                    int(acc["id"]),
                    folder,
                    int(uid),
                    message_id or None,
                    subject or None,
                    from_addr or None,
                    _normalize_dt(internal),
                    1 if _folder_flags_seen(flags) else 0,
                    1 if _bodystructure_has_attachments(bs) else 0,
                    c_json,
                )
            )

        if rows:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO mail_message_meta
                  (mail_account_id, folder, imap_uid, message_id, subject, from_addr, date_sent, is_read, has_attachments, container_ids_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  message_id=VALUES(message_id),
                  subject=VALUES(subject),
                  from_addr=VALUES(from_addr),
                  date_sent=VALUES(date_sent),
                  is_read=VALUES(is_read),
                  has_attachments=VALUES(has_attachments),
                  container_ids_json=IF(
                    (container_ids_json IS NOT NULL AND TRIM(container_ids_json) <> ''),
                    container_ids_json,
                    VALUES(container_ids_json)
                  )
                """,
                rows,
            )
            conn.commit()
            inserted = len(rows)
        return inserted
    except Exception:
        return 0
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
        try:
            client.logout()
        except Exception:
            pass


def _rfc822_local_path(account_id: int, folder: str, uid: int) -> Path:
    safe_folder = re.sub(r"[^\w.\-]+", "_", folder)[:120]
    d = _email_root() / "rfc822" / str(account_id) / safe_folder
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uid}.eml"


def _persist_rfc822_local(account_id: int, folder: str, uid: int, raw: bytes) -> None:
    if not raw or account_id <= 0:
        return
    try:
        _rfc822_local_path(account_id, folder, uid).write_bytes(raw)
    except OSError:
        pass


def _try_local_rfc822(account_id: int, folder: str, uid: int) -> Optional[bytes]:
    """Return cached raw message from disk if present (written after a prior IMAP fetch)."""
    if account_id <= 0:
        return None
    path = _rfc822_local_path(account_id, folder, uid)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path.read_bytes()
    except OSError:
        pass
    return None


def prune_local_email_mirror_files(*, retention_days: int = MAIL_LOCAL_RETENTION_DAYS) -> int:
    """
    Delete on-disk *.html body mirrors and *.eml RFC822 copies older than retention_days.
    Does not modify MySQL mail_message_meta.
    """
    root = _email_root()
    if not root.is_dir():
        return 0
    cutoff_ts = datetime.now().timestamp() - max(1, int(retention_days)) * 86400
    removed = 0
    for child in root.iterdir():
        try:
            if not child.is_dir():
                continue
            if child.name == "rfc822":
                for f in child.rglob("*.eml"):
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff_ts:
                            f.unlink()
                            removed += 1
                    except OSError:
                        pass
            elif child.name.isdigit():
                for f in child.rglob("*.html"):
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff_ts:
                            f.unlink()
                            removed += 1
                    except OSError:
                        pass
        except OSError:
            continue
    return removed


def fetch_raw_message(acc: Dict[str, Any], uid: int, *, mark_seen: bool = False) -> bytes:
    """
    Load full RFC822 bytes: prefer on-disk mirror under EMAIL_DATA_DIR when available,
    otherwise fetch from IMAP.

    On first open there is no local copy yet (monthly bootstrap only stores envelopes in MySQL).
    After one successful fetch we persist *.eml so subsequent opens skip IMAP until prune.

    mark_seen: when True and the message is fetched from IMAP (not local), set \\Seen in the
    same IMAP session to avoid a second connection—no effect on cache hits.
    """
    folder = acc.get("inbox_folder") or "INBOX"
    aid = int(acc.get("id") or 0)
    cached = _try_local_rfc822(aid, folder, int(uid)) if aid else None
    if cached is not None:
        return cached

    client = _imap_connect(acc)
    try:
        client.select_folder(folder, readonly=not mark_seen)
        data = client.fetch([uid], ["RFC822"])
        if uid not in data or b"RFC822" not in data[uid]:
            abort(404)
        raw = bytes(data[uid][b"RFC822"])
        if aid:
            _persist_rfc822_local(aid, folder, int(uid), raw)
        if mark_seen:
            try:
                client.add_flags([uid], [SEEN])
            except Exception:
                pass
        return raw
    finally:
        try:
            client.logout()
        except Exception:
            pass


def mark_seen_on_server(acc: Dict[str, Any], uid: int) -> None:
    folder = acc.get("inbox_folder") or "INBOX"
    client = _imap_connect(acc)
    try:
        client.select_folder(folder, readonly=False)
        client.add_flags([uid], [SEEN])
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _stringify_addr_field(addr_field) -> str:
    if not addr_field:
        return ""
    chunks = []
    for entry in addr_field:
        if isinstance(entry, (list, tuple)):
            chunks.append(" ".join(str(x) for x in entry))
        else:
            chunks.append(str(entry))
    return ", ".join(chunks)


def _inline_cid_images(html: str, raw: bytes) -> str:
    """
    Replace cid:... image refs with data URIs from MIME parts so previews
    can render inline images in srcdoc iframe.
    """
    if not html or "cid:" not in html.lower():
        return html
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return html

    cid_map: Dict[str, str] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_id = str(part.get("Content-ID") or "").strip()
        if not content_id:
            continue
        content_id = content_id.strip("<> ").lower()
        ctype = (part.get_content_type() or "").lower()
        if not ctype.startswith("image/"):
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        b64 = base64.b64encode(payload).decode("ascii")
        cid_map[content_id] = f"data:{ctype};base64,{b64}"

    if not cid_map:
        return html

    def repl(match):
        cid_key = (match.group(1) or "").strip("<> ").lower()
        return cid_map.get(cid_key, match.group(0))

    return re.sub(r"cid:([^\"'>\s]+)", repl, html, flags=re.IGNORECASE)


def _mime_part_is_real_attachment(part) -> bool:
    """Exclude inline/CID images; keep explicit attachments and typical file parts."""
    disp = (part.get_content_disposition() or "").lower()
    ctype = (part.get_content_type() or "").lower()
    fname = part.get_filename()
    cid = (part.get("Content-ID") or "").strip()

    if disp == "inline":
        return False
    if disp == "attachment":
        return True
    if cid and ctype.startswith("image/"):
        return False
    if ctype.startswith("image/"):
        return False
    if ctype in ("text/plain", "text/html"):
        return False
    if fname:
        return True
    if ctype.startswith("application/") or ctype.startswith("message/"):
        return True
    return False


def _collect_attachment_parts(raw: bytes) -> List[Tuple[Any, str, str]]:
    """Return list of (part, filename, content_type) for downloadable attachments."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return []
    out: List[Tuple[Any, str, str]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if not _mime_part_is_real_attachment(part):
            continue
        fname = part.get_filename()
        safe_name = fname or "attachment"
        out.append((part, safe_name, part.get_content_type() or "application/octet-stream"))
    return out


def list_attachments_meta(raw: bytes) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i, (part, fn, ctype) in enumerate(_collect_attachment_parts(raw)):
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        rows.append({"index": i, "filename": fn, "content_type": ctype, "size": len(payload)})
    return rows


def get_attachment_by_index(raw: bytes, index: int) -> Optional[Tuple[bytes, str, str]]:
    parts = _collect_attachment_parts(raw)
    if index < 0 or index >= len(parts):
        return None
    part, fn, ctype = parts[index]
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = b""
    return payload, fn, ctype


def _parse_with_stdlib(raw: bytes) -> Tuple[str, str, Dict[str, Any]]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(msg.get("subject") or "")
    from_v = str(msg.get("from") or "")
    to_v = str(msg.get("to") or "")
    date_v = str(msg.get("date") or "")
    message_id = str(msg.get("message-id") or "").strip("<> ")

    html_parts: List[str] = []
    text_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get_content_disposition() or "").lower()
            if disp == "attachment":
                continue
            try:
                payload = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            payload = str(payload or "")
            if ctype == "text/html":
                html_parts.append(payload)
            elif ctype == "text/plain":
                text_parts.append(payload)
    else:
        ctype = (msg.get_content_type() or "").lower()
        body = msg.get_content()
        if isinstance(body, bytes):
            body = body.decode(msg.get_content_charset() or "utf-8", errors="replace")
        body = str(body or "")
        if ctype == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)

    html_body = "\n<hr/>\n".join(p for p in html_parts if p)
    plain = "\n\n".join(p for p in text_parts if p)
    html_body = _inline_cid_images(html_body, raw)
    safe_html = sanitize_html_fragment(html_body)
    if not safe_html.strip() and plain:
        safe_html = sanitize_html_fragment(f"<pre>{bleach.clean(plain, tags=[], strip=True)}</pre>")

    meta = {
        "subject": subject,
        "from": from_v,
        "to": to_v,
        "cc": str(msg.get("cc") or ""),
        "bcc": str(msg.get("bcc") or ""),
        "date": date_v,
        "message_id": message_id,
    }
    return subject, safe_html, meta


def _reply_seed_from_raw(raw: bytes) -> Dict[str, str]:
    """Build reply defaults without relying on mail-parser address internals."""
    try:
        parsed = mailparser.parse_from_bytes(raw)
        subj = str(parsed.subject or "")
        hdr_from = None
        if getattr(parsed, "headers", None):
            hdr_from = parsed.headers.get("From") or parsed.headers.get("from")
        if isinstance(hdr_from, list) and hdr_from:
            hdr_from = hdr_from[0]
        _, to_addr = parseaddr(str(hdr_from or ""))
        mid = str(parsed.message_id or "").strip()
    except Exception:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        subj = str(msg.get("subject") or "")
        _, to_addr = parseaddr(str(msg.get("from") or ""))
        mid = str(msg.get("message-id") or "").strip()

    if subj and not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"
    if mid and not mid.startswith("<"):
        mid = f"<{mid}>"
    return {"to": to_addr, "subject": subj, "message_id": mid}


def _header_get(msg, name: str) -> str:
    try:
        return str(msg.get(name) or "")
    except Exception:
        return ""


def _find_sent_reply_for_message(
    acc: Dict[str, Any],
    *,
    orig_message_id: str,
    pickup_route_id: str = "",
    lookback: int = 180,
) -> Optional[Dict[str, Any]]:
    """
    Find a likely sent reply in Sent folders:
    - prefer In-Reply-To / References containing original Message-ID
    - fallback: same CS route id in subject/body for Burlington pickup
    """
    client = _imap_connect(acc)
    try:
        sent_candidates = [
            "Sent",
            "INBOX.Sent",
            "Sent Items",
            "INBOX.Sent Items",
            "INBOX/Sent",
            "INBOX/Sent Items",
        ]
        try:
            all_folders = client.list_folders()
            for _, _, folder_name in all_folders:
                fn = str(folder_name or "")
                fl = fn.lower()
                if "sent" in fl and fn not in sent_candidates:
                    sent_candidates.append(fn)
        except Exception:
            pass

        orig_mid = (orig_message_id or "").strip().strip("<>").lower()
        route_id = (pickup_route_id or "").strip().upper()

        for folder in sent_candidates:
            try:
                client.select_folder(folder, readonly=True)
            except Exception:
                continue
            try:
                uids = client.search(["ALL"])
            except Exception:
                continue
            if not uids:
                continue
            uids = sorted(uids)[-max(20, int(lookback)) :]
            try:
                data = client.fetch(uids, ["RFC822"])
            except Exception:
                continue
            for uid in reversed(uids):
                row = data.get(uid) or {}
                raw = row.get(b"RFC822") or row.get("RFC822")
                if not raw:
                    continue
                try:
                    msg = BytesParser(policy=policy.default).parsebytes(raw)
                except Exception:
                    continue
                in_reply_to = _header_get(msg, "In-Reply-To").lower()
                references = _header_get(msg, "References").lower()
                subj = _header_get(msg, "Subject")
                match_mid = bool(orig_mid and (orig_mid in in_reply_to or orig_mid in references))
                match_route = bool(route_id and route_id in (subj or "").upper())
                if not (match_mid or match_route):
                    continue
                s_subj, s_html, s_meta = parse_and_sanitize_mail(raw)
                return {
                    "uid": int(uid),
                    "folder": folder,
                    "subject": s_subj,
                    "html": s_html,
                    "meta": s_meta,
                }
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return None


def parse_and_sanitize_mail(raw: bytes) -> Tuple[str, str, Dict[str, Any]]:
    try:
        parsed = mailparser.parse_from_bytes(raw)
        meta = {
            "subject": parsed.subject or "",
            "from": _stringify_addr_field(parsed.from_),
            "to": _stringify_addr_field(parsed.to),
            "cc": _stringify_addr_field(getattr(parsed, "cc", None)),
            "bcc": _stringify_addr_field(getattr(parsed, "bcc", None)),
            "date": parsed.date.isoformat() if parsed.date else "",
            "message_id": (parsed.message_id or "").strip("<> "),
        }
        html_body = parsed.text_html or ""
        if isinstance(html_body, list):
            html_body = "\n<hr/>\n".join(html_body)
        plain = parsed.text_plain or ""
        if isinstance(plain, list):
            plain = "\n\n".join(plain)
        html_body = _inline_cid_images(html_body, raw)
        safe_html = sanitize_html_fragment(html_body)
        if not safe_html.strip() and plain:
            safe_html = sanitize_html_fragment(f"<pre>{bleach.clean(plain, tags=[], strip=True)}</pre>")
        return meta["subject"], safe_html, meta
    except Exception:
        return _parse_with_stdlib(raw)


def _cache_body_path(account_id: int, folder: str, uid: int) -> Path:
    safe_folder = re.sub(r"[^\w.\-]+", "_", folder)[:120]
    d = _email_root() / str(account_id) / safe_folder
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uid}.html"


def upsert_message_cache(
    account_id: int,
    folder: str,
    uid: int,
    *,
    message_id: str,
    subject: str,
    from_addr: str,
    date_sent: Optional[datetime],
    is_read: bool,
    body_html_sanitized: str,
    has_attachments: Optional[bool] = None,
) -> None:
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    try:
        c_json = _serialized_container_ids_for_row(subject, body_html_sanitized)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO mail_message_meta
              (mail_account_id, folder, imap_uid, message_id, subject, from_addr, date_sent, is_read,
               has_attachments, body_html_sanitized, body_cached_at, container_ids_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            ON DUPLICATE KEY UPDATE
              message_id=VALUES(message_id),
              subject=VALUES(subject),
              from_addr=VALUES(from_addr),
              date_sent=VALUES(date_sent),
              is_read=VALUES(is_read),
              has_attachments=COALESCE(VALUES(has_attachments), has_attachments),
              body_html_sanitized=VALUES(body_html_sanitized),
              body_cached_at=VALUES(body_cached_at),
              container_ids_json=VALUES(container_ids_json)
            """,
            (
                account_id,
                folder,
                uid,
                message_id or None,
                subject[:1024] if subject else None,
                from_addr[:512] if from_addr else None,
                date_sent,
                1 if is_read else 0,
                (1 if has_attachments else 0) if has_attachments is not None else None,
                body_html_sanitized,
                c_json,
            ),
        )
        conn.commit()
        path = _cache_body_path(account_id, folder, uid)
        path.write_text(body_html_sanitized, encoding="utf-8")
    except MySQLError:
        pass
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def update_read_db(account_id: int, folder: str, uid: int, is_read: bool) -> None:
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO mail_message_meta (mail_account_id, folder, imap_uid, is_read)
            VALUES (%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE is_read=VALUES(is_read)
            """,
            (account_id, folder, uid, 1 if is_read else 0),
        )
        conn.commit()
    except MySQLError:
        pass
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def search_cached_messages(
    query: str,
    *,
    account_id: Optional[int] = None,
    limit: int = 300,
    attachment_only: bool = False,
) -> List[Dict[str, Any]]:
    """Search cached metadata in mail_message_meta (subject/from/message_id)."""
    q = (query or "").strip()
    if not q:
        return []
    conn = get_mysql_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        like = f"%{q}%"
        att_clause = " AND m.has_attachments = 1 " if attachment_only else ""
        if account_id:
            cur.execute(
                f"""
                SELECT
                    m.mail_account_id AS account_id,
                    a.label AS account_label,
                    a.email AS account_email,
                    m.imap_uid AS uid,
                    m.subject,
                    m.from_addr,
                    m.date_sent AS internal_date,
                    m.is_read,
                    m.has_attachments AS has_attachments
                FROM mail_message_meta m
                JOIN mail_accounts a ON a.id = m.mail_account_id
                WHERE m.mail_account_id = %s
                  AND (
                    m.subject LIKE %s OR
                    m.from_addr LIKE %s OR
                    m.message_id LIKE %s
                  )
                  {att_clause}
                ORDER BY m.date_sent DESC, m.imap_uid DESC
                LIMIT %s
                """,
                (account_id, like, like, like, limit),
            )
        else:
            cur.execute(
                f"""
                SELECT
                    m.mail_account_id AS account_id,
                    a.label AS account_label,
                    a.email AS account_email,
                    m.imap_uid AS uid,
                    m.subject,
                    m.from_addr,
                    m.date_sent AS internal_date,
                    m.is_read,
                    m.has_attachments AS has_attachments
                FROM mail_message_meta m
                JOIN mail_accounts a ON a.id = m.mail_account_id
                WHERE
                  (m.subject LIKE %s OR
                  m.from_addr LIKE %s OR
                  m.message_id LIKE %s)
                  {att_clause}
                ORDER BY m.date_sent DESC, m.imap_uid DESC
                LIMIT %s
                """,
                (like, like, like, limit),
            )
        rows = list(cur.fetchall())
        for row in rows:
            ha = row.get("has_attachments")
            row["has_attachments"] = bool(ha) if ha is not None else False
        return rows
    except MySQLError:
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


@email_bp.route("")
@login_required
def mailboxes():
    rows = _fetch_accounts_public()
    return render_template(
        "email/mailboxes.html",
        current_user=session["username"],
        accounts=rows,
        show_admin=_accounts_admin_required(),
    )


@email_bp.route("/accounts/manage", methods=["GET", "POST"])
@login_required
@accounts_admin_required
def manage_accounts():
    conn = get_mysql_connection()
    if not conn:
        flash("Database connection failed.")
        return redirect(url_for("email.mailboxes"))
    cur = None
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                label = (request.form.get("label") or "").strip()
                email_a = (request.form.get("email") or "").strip()
                pw = request.form.get("password") or ""
                imap_host = (request.form.get("imap_host") or "imap.fatcow.com").strip()
                smtp_host = (request.form.get("smtp_host") or "smtp.fatcow.com").strip()
                try:
                    imap_port = int(request.form.get("imap_port") or 993)
                    smtp_port = int(request.form.get("smtp_port") or 465)
                except ValueError:
                    flash("Ports must be integers.")
                    return redirect(url_for("email.manage_accounts"))
                inbox_folder = (request.form.get("inbox_folder") or "INBOX").strip() or "INBOX"
                if not label or not email_a or not pw:
                    flash("Label, email, and password are required.")
                    return redirect(url_for("email.manage_accounts"))
                blob = encrypt_secret(pw)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO mail_accounts
                      (label, email, imap_host, imap_port, smtp_host, smtp_port, encrypted_password, inbox_folder)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (label, email_a, imap_host, imap_port, smtp_host, smtp_port, blob, inbox_folder),
                )
                conn.commit()
                account_id = cur.lastrowid
                bootstrap_account = {
                    "id": account_id,
                    "label": label,
                    "email": email_a,
                    "imap_host": imap_host,
                    "imap_port": imap_port,
                    "smtp_host": smtp_host,
                    "smtp_port": smtp_port,
                    "inbox_folder": inbox_folder,
                    "encrypted_password": blob,
                }
                cached = backfill_recent_metadata(bootstrap_account, days=31, max_messages=4000)
                flash(f"Mailbox saved (password encrypted). Bootstrapped ~1 month metadata: {cached} messages.")
                try:
                    cur.close()
                except Exception:
                    pass
                cur = None
                return redirect(url_for("email.manage_accounts"))
            elif action == "delete":
                aid = request.form.get("account_id")
                try:
                    aid_int = int(aid)
                except (TypeError, ValueError):
                    aid_int = None
                if aid_int:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM mail_accounts WHERE id=%s", (aid_int,))
                    conn.commit()
                    flash("Mailbox removed.")
                    try:
                        cur.close()
                    except Exception:
                        pass
                    cur = None
                    return redirect(url_for("email.manage_accounts"))
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, label, email, imap_host, imap_port, smtp_host, smtp_port, inbox_folder FROM mail_accounts ORDER BY label"
        )
        rows = cur.fetchall()
        return render_template(
            "email/mail_accounts_manage.html",
            current_user=session["username"],
            accounts=rows,
        )
    except MySQLError as exc:
        flash(f"MySQL error: {exc}")
        return redirect(url_for("email.mailboxes"))
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


@email_bp.route("/<int:account_id>/inbox")
@login_required
def inbox(account_id: int):
    acc = _fetch_account_secret(account_id)
    if not acc:
        abort(404)
    q = (request.args.get("q") or "").strip()
    search_scope = (request.args.get("scope") or "current").strip().lower()
    if search_scope not in {"current", "all"}:
        search_scope = "current"
    list_filter = (request.args.get("filter") or "").strip().lower()
    show_full_folder = request.args.get("all_in_folder") == "1"
    mail_group = (request.args.get("mail_group") or "").strip().lower()
    if mail_group not in _ALLMAIL_GROUP_IDS:
        mail_group = ""
    bt_mode = _normalize_bt_mode(request.args.get("bt_mode") or "")
    bt_mode = _normalize_bt_mode(request.args.get("bt_mode") or "")
    fdc_focus = _sanitize_lane_param(request.args.get("fdc") or "") if mail_group == "container" else ""
    bol_focus = _sanitize_lane_param(request.args.get("bol") or "") if mail_group == "container" else ""
    show_landed_container = request.args.get("show_landed") in ("1", "true", "yes") if mail_group == "container" else False

    def _mail_group_nav_suffix(target: str) -> str:
        next_mg: Optional[str] = None
        if mail_group == target:
            next_mg = None
        else:
            next_mg = target
        lf_use = list_filter if not next_mg else ""
        aif_use = show_full_folder if not next_mg else False
        return _inbox_query_suffix(
            q=q,
            scope=search_scope,
            list_filter=lf_use,
            all_in_folder=aif_use,
            mail_group=next_mg,
            show_landed=show_landed_container if next_mg == "container" else False,
            bt_mode=bt_mode if next_mg in _BT_ROUTE_PICKUP_GROUPS else "route",
        )

    messages = _fetch_messages_for_mail_ui(
        account_id,
        acc,
        sidebar=False,
        suppress_flash=False,
        database_snapshot_only=True,
        q=q,
        search_scope=search_scope,
        list_filter=list_filter,
        show_full_folder=show_full_folder,
        mail_group=mail_group,
        bt_mode=bt_mode,
        fdc_focus=fdc_focus,
        bol_focus=bol_focus,
    )
    list_nav = "" if mail_group else list_filter
    inbox_nav_query = _inbox_query_suffix(
        q=q,
        scope=search_scope,
        list_filter=list_nav,
        all_in_folder=show_full_folder if not mail_group else False,
        mail_group=mail_group or None,
        fdc=fdc_focus or None,
        bol=bol_focus or None,
        show_landed=show_landed_container if mail_group == "container" else False,
        bt_mode=bt_mode if mail_group in _BT_ROUTE_PICKUP_GROUPS else "route",
    )
    filter_query_all = _inbox_query_suffix(
        q=q, scope=search_scope, list_filter="", all_in_folder=False, mail_group=None
    )
    filter_query_attachments = _inbox_query_suffix(
        q=q,
        scope=search_scope,
        list_filter="attachments",
        all_in_folder=show_full_folder,
        mail_group=None,
    )
    search_clear_suffix = _inbox_query_suffix(
        q="",
        scope=search_scope,
        list_filter=list_filter if not mail_group else "",
        all_in_folder=show_full_folder if not mail_group else False,
        mail_group=mail_group or None,
        fdc=fdc_focus or None,
        bol=bol_focus or None,
        show_landed=show_landed_container if mail_group == "container" else False,
        bt_mode=bt_mode if mail_group in _BT_ROUTE_PICKUP_GROUPS else "route",
    )
    view_msg_group_q = _inbox_query_suffix(
        q="",
        scope="current",
        list_filter="",
        all_in_folder=False,
        mail_group=mail_group or None,
        fdc=fdc_focus or None,
        bol=bol_focus or None,
        show_landed=show_landed_container if mail_group == "container" else False,
        bt_mode=bt_mode if mail_group in _BT_ROUTE_PICKUP_GROUPS else "route",
    )
    boat_lane_rows: List[Dict[str, Any]] = []
    boat_lane_clear_suffix = ""
    boat_lane_return_suffix = ""
    if mail_group == "container":
        boat_lane_rows = _get_open_container_bol_rows(include_landed=show_landed_container)
        _enrich_boat_lane_rows_with_mail_hints(boat_lane_rows)
        boat_lane_clear_suffix = _inbox_query_suffix(
            q=q,
            scope=search_scope,
            list_filter=list_nav,
            all_in_folder=show_full_folder if not mail_group else False,
            mail_group="container",
            show_landed=show_landed_container,
        )
        boat_lane_toggle_suffix = _inbox_query_suffix(
            q=q,
            scope=search_scope,
            list_filter=list_nav,
            all_in_folder=show_full_folder if not mail_group else False,
            mail_group="container",
            fdc=fdc_focus or None,
            bol=bol_focus or None,
            show_landed=not show_landed_container,
        )
        boat_lane_return_suffix = _inbox_query_suffix(
            q=q,
            scope=search_scope,
            list_filter=list_nav,
            all_in_folder=show_full_folder if not mail_group else False,
            mail_group="container",
            fdc=fdc_focus or None,
            bol=bol_focus or None,
            show_landed=show_landed_container,
        )
        for br in boat_lane_rows:
            c = (br.get("container") or "").strip()
            b = (br.get("bol") or "").strip()
            br["fdc_href_suffix"] = (
                _inbox_query_suffix(
                    q=q,
                    scope=search_scope,
                    list_filter=list_nav,
                    all_in_folder=show_full_folder if not mail_group else False,
                    mail_group="container",
                    fdc=c,
                    bol=None,
                    show_landed=show_landed_container,
                    bt_mode=bt_mode,
                )
                if c
                else ""
            )
            br["bol_href_suffix"] = (
                _inbox_query_suffix(
                    q=q,
                    scope=search_scope,
                    list_filter=list_nav,
                    all_in_folder=show_full_folder if not mail_group else False,
                    mail_group="container",
                    fdc=None,
                    bol=b,
                    show_landed=show_landed_container,
                    bt_mode=bt_mode,
                )
                if b
                else ""
            )
    else:
        boat_lane_toggle_suffix = ""
        boat_lane_return_suffix = ""
    if mail_group == "container":
        inbox_header_title = "Combined inbox container"
        if fdc_focus and bol_focus:
            inbox_header_title = f"{inbox_header_title} — {fdc_focus} / {bol_focus}"
        elif fdc_focus:
            inbox_header_title = f"{inbox_header_title} — {fdc_focus}"
        elif bol_focus:
            inbox_header_title = f"{inbox_header_title} — {bol_focus}"
    elif mail_group:
        inbox_header_title = "Combined inbox"
    else:
        inbox_header_title = "Inbox"
    all_accounts = _fetch_accounts_public()
    return render_template(
        "email/email.html",
        current_user=session["username"],
        accounts=all_accounts,
        active_account_id=account_id,
        active_account=_strip_secret(acc),
        messages=messages,
        selected_uid=None,
        preview_subject=None,
        preview_html=None,
        preview_meta=None,
        reply_uid=None,
        search_query=q,
        search_scope=search_scope,
        attachments=[],
        list_filter=list_filter,
        mail_group=mail_group,
        combined_inbox=bool(mail_group),
        inbox_header_title=inbox_header_title,
        fdc_focus=fdc_focus,
        bol_focus=bol_focus,
        boat_lane_rows=boat_lane_rows,
        boat_lane_clear_suffix=boat_lane_clear_suffix,
        boat_lane_toggle_suffix=boat_lane_toggle_suffix,
        boat_lane_return_suffix=boat_lane_return_suffix,
        show_landed_container=show_landed_container,
        bt_mode=bt_mode,
        bt_mode_toggle_suffix=_inbox_query_suffix(
            q=q,
            scope=search_scope,
            list_filter=list_nav,
            all_in_folder=show_full_folder if not mail_group else False,
            mail_group=mail_group or None,
            fdc=fdc_focus or None,
            bol=bol_focus or None,
            show_landed=show_landed_container if mail_group == "container" else False,
            bt_mode="pickup" if bt_mode == "route" else "route",
        ) if mail_group in _BT_ROUTE_PICKUP_GROUPS else "",
        allmail_suffix_container=_mail_group_nav_suffix("container"),
        allmail_suffix_burlington=_mail_group_nav_suffix("burlington"),
        allmail_suffix_gabe=_mail_group_nav_suffix("gabe"),
        allmail_suffix_ross=_mail_group_nav_suffix("ross"),
        allmail_suffix_macy=_mail_group_nav_suffix("macy"),
        allmail_suffix_tj=_mail_group_nav_suffix("tj"),
        view_msg_group_q=view_msg_group_q,
        inbox_nav_query=inbox_nav_query,
        filter_query_all=filter_query_all,
        filter_query_attachments=filter_query_attachments,
        search_clear_suffix=search_clear_suffix,
        preview_container_ids=None,
        preview_container_origin=None,
        sidebar_poll_query="",
        mail_poll_initial_delay_ms=int(MAIL_POLL_INITIAL_DELAY_S * 1000),
    )


@email_bp.route("/<int:account_id>/inbox/poll")
@login_required
def inbox_poll(account_id: int):
    """JSON inbox list for periodic refresh while the mail UI stays open."""
    acc = _fetch_account_secret(account_id)
    if not acc:
        return jsonify(ok=False, error="not_found"), 404
    sidebar = request.args.get("sidebar") == "1"
    q = (request.args.get("q") or "").strip()
    search_scope = (request.args.get("scope") or "current").strip().lower()
    list_filter = (request.args.get("filter") or "").strip().lower()
    show_full_folder = request.args.get("all_in_folder") == "1"
    mail_group = (request.args.get("mail_group") or "").strip().lower()
    if mail_group not in _ALLMAIL_GROUP_IDS:
        mail_group = ""
    fdc_focus = _sanitize_lane_param(request.args.get("fdc") or "") if mail_group == "container" else ""
    bol_focus = _sanitize_lane_param(request.args.get("bol") or "") if mail_group == "container" else ""

    messages = _fetch_messages_for_mail_ui(
        account_id,
        acc,
        sidebar=sidebar,
        suppress_flash=True,
        database_snapshot_only=False,
        q=q,
        search_scope=search_scope,
        list_filter=list_filter,
        show_full_folder=show_full_folder,
        mail_group=mail_group,
        bt_mode=bt_mode,
        fdc_focus=fdc_focus,
        bol_focus=bol_focus,
    )
    bodies_prefetched = _prefetch_mail_bodies_for_poll(messages, cap=MAIL_POLL_BODY_PREFETCH_CAP)
    meta_pruned = prune_mail_message_meta_older_than(retention_days=MAIL_META_RETENTION_DAYS)
    try:
        prune_local_email_mirror_files(retention_days=MAIL_LOCAL_RETENTION_DAYS)
    except Exception:
        pass
    payload = [_serialize_mail_row_for_poll(m, account_id) for m in messages]
    polled_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return jsonify(
        ok=True,
        messages=payload,
        polled_at=polled_at,
        bodies_prefetched=bodies_prefetched,
        meta_pruned=meta_pruned,
    )


@email_bp.route("/<int:account_id>/message/<int:uid>")
@login_required
def view_message(account_id: int, uid: int):
    acc = _fetch_account_secret(account_id)
    if not acc:
        abort(404)
    folder = acc.get("inbox_folder") or "INBOX"
    all_accounts = _fetch_accounts_public()
    show_full_folder = request.args.get("all_in_folder") == "1"
    mail_group_vm = (request.args.get("mail_group") or "").strip().lower()
    if mail_group_vm not in _ALLMAIL_GROUP_IDS:
        mail_group_vm = ""
    bt_mode_vm = _normalize_bt_mode(request.args.get("bt_mode") or "")
    fdc_focus_vm = _sanitize_lane_param(request.args.get("fdc") or "") if mail_group_vm == "container" else ""
    bol_focus_vm = _sanitize_lane_param(request.args.get("bol") or "") if mail_group_vm == "container" else ""
    show_landed_vm = request.args.get("show_landed") in ("1", "true", "yes") if mail_group_vm == "container" else False

    def _mail_group_nav_suffix_vm(target: str) -> str:
        next_mg: Optional[str] = None
        if mail_group_vm == target:
            next_mg = None
        else:
            next_mg = target
        return _inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=False,
            mail_group=next_mg,
            show_landed=show_landed_vm if next_mg == "container" else False,
            bt_mode=bt_mode_vm if next_mg in _BT_ROUTE_PICKUP_GROUPS else "route",
        )

    messages = _fetch_messages_for_mail_ui(
        account_id,
        acc,
        sidebar=True,
        suppress_flash=False,
        database_snapshot_only=True,
        q="",
        search_scope="current",
        list_filter="",
        show_full_folder=show_full_folder,
        mail_group=mail_group_vm,
        bt_mode=bt_mode_vm,
        fdc_focus=fdc_focus_vm,
        bol_focus=bol_focus_vm,
    )
    boat_lane_rows_vm: List[Dict[str, Any]] = []
    boat_lane_clear_suffix_vm = ""
    boat_lane_return_suffix_vm = ""
    if mail_group_vm == "container":
        boat_lane_rows_vm = _get_open_container_bol_rows(include_landed=show_landed_vm)
        _enrich_boat_lane_rows_with_mail_hints(boat_lane_rows_vm)
        boat_lane_clear_suffix_vm = _inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=show_full_folder,
            mail_group="container",
            show_landed=show_landed_vm,
        )
        boat_lane_toggle_suffix_vm = _inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=show_full_folder,
            mail_group="container",
            fdc=fdc_focus_vm or None,
            bol=bol_focus_vm or None,
            show_landed=not show_landed_vm,
        )
        boat_lane_return_suffix_vm = _inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=show_full_folder,
            mail_group="container",
            fdc=fdc_focus_vm or None,
            bol=bol_focus_vm or None,
            show_landed=show_landed_vm,
        )
        for br in boat_lane_rows_vm:
            c = (br.get("container") or "").strip()
            b = (br.get("bol") or "").strip()
            br["fdc_href_suffix"] = (
                _inbox_query_suffix(
                    q="",
                    scope="current",
                    list_filter="",
                    all_in_folder=show_full_folder,
                    mail_group="container",
                    fdc=c,
                    bol=None,
                    show_landed=show_landed_vm,
            bt_mode=bt_mode_vm,
                )
                if c
                else ""
            )
            br["bol_href_suffix"] = (
                _inbox_query_suffix(
                    q="",
                    scope="current",
                    list_filter="",
                    all_in_folder=show_full_folder,
                    mail_group="container",
                    fdc=None,
                    bol=b,
                    show_landed=show_landed_vm,
                    bt_mode=bt_mode_vm,
                )
                if b
                else ""
            )
    else:
        boat_lane_toggle_suffix_vm = ""
        boat_lane_return_suffix_vm = ""
    if mail_group_vm == "container":
        inbox_header_title_vm = "Combined inbox container"
        if fdc_focus_vm and bol_focus_vm:
            inbox_header_title_vm = f"{inbox_header_title_vm} — {fdc_focus_vm} / {bol_focus_vm}"
        elif fdc_focus_vm:
            inbox_header_title_vm = f"{inbox_header_title_vm} — {fdc_focus_vm}"
        elif bol_focus_vm:
            inbox_header_title_vm = f"{inbox_header_title_vm} — {bol_focus_vm}"
    elif mail_group_vm:
        inbox_header_title_vm = "Combined inbox"
    else:
        inbox_header_title_vm = "Inbox"
    preview_subject = ""
    preview_html = ""
    preview_meta = {}
    sent_reply_preview: Optional[Dict[str, Any]] = None
    attachments: List[Dict[str, Any]] = []
    addressed_here = True
    preview_container_ids: List[str] = []
    preview_container_origin = ""
    try:
        raw = fetch_raw_message(acc, uid)
        attachments = list_attachments_meta(raw)
        preview_subject, preview_html, preview_meta = parse_and_sanitize_mail(raw)
        if mail_group_vm == "burlington" and bt_mode_vm == "pickup":
            pickup_fields = _extract_burlington_pickup_fields(
                str(preview_meta.get("subject") or preview_subject or ""),
                preview_html,
            )
            sent_reply_preview = _find_sent_reply_for_message(
                acc,
                orig_message_id=str(preview_meta.get("message_id") or ""),
                pickup_route_id=str(pickup_fields.get("burlington_pickup_route_id") or ""),
            )
        if mail_group_vm == "container":
            scan_blob = _scan_text_from_raw_message(raw)
            preview_container_ids = _container_ids_from_subject_then_body(
                str(preview_meta.get("subject") or preview_subject or ""),
                scan_blob,
            )
            preview_container_origin = _container_origin_from_from_field(
                str(preview_meta.get("from") or "")
            )
        addressed_here = _meta_recipient_lines_contain_mailbox(
            preview_meta, (acc.get("email") or "").strip()
        )
        upsert_message_cache(
            account_id,
            folder,
            uid,
            message_id=str(preview_meta.get("message_id") or ""),
            subject=str(preview_meta.get("subject") or ""),
            from_addr=str(preview_meta.get("from") or ""),
            date_sent=None,
            is_read=True,
            body_html_sanitized=preview_html,
            has_attachments=len(attachments) > 0,
        )
        update_read_db(account_id, folder, uid, True)
    except Exception as exc:
        flash(f"Could not load message: {exc}")

    return render_template(
        "email/email.html",
        current_user=session["username"],
        accounts=all_accounts,
        active_account_id=account_id,
        active_account=_strip_secret(acc),
        messages=messages,
        selected_uid=uid,
        preview_subject=preview_subject,
        preview_html=preview_html,
        preview_meta=preview_meta,
        sent_reply_preview=sent_reply_preview,
        reply_uid=uid,
        attachments=attachments,
        search_query="",
        search_scope="current",
        list_filter="",
        mail_group=mail_group_vm,
        combined_inbox=bool(mail_group_vm),
        allmail_suffix_container=_mail_group_nav_suffix_vm("container"),
        allmail_suffix_burlington=_mail_group_nav_suffix_vm("burlington"),
        allmail_suffix_gabe=_mail_group_nav_suffix_vm("gabe"),
        allmail_suffix_ross=_mail_group_nav_suffix_vm("ross"),
        allmail_suffix_macy=_mail_group_nav_suffix_vm("macy"),
        allmail_suffix_tj=_mail_group_nav_suffix_vm("tj"),
        view_msg_group_q=_inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=False,
            mail_group=mail_group_vm or None,
            fdc=fdc_focus_vm or None,
            bol=bol_focus_vm or None,
            show_landed=show_landed_vm if mail_group_vm == "container" else False,
            bt_mode=bt_mode_vm if mail_group_vm in _BT_ROUTE_PICKUP_GROUPS else "route",
        ),
        inbox_nav_query=_inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=show_full_folder,
            mail_group=mail_group_vm or None,
            fdc=fdc_focus_vm or None,
            bol=bol_focus_vm or None,
            show_landed=show_landed_vm if mail_group_vm == "container" else False,
            bt_mode=bt_mode_vm if mail_group_vm in _BT_ROUTE_PICKUP_GROUPS else "route",
        ),
        filter_query_all=_inbox_query_suffix(
            q="", scope="current", list_filter="", all_in_folder=False, mail_group=None
        ),
        filter_query_attachments=_inbox_query_suffix(
            q="",
            scope="current",
            list_filter="attachments",
            all_in_folder=show_full_folder,
            mail_group=None,
        ),
        search_clear_suffix="",
        addressed_here=addressed_here,
        preview_container_ids=preview_container_ids,
        preview_container_origin=preview_container_origin,
        inbox_header_title=inbox_header_title_vm,
        fdc_focus=fdc_focus_vm,
        bol_focus=bol_focus_vm,
        boat_lane_rows=boat_lane_rows_vm,
        boat_lane_clear_suffix=boat_lane_clear_suffix_vm,
        boat_lane_toggle_suffix=boat_lane_toggle_suffix_vm,
        boat_lane_return_suffix=boat_lane_return_suffix_vm,
        show_landed_container=show_landed_vm,
        bt_mode=bt_mode_vm,
        bt_mode_toggle_suffix=_inbox_query_suffix(
            q="",
            scope="current",
            list_filter="",
            all_in_folder=show_full_folder,
            mail_group=mail_group_vm or None,
            fdc=fdc_focus_vm or None,
            bol=bol_focus_vm or None,
            show_landed=show_landed_vm if mail_group_vm == "container" else False,
            bt_mode="pickup" if bt_mode_vm == "route" else "route",
        ) if mail_group_vm in _BT_ROUTE_PICKUP_GROUPS else "",
        sidebar_poll_query=_sidebar_poll_query_string(),
        mail_poll_initial_delay_ms=int(MAIL_POLL_INITIAL_DELAY_S * 1000),
    )


@email_bp.route("/<int:account_id>/attachment/<int:uid>/<int:att_idx>")
@login_required
def download_attachment(account_id: int, uid: int, att_idx: int):
    acc = _fetch_account_secret(account_id)
    if not acc:
        abort(404)
    raw = fetch_raw_message(acc, uid)
    got = get_attachment_by_index(raw, att_idx)
    if not got:
        abort(404)
    payload, fn, ctype = got
    safe = secure_filename(fn) or "attachment"
    inline = request.args.get("inline") in ("1", "true", "yes")
    ct_lower = (ctype or "").lower()
    if inline and ct_lower.startswith("image/"):
        disp = "inline"
    else:
        disp = "attachment"
    resp = Response(payload, mimetype=ctype or "application/octet-stream")
    resp.headers["Content-Disposition"] = f'{disp}; filename="{safe}"'
    return resp


@email_bp.route("/<int:account_id>/send", methods=["GET", "POST"])
@login_required
def send_mail(account_id: int):
    acc = _fetch_account_secret(account_id)
    if not acc:
        abort(404)
    all_accounts = _fetch_accounts_public()
    reply_uid = request.args.get("reply", type=int)

    defaults = {"to": "", "subject": "", "body": "", "reply_uid": reply_uid}
    extra_headers: Dict[str, str] = {}

    if request.method == "GET" and reply_uid:
        try:
            raw = fetch_raw_message(acc, reply_uid)
            seed = _reply_seed_from_raw(raw)
            defaults["subject"] = seed.get("subject") or defaults["subject"]
            if seed.get("to"):
                defaults["to"] = seed["to"]
            if seed.get("message_id"):
                extra_headers["In-Reply-To"] = seed["message_id"]
                extra_headers["References"] = seed["message_id"]
        except Exception as exc:
            flash(f"Could not prepare reply: {exc}")

    if request.method == "POST":
        to_raw = (request.form.get("to") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        body = request.form.get("body") or ""
        reply_uid_form = request.form.get("reply_uid", type=int)
        ir = (request.form.get("in_reply_to") or "").strip()
        refs = (request.form.get("references") or "").strip()
        if ir:
            extra_headers["In-Reply-To"] = ir
        if refs:
            extra_headers["References"] = refs
        to_addrs = [x.strip() for x in re.split(r"[;,]", to_raw) if x.strip()]
        if not to_addrs or not subject:
            flash("To and subject are required.")
        else:
            hdrs = {}
            if ir:
                hdrs["In-Reply-To"] = ir
            if refs:
                hdrs["References"] = refs
            safe_html = sanitize_html_fragment(f"<pre>{bleach.clean(body, tags=[], strip=True)}</pre>")
            try:
                _smtp_send(acc, to_addrs=to_addrs, subject=subject, body_text=body, html_body=safe_html, extra_headers=hdrs or None)
                flash("Message sent.")
                if reply_uid_form:
                    return redirect(url_for("email.view_message", account_id=account_id, uid=reply_uid_form))
                return redirect(url_for("email.inbox", account_id=account_id))
            except Exception as exc:
                flash(f"Send failed: {exc}")
        defaults.update({"to": to_raw, "subject": subject, "body": body, "reply_uid": reply_uid_form})

    return render_template(
        "email/send.html",
        current_user=session["username"],
        accounts=all_accounts,
        active_account_id=account_id,
        active_account=_strip_secret(acc),
        defaults=defaults,
        hidden_headers=extra_headers,
    )


@email_bp.route("/<int:account_id>/boat-date-override", methods=["POST"])
@login_required
def boat_date_override(account_id: int):
    acc = _fetch_account_secret(account_id)
    if not acc:
        abort(404)
    if (request.form.get("mail_group") or "").strip().lower() != "container":
        return redirect(url_for("email.inbox", account_id=account_id))

    container = _sanitize_lane_param(request.form.get("container") or "", max_len=64)
    bol = _sanitize_lane_param(request.form.get("bol") or "", max_len=128)
    date_md = (request.form.get("manual_date") or "").strip()
    return_suffix = request.form.get("return_suffix") or ""
    if return_suffix and not return_suffix.startswith("?"):
        return_suffix = ""

    if not container:
        flash("Container is required for manual date override.")
        return redirect(url_for("email.inbox", account_id=account_id) + return_suffix)

    if date_md and not re.fullmatch(r"\d{1,2}/\d{1,2}", date_md):
        flash("Date must be in MM/DD format (example: 05/08).")
        return redirect(url_for("email.inbox", account_id=account_id) + return_suffix)

    conn = get_mysql_connection()
    if conn:
        cur = None
        try:
            cur = conn.cursor()
            if date_md:
                cur.execute(
                    """
                    INSERT INTO mail_boat_date_overrides (container, bol, override_date_md, updated_by)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        override_date_md=VALUES(override_date_md),
                        updated_by=VALUES(updated_by),
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (container, bol, date_md, str(session.get("username") or "")),
                )
            else:
                cur.execute(
                    "DELETE FROM mail_boat_date_overrides WHERE container = %s AND bol = %s",
                    (container, bol),
                )
            conn.commit()
            flash("Manual date updated." if date_md else "Manual date cleared.")
        except MySQLError:
            try:
                conn.rollback()
            except Exception:
                pass
            flash("Could not save manual date override.")
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            conn.close()
    else:
        flash("Could not save manual date override.")
    return redirect(url_for("email.inbox", account_id=account_id) + return_suffix)


@email_bp.route("/<int:account_id>/mark-read/<int:uid>", methods=["POST"])
@login_required
def mark_read(account_id: int, uid: int):
    acc = _fetch_account_secret(account_id)
    if not acc:
        abort(404)
    folder = acc.get("inbox_folder") or "INBOX"
    try:
        mark_seen_on_server(acc, uid)
        update_read_db(account_id, folder, uid, True)
        flash("Marked read.")
    except Exception as exc:
        flash(f"Could not mark read: {exc}")
    return redirect(request.referrer or url_for("email.inbox", account_id=account_id))

