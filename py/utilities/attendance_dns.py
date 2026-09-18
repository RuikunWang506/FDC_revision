"""Pull / parse Windows DNS debug logs from the office DNS server (THSERVER4)."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from mysql.connector import Error as MySQLError

from py.utilities.attendance_store import (
    ENV_FILE_NAME,
    TZ,
    _close,
    _conn,
    ensure_schema,
    iso_local,
    parse_env_file_values,
    to_utc_naive,
    utc_now,
)

DEFAULT_DNS_HOST = "192.168.254.204"
DEFAULT_DNS_USER = r"fdc\administrator"
DEFAULT_DNS_LOG_PATH = r"C:\Windows\System32\dns\dns.log"
DEFAULT_DNS_POLL_SECONDS = 60
DEFAULT_DNS_RETENTION_DAYS = 14
DNS_ENV_KEYS = ("DNS_SSH_HOST", "DNS_SSH_USER", "DNS_SSH_PASSWORD", "DNS_LOG_PATH")

_PACKET_RE = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{1,2}:\d{1,2}:\d{1,2}\s+[AP]M)\s+"
    r"[0-9A-Fa-f]+\s+PACKET\s+[0-9A-Fa-f]+\s+"
    r"(?P<proto>UDP|TCP)\s+(?P<dir>Rcv|Snd)\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"[0-9A-Fa-f]+\s+"
    r"(?P<qr>R?)\s*(?P<opcode>[QNU\?])\s+\[[^\]]*\]\s+"
    r"(?P<qtype>\S+)\s+(?P<qname>\S+)\s*$"
)
_PACKET_START_RE = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{1,2}:\d{1,2}:\d{1,2}\s+[AP]M)\s+"
    r"[0-9A-Fa-f]+\s+PACKET\s+[0-9A-Fa-f]+\s+"
    r"(?P<proto>UDP|TCP)\s+(?P<dir>Rcv|Snd)\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<xid>[0-9A-Fa-f]+)\s*$"
)
_PACKET_CONT_RE = re.compile(
    r"^(?P<xidrest>[0-9A-Fa-f]+)\s+(?P<qr>R?)\s*(?P<opcode>[QNU\?])\s+\[[^\]]*\]\s+"
    r"(?P<qtype>\S+)\s+(?P<qname>\S+)\s*$"
)
_DNS_LABEL_RE = re.compile(r"\((\d+)\)([^\(]*)")


def decode_dns_logged_name(raw: str) -> str:
    """Convert (3)www(6)google(3)com(0) → www.google.com"""
    text = (raw or "").strip().strip('"')
    if not text:
        return ""
    if "(" not in text:
        return text.rstrip(".")
    parts: List[str] = []
    for match in _DNS_LABEL_RE.finditer(text):
        length = int(match.group(1))
        label = match.group(2)
        if length == 0:
            break
        parts.append(label[:length] if label else "")
    return ".".join(p for p in parts if p).rstrip(".")


def _parse_log_timestamp(date_s: str, time_s: str) -> Optional[datetime]:
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(f"{date_s} {time_s}", fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _packet_dict(groups: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if groups.get("dir") != "Rcv":
        return None
    if (groups.get("opcode") or "Q") != "Q":
        return None
    if (groups.get("qr") or "") == "R":
        return None
    ip = (groups.get("ip") or "").strip()
    if not ip.startswith("192.168.254."):
        return None
    qname = decode_dns_logged_name(groups.get("qname") or "")
    if not qname or qname.endswith(".in-addr.arpa"):
        return None
    when = _parse_log_timestamp(groups.get("date") or "", groups.get("time") or "")
    if not when:
        return None
    return {
        "queried_at": when,
        "client_ip": ip,
        "qname": qname[:255],
        "qtype": (groups.get("qtype") or "")[:32],
        "proto": (groups.get("proto") or "")[:8],
    }


def parse_dns_debug_lines(lines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, str]] = None
    for raw in lines:
        line = (raw or "").rstrip()
        if not line or line.startswith("Message logging") or line.startswith("\t"):
            continue
        if pending:
            cont = _PACKET_CONT_RE.match(line)
            if cont:
                pending.update(cont.groupdict())
                row = _packet_dict(pending)
                if row:
                    out.append(row)
                pending = None
                continue
            pending = None
        one = _PACKET_RE.match(line)
        if one:
            row = _packet_dict(one.groupdict())
            if row:
                out.append(row)
            continue
        start = _PACKET_START_RE.match(line)
        if start:
            pending = start.groupdict()
    return out


def dns_ssh_settings() -> Dict[str, Any]:
    file_vals = parse_env_file_values(DNS_ENV_KEYS)

    def _pick(env_name: str, default: str = "") -> str:
        return (os.getenv(env_name) or file_vals.get(env_name) or default).strip()

    password = _pick("DNS_SSH_PASSWORD")
    return {
        "host": _pick("DNS_SSH_HOST", DEFAULT_DNS_HOST),
        "user": _pick("DNS_SSH_USER", DEFAULT_DNS_USER),
        "password": password,
        "password_set": bool(password),
        "log_path": _pick("DNS_LOG_PATH", DEFAULT_DNS_LOG_PATH),
        "env_file": ENV_FILE_NAME,
        "poll_seconds": DEFAULT_DNS_POLL_SECONDS,
        "retention_days": DEFAULT_DNS_RETENTION_DAYS,
    }


def ensure_dns_schema() -> None:
    ensure_schema()
    conn = _conn()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_dns_queries (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                queried_utc DATETIME NOT NULL,
                client_ip VARCHAR(45) NOT NULL,
                qname VARCHAR(255) NOT NULL,
                qtype VARCHAR(32) NOT NULL DEFAULT '',
                proto VARCHAR(8) NOT NULL DEFAULT '',
                UNIQUE KEY uq_att_dns (queried_utc, client_ip, qname, qtype),
                INDEX idx_att_dns_ip_time (client_ip, queried_utc),
                INDEX idx_att_dns_time (queried_utc)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_dns_meta (
                meta_key VARCHAR(64) NOT NULL PRIMARY KEY,
                meta_value TEXT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.commit()
    except MySQLError:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _close(conn, cur)


def _get_meta(key: str) -> str:
    ensure_dns_schema()
    conn = _conn()
    if not conn:
        return ""
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT meta_value FROM attendance_dns_meta WHERE meta_key = %s", (key,))
        row = cur.fetchone()
        return "" if not row or row[0] is None else str(row[0])
    finally:
        _close(conn, cur)


def _set_meta(key: str, value: str) -> None:
    ensure_dns_schema()
    conn = _conn()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO attendance_dns_meta (meta_key, meta_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)
            """,
            (key, value),
        )
        conn.commit()
    except MySQLError:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _close(conn, cur)


def fetch_dns_log_tail(max_bytes: int = 2_000_000) -> Tuple[str, str]:
    """SSH/SFTP-read the end of the DNS debug log. Returns (text, error)."""
    settings = dns_ssh_settings()
    if not settings["password_set"]:
        return "", "DNS_SSH_PASSWORD is empty in nssm-fdcef-environment.env"
    try:
        import paramiko
    except ImportError:
        return "", "paramiko is not installed"

    log_path = settings["log_path"]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    start = 0
    raw_bytes: Any = b""
    try:
        client.connect(
            settings["host"],
            username=settings["user"],
            password=settings["password"],
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
            allow_agent=False,
            look_for_keys=False,
        )
        sftp = client.open_sftp()
        try:
            try:
                stat = sftp.stat(log_path)
            except FileNotFoundError:
                return "", f"DNS log missing on server: {log_path}"
            size = int(stat.st_size or 0)
            if size <= 0:
                return "", ""
            start = max(0, size - int(max_bytes))
            with sftp.open(log_path, "rb") as handle:
                if start:
                    handle.seek(start)
                raw_bytes = handle.read()
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    finally:
        try:
            client.close()
        except Exception:
            pass

    if isinstance(raw_bytes, str):
        text = raw_bytes
    else:
        text = raw_bytes.decode("utf-8", errors="replace")
    # If we started mid-line, drop the partial first line
    if start and text:
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    return text, ""


def ingest_dns_log(*, max_bytes: int = 2_000_000) -> Dict[str, Any]:
    ensure_dns_schema()
    text, error = fetch_dns_log_tail(max_bytes=max_bytes)
    if error and not text:
        _set_meta("last_error", error)
        _set_meta("last_poll_local", iso_local(datetime.now(TZ)))
        return {"ok": False, "error": error, "inserted": 0, "parsed": 0}

    lines = text.splitlines()
    rows = parse_dns_debug_lines(lines)
    inserted = 0
    conn = _conn(unpooled=True)
    if not conn:
        return {"ok": False, "error": "MySQL unavailable", "inserted": 0, "parsed": len(rows)}
    cur = None
    try:
        cur = conn.cursor()
        for row in rows:
            cur.execute(
                """
                INSERT IGNORE INTO attendance_dns_queries
                    (queried_utc, client_ip, qname, qtype, proto)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    to_utc_naive(row["queried_at"]),
                    row["client_ip"],
                    row["qname"],
                    row["qtype"],
                    row["proto"],
                ),
            )
            inserted += int(cur.rowcount or 0)
        cutoff = utc_now() - timedelta(days=DEFAULT_DNS_RETENTION_DAYS)
        cur.execute("DELETE FROM attendance_dns_queries WHERE queried_utc < %s", (cutoff,))
        conn.commit()
        _set_meta("last_error", error or "")
        _set_meta("last_poll_local", iso_local(datetime.now(TZ)))
        _set_meta("last_parsed", str(len(rows)))
        _set_meta("last_inserted", str(inserted))
        return {
            "ok": True,
            "error": error or "",
            "parsed": len(rows),
            "inserted": inserted,
            "log_lines": len(lines),
        }
    except MySQLError as err:
        try:
            conn.rollback()
        except Exception:
            pass
        msg = str(err)
        _set_meta("last_error", msg)
        return {"ok": False, "error": msg, "inserted": 0, "parsed": len(rows)}
    finally:
        _close(conn, cur)


def list_dns_client_ips() -> List[Dict[str, Any]]:
    ensure_dns_schema()
    conn = _conn()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT client_ip,
                   COUNT(*) AS query_count,
                   MAX(queried_utc) AS last_query_utc
            FROM attendance_dns_queries
            GROUP BY client_ip
            ORDER BY last_query_utc DESC
            """
        )
        dns_rows = cur.fetchall() or []
        cur.execute(
            """
            SELECT last_ip, alias, hostname, currently_online
            FROM attendance_devices
            WHERE last_ip IS NOT NULL AND last_ip <> ''
            """
        )
        by_ip: Dict[str, Dict[str, Any]] = {}
        for row in cur.fetchall() or []:
            ip = str(row.get("last_ip") or "").strip()
            if ip and ip not in by_ip:
                by_ip[ip] = row
        out = []
        for row in dns_rows:
            ip = row["client_ip"]
            dev = by_ip.get(ip) or {}
            label = (dev.get("alias") or dev.get("hostname") or "").strip()
            out.append(
                {
                    "ip": ip,
                    "label": label,
                    "display": f"{label} ({ip})" if label else ip,
                    "query_count": int(row.get("query_count") or 0),
                    "last_query": iso_local(row.get("last_query_utc")),
                    "currently_online": bool(dev.get("currently_online")),
                }
            )
        return out
    finally:
        _close(conn, cur)


def list_dns_queries_for_ip(
    client_ip: str,
    *,
    hours: int = 24,
    limit: int = 500,
) -> Dict[str, Any]:
    ensure_dns_schema()
    ip = (client_ip or "").strip()
    hours = max(1, min(int(hours or 24), 24 * DEFAULT_DNS_RETENTION_DAYS))
    limit = max(20, min(int(limit or 500), 50000))
    if not ip:
        return {"ok": False, "error": "ip required", "rows": [], "total": 0, "limit": limit}
    conn = _conn()
    if not conn:
        return {"ok": False, "error": "MySQL unavailable", "rows": [], "total": 0, "limit": limit}
    cur = None
    start = utc_now() - timedelta(hours=hours)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM attendance_dns_queries
            WHERE client_ip = %s AND queried_utc >= %s
            """,
            (ip, start),
        )
        total = int((cur.fetchone() or {}).get("cnt") or 0)
        cur.execute(
            """
            SELECT queried_utc, client_ip, qname, qtype, proto
            FROM attendance_dns_queries
            WHERE client_ip = %s AND queried_utc >= %s
            ORDER BY queried_utc DESC
            LIMIT %s
            """,
            (ip, start, limit),
        )
        rows = []
        for row in cur.fetchall() or []:
            rows.append(
                {
                    "time": iso_local(row.get("queried_utc")),
                    "ip": row.get("client_ip") or "",
                    "qname": row.get("qname") or "",
                    "qtype": row.get("qtype") or "",
                    "proto": row.get("proto") or "",
                }
            )
        return {
            "ok": True,
            "ip": ip,
            "hours": hours,
            "rows": rows,
            "total": total,
            "limit": limit,
            "truncated": total > len(rows),
            "meta": {
                "last_poll": _get_meta("last_poll_local"),
                "last_error": _get_meta("last_error"),
                "last_inserted": _get_meta("last_inserted"),
                "password_set": dns_ssh_settings()["password_set"],
                "retention_days": DEFAULT_DNS_RETENTION_DAYS,
            },
        }
    finally:
        _close(conn, cur)


def dns_queries_csv_for_ip(client_ip: str, *, hours: int = 24, limit: int = 50000) -> Tuple[str, str]:
    """Return (filename, csv_text) for download. Empty ip → error csv."""
    payload = list_dns_queries_for_ip(client_ip, hours=hours, limit=limit)
    ip = (client_ip or "unknown").strip() or "unknown"
    safe_ip = re.sub(r"[^0-9A-Za-z._-]+", "_", ip)
    filename = f"dns_{safe_ip}_{int(hours)}h.csv"
    lines = ["time,ip,domain,type,proto"]
    if not payload.get("ok"):
        return filename, "time,ip,domain,type,proto\n"
    for row in payload.get("rows") or []:
        cells = [
            str(row.get("time") or ""),
            str(row.get("ip") or ""),
            str(row.get("qname") or ""),
            str(row.get("qtype") or ""),
            str(row.get("proto") or ""),
        ]
        lines.append(
            ",".join('"' + c.replace('"', '""') + '"' for c in cells)
        )
    return filename, "\n".join(lines) + "\n"
