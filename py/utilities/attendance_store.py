"""MySQL persistence and presence logic for the office Wi-Fi attendance log."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from mysql.connector import Error as MySQLError

from py.utilities.attendance_netgear import (
    fetch_office_devices,
    lan_ip_of_this_host,
    normalize_mac,
    ping_hosts,
    prefer_hostname,
    read_arp_table,
    subnet_ips,
)
from py.utilities.database_utils import get_mysql_connection, get_mysql_connection_unpooled

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_ROUTER_HOST = "192.168.254.1"
DEFAULT_ROUTER_USER = "admin"
DEFAULT_POLL_SECONDS = 30
# Deco/mesh roaming often drops clients for 1–3 minutes; keep them "online" until then.
DEFAULT_ABSENT_MINUTES = 4
DEFAULT_NIGHT_START = "22:00"
DEFAULT_NIGHT_END = "06:00"
DEFAULT_SUBNET = "192.168.254.0/24"
DEFAULT_SUBNET_PREFIX = "192.168.254."
ENV_FILE_NAME = "nssm-fdcef-environment.env"
NETGEAR_ENV_KEYS = ("NETGEAR_HOST", "NETGEAR_USER", "NETGEAR_PASSWORD")

OWNER_CATEGORIES = ("personal", "business")
DEVICE_TYPES = ("phone", "computer", "printer", "other")
AUTO_ROLES = ("phone", "warehouse_service", "unknown")

PHONE_HINTS = (
    "iphone",
    "ipad",
    "ipod",
    "ios",
    "android",
    "galaxy",
    "pixel",
    "oneplus",
    "xiaomi",
    "huawei",
    "oppo",
    "vivo",
    "redmi",
    "phone",
    "samsung",
    "z-flip",
    "zflip",
    "z-fold",
    "zfold",
)
PRINTER_HINTS = ("printer", "laserjet", "canon", "epson", "brother", "xerox", "deskjet")
COMPUTER_HINTS = (
    "desktop",
    "laptop",
    "macbook",
    "imac",
    "thinkpad",
    "surface",
    "windows",
    "pc-",
    "win-",
    "mac-",
)
SERVICE_HINTS = (
    "deco",
    "nighthawk",
    "synology",
    "nas",
    "camera",
    "nvr",
    "raspberry",
    "esp32",
    "nest",
    "ring",
    "ap-",
    "access point",
    "roku",
    "chromecast",
    "tv",
)

def env_file_path() -> Path:
    return Path(__file__).resolve().parents[2] / ENV_FILE_NAME


def parse_env_file_values(keys: Sequence[str]) -> Dict[str, str]:
    """Read selected KEY=VALUE pairs from nssm-fdcef-environment.env (no restart needed)."""
    wanted = {str(k) for k in keys}
    out: Dict[str, str] = {}
    path = env_file_path()
    if not path.is_file():
        return out
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key not in wanted:
                continue
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key] = val
    except OSError:
        return out
    return out


def _netgear_creds() -> Dict[str, str]:
    file_vals = parse_env_file_values(NETGEAR_ENV_KEYS)
    host = (file_vals.get("NETGEAR_HOST") or os.getenv("NETGEAR_HOST") or DEFAULT_ROUTER_HOST).strip()
    user = (file_vals.get("NETGEAR_USER") or os.getenv("NETGEAR_USER") or DEFAULT_ROUTER_USER).strip()
    password = (file_vals.get("NETGEAR_PASSWORD") or os.getenv("NETGEAR_PASSWORD") or "").strip()
    return {"host": host or DEFAULT_ROUTER_HOST, "user": user or DEFAULT_ROUTER_USER, "password": password}


def utc_now() -> datetime:
    return datetime.now(TZ).astimezone(ZoneInfo("UTC")).replace(microsecond=0, tzinfo=None)


def to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(microsecond=0)
    return dt.astimezone(ZoneInfo("UTC")).replace(microsecond=0, tzinfo=None)


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ)


def iso_local(dt: Optional[datetime]) -> str:
    local = to_local(dt)
    return local.strftime("%Y-%m-%d %H:%M:%S") if local else ""


def _parse_hhmm(value: str, fallback: str) -> Tuple[int, int]:
    text = (value or fallback).strip()
    try:
        hour_s, min_s = text.split(":", 1)
        hour, minute = int(hour_s), int(min_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    hour_s, min_s = fallback.split(":", 1)
    return int(hour_s), int(min_s)


def is_night(local_dt: datetime, night_start: str, night_end: str) -> bool:
    start_h, start_m = _parse_hhmm(night_start, DEFAULT_NIGHT_START)
    end_h, end_m = _parse_hhmm(night_end, DEFAULT_NIGHT_END)
    minutes = local_dt.hour * 60 + local_dt.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def night_bucket_for(local_dt: datetime, night_start: str) -> str:
    start_h, start_m = _parse_hhmm(night_start, DEFAULT_NIGHT_START)
    start_minutes = start_h * 60 + start_m
    minutes = local_dt.hour * 60 + local_dt.minute
    if minutes >= start_minutes:
        return local_dt.date().isoformat()
    return (local_dt.date() - timedelta(days=1)).isoformat()


def guess_device_type(hostname: str, connection: str = "") -> str:
    blob = f"{hostname or ''} {connection or ''}".strip().lower()
    if not blob:
        return ""
    if any(h in blob for h in PRINTER_HINTS):
        return "printer"
    if any(h in blob for h in PHONE_HINTS):
        return "phone"
    if any(h in blob for h in COMPUTER_HINTS):
        return "computer"
    if any(h in blob for h in SERVICE_HINTS):
        return "other"
    return ""


def is_phone_notify_candidate(
    device_type: str = "",
    auto_role: str = "",
    hostname: str = "",
    alias: str = "",
) -> bool:
    """Phones we alert on: classified phone, night-pattern phone, or phone-like hostname."""
    blob = f"{hostname or ''} {alias or ''}".strip().lower()
    # Watches flap constantly on mesh Wi-Fi; never Slack them as phones.
    if any(token in blob for token in ("iwatch", "watch", "apple watch")):
        return False
    if (device_type or "") == "phone" or (auto_role or "") == "phone":
        return True
    if guess_device_type(hostname or "") == "phone":
        return True
    if guess_device_type(alias or "") == "phone":
        return True
    return False


def classify_auto_role(nights_present: int, nights_absent: int, min_nights: int = 3) -> str:
    sampled = int(nights_present or 0) + int(nights_absent or 0)
    if sampled < min_nights:
        return "unknown"
    leave_ratio = int(nights_absent or 0) / sampled
    if leave_ratio >= 0.7:
        return "phone"
    if leave_ratio <= 0.2:
        return "warehouse_service"
    return "unknown"


def session_overlap_seconds(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> float:
    a = max(start, window_start)
    b = min(end, window_end)
    return max(0.0, (b - a).total_seconds())


def _close(conn, cur) -> None:
    try:
        if cur is not None:
            cur.close()
    except Exception:
        pass
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


_SCHEMA_READY = False


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn = get_mysql_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_settings (
                setting_key VARCHAR(64) NOT NULL PRIMARY KEY,
                setting_value TEXT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_devices (
                mac CHAR(17) NOT NULL PRIMARY KEY,
                alias VARCHAR(128) NULL,
                hostname VARCHAR(255) NULL,
                last_ip VARCHAR(45) NULL,
                connection VARCHAR(64) NULL,
                owner_category VARCHAR(32) NOT NULL DEFAULT '',
                device_type VARCHAR(32) NOT NULL DEFAULT '',
                device_type_source VARCHAR(16) NOT NULL DEFAULT 'auto',
                owner_source VARCHAR(16) NOT NULL DEFAULT 'auto',
                auto_role VARCHAR(32) NOT NULL DEFAULT 'unknown',
                nights_present INT NOT NULL DEFAULT 0,
                nights_absent INT NOT NULL DEFAULT 0,
                night_bucket VARCHAR(10) NULL,
                seen_this_night TINYINT NOT NULL DEFAULT 0,
                first_seen_utc DATETIME NOT NULL,
                last_seen_utc DATETIME NOT NULL,
                currently_online TINYINT NOT NULL DEFAULT 0,
                hidden TINYINT NOT NULL DEFAULT 0,
                notes VARCHAR(512) NULL,
                INDEX idx_att_dev_online (currently_online, last_seen_utc),
                INDEX idx_att_dev_role (auto_role, device_type)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_sessions (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                mac CHAR(17) NOT NULL,
                started_utc DATETIME NOT NULL,
                last_seen_utc DATETIME NOT NULL,
                ended_utc DATETIME NULL,
                close_reason VARCHAR(32) NULL,
                INDEX idx_att_sess_mac_start (mac, started_utc),
                INDEX idx_att_sess_open (mac, ended_utc)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_polls (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                polled_utc DATETIME NOT NULL,
                source VARCHAR(32) NOT NULL,
                complete TINYINT NOT NULL DEFAULT 0,
                device_count INT NOT NULL DEFAULT 0,
                error_text VARCHAR(512) NULL,
                INDEX idx_att_polls_time (polled_utc)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # One-shot: prior defaults (60s / 10m) were too loose for leave timing.
        cur.execute(
            "SELECT setting_value FROM attendance_settings WHERE setting_key = %s",
            ("timing_v2",),
        )
        row = cur.fetchone()
        if not row:
            for key, value in (
                ("poll_seconds", str(DEFAULT_POLL_SECONDS)),
                ("absent_minutes", str(DEFAULT_ABSENT_MINUTES)),
                ("timing_v2", "1"),
            ):
                cur.execute(
                    """
                    INSERT INTO attendance_settings (setting_key, setting_value)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                    """,
                    (key, value),
                )
        conn.commit()
        _SCHEMA_READY = True
    except MySQLError:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _close(conn, cur)


def _conn(unpooled: bool = False):
    ensure_schema()
    return get_mysql_connection_unpooled() if unpooled else get_mysql_connection()


def get_settings() -> Dict[str, Any]:
    ensure_schema()
    stored: Dict[str, str] = {}
    conn = _conn()
    cur = None
    try:
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT setting_key, setting_value FROM attendance_settings")
            for key, value in cur.fetchall() or []:
                stored[str(key)] = "" if value is None else str(value)
    finally:
        _close(conn, cur)

    def _pick(key: str, env_name: str, default: str) -> str:
        if stored.get(key):
            return stored[key]
        env = (os.getenv(env_name) or "").strip()
        return env or default

    creds = _netgear_creds()
    poll_raw = _pick("poll_seconds", "ATTENDANCE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
    # Env wins for roaming grace so ops can tune without editing MySQL.
    env_absent = (os.getenv("ATTENDANCE_ABSENT_MINUTES") or "").strip()
    absent_raw = env_absent or _pick(
        "absent_minutes", "ATTENDANCE_ABSENT_MINUTES", str(DEFAULT_ABSENT_MINUTES)
    )
    try:
        poll_seconds = max(30, min(int(poll_raw), 600))
    except ValueError:
        poll_seconds = DEFAULT_POLL_SECONDS
    try:
        # 1–2 min target: allow 1; keep a small upper bound for noisy Wi-Fi sites
        absent_minutes = max(1, min(int(absent_raw), 120))
    except ValueError:
        absent_minutes = DEFAULT_ABSENT_MINUTES
    return {
        "router_host": creds["host"],
        "router_user": creds["user"],
        "router_password": creds["password"],
        "password_set": bool(creds["password"]),
        "env_file": ENV_FILE_NAME,
        "poll_seconds": poll_seconds,
        "absent_minutes": absent_minutes,
        "night_start": _pick("night_start", "ATTENDANCE_NIGHT_START", DEFAULT_NIGHT_START),
        "night_end": _pick("night_end", "ATTENDANCE_NIGHT_END", DEFAULT_NIGHT_END),
        "subnet": _pick("subnet", "ATTENDANCE_SUBNET", DEFAULT_SUBNET),
    }


def public_settings() -> Dict[str, Any]:
    data = get_settings()
    data.pop("router_password", None)
    return data


def save_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_schema()
    allowed = {
        "poll_seconds",
        "absent_minutes",
        "night_start",
        "night_end",
        "subnet",
    }
    conn = _conn()
    if not conn:
        raise RuntimeError("MySQL unavailable")
    cur = None
    try:
        cur = conn.cursor()
        for key, value in payload.items():
            if key not in allowed:
                continue
            cur.execute(
                """
                INSERT INTO attendance_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                """,
                (key, str(value).strip()),
            )
        conn.commit()
    except MySQLError as err:
        conn.rollback()
        raise RuntimeError(str(err)) from err
    finally:
        _close(conn, cur)
    return public_settings()


def _row_device(row: Dict[str, Any]) -> Dict[str, Any]:
    first_seen = row.get("first_seen_utc")
    last_seen = row.get("last_seen_utc")
    hostname = row.get("hostname") or ""
    alias = row.get("alias") or ""
    device_type = row.get("device_type") or ""
    auto_role = row.get("auto_role") or "unknown"
    return {
        "mac": row.get("mac"),
        "alias": alias,
        "hostname": hostname,
        "display_name": alias or hostname or row.get("mac"),
        "last_ip": row.get("last_ip") or "",
        "connection": row.get("connection") or "",
        "owner_category": row.get("owner_category") or "",
        "device_type": device_type,
        "device_type_source": row.get("device_type_source") or "auto",
        "owner_source": row.get("owner_source") or "auto",
        "auto_role": auto_role,
        "nights_present": int(row.get("nights_present") or 0),
        "nights_absent": int(row.get("nights_absent") or 0),
        "first_seen": iso_local(first_seen),
        "last_seen": iso_local(last_seen),
        "currently_online": bool(row.get("currently_online")),
        "hidden": bool(row.get("hidden")),
        "notes": row.get("notes") or "",
        "is_roll": _is_roll_device(device_type, auto_role, row.get("owner_category") or ""),
    }


def _is_roll_device(device_type: str, auto_role: str, owner_category: str) -> bool:
    if device_type == "phone" or auto_role == "phone" or owner_category == "personal":
        return True
    return False


def list_devices() -> List[Dict[str, Any]]:
    ensure_schema()
    conn = _conn()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT * FROM attendance_devices
            ORDER BY currently_online DESC, alias ASC, hostname ASC, mac ASC
            """
        )
        return [_row_device(row) for row in (cur.fetchall() or [])]
    finally:
        _close(conn, cur)


def get_device(mac: str) -> Optional[Dict[str, Any]]:
    mac = normalize_mac(mac)
    if not mac:
        return None
    ensure_schema()
    conn = _conn()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM attendance_devices WHERE mac = %s", (mac,))
        row = cur.fetchone()
        return _row_device(row) if row else None
    finally:
        _close(conn, cur)


def update_device(mac: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mac = normalize_mac(mac)
    if not mac:
        return None
    fields = []
    args: List[Any] = []
    alias = payload.get("alias")
    if alias is not None:
        fields.append("alias = %s")
        args.append(str(alias).strip()[:128])
    if "owner_category" in payload:
        owner = str(payload.get("owner_category") or "").strip().lower()
        if owner and owner not in OWNER_CATEGORIES:
            owner = ""
        fields.append("owner_category = %s")
        fields.append("owner_source = %s")
        args.extend([owner, "user" if owner else "auto"])
    if "device_type" in payload:
        kind = str(payload.get("device_type") or "").strip().lower()
        if kind and kind not in DEVICE_TYPES:
            kind = ""
        fields.append("device_type = %s")
        fields.append("device_type_source = %s")
        args.extend([kind, "user" if kind else "auto"])
    if "hidden" in payload:
        fields.append("hidden = %s")
        args.append(1 if payload.get("hidden") else 0)
    if "notes" in payload:
        fields.append("notes = %s")
        args.append(str(payload.get("notes") or "").strip()[:512])
    if not fields:
        return get_device(mac)
    ensure_schema()
    conn = _conn()
    if not conn:
        raise RuntimeError("MySQL unavailable")
    cur = None
    try:
        cur = conn.cursor()
        args.append(mac)
        cur.execute(
            f"UPDATE attendance_devices SET {', '.join(fields)} WHERE mac = %s",
            tuple(args),
        )
        conn.commit()
    except MySQLError as err:
        conn.rollback()
        raise RuntimeError(str(err)) from err
    finally:
        _close(conn, cur)
    return get_device(mac)


def delete_device(mac: str) -> bool:
    """Remove a device and its sessions. MAC is the stable key; it can reappear on the next poll."""
    mac = normalize_mac(mac)
    if not mac:
        return False
    ensure_schema()
    conn = _conn()
    if not conn:
        raise RuntimeError("MySQL unavailable")
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM attendance_sessions WHERE mac = %s", (mac,))
        cur.execute("DELETE FROM attendance_devices WHERE mac = %s", (mac,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except MySQLError as err:
        conn.rollback()
        raise RuntimeError(str(err)) from err
    finally:
        _close(conn, cur)


def _finalize_night_row(
    row: Dict[str, Any], now_local: datetime, night_start: str, night_end: str
) -> Dict[str, Any]:
    bucket = (row.get("night_bucket") or "").strip()
    if not bucket:
        return row
    current = night_bucket_for(now_local, night_start) if is_night(now_local, night_start, night_end) else ""
    if bucket == current:
        return row
    first_local = to_local(row.get("first_seen_utc"))
    if first_local and first_local.date().isoformat() > bucket:
        row["night_bucket"] = None
        row["seen_this_night"] = 0
        return row
    if int(row.get("seen_this_night") or 0):
        row["nights_present"] = int(row.get("nights_present") or 0) + 1
    else:
        row["nights_absent"] = int(row.get("nights_absent") or 0) + 1
    row["auto_role"] = classify_auto_role(row["nights_present"], row["nights_absent"])
    row["night_bucket"] = None
    row["seen_this_night"] = 0
    return row


def apply_snapshot(
    devices: Sequence[Dict[str, Any]],
    *,
    complete: bool,
    source: str,
    error: str = "",
    now: Optional[datetime] = None,
    settings: Optional[Dict[str, Any]] = None,
    unpooled: bool = False,
    immediate_absent: bool = False,
) -> Dict[str, Any]:
    ensure_schema()
    settings = settings or get_settings()
    now_utc = now or utc_now()
    now_local = to_local(now_utc) or datetime.now(TZ)
    absent_after = timedelta(minutes=int(settings.get("absent_minutes") or DEFAULT_ABSENT_MINUTES))
    night_start = settings.get("night_start") or DEFAULT_NIGHT_START
    night_end = settings.get("night_end") or DEFAULT_NIGHT_END
    in_night = is_night(now_local, night_start, night_end)
    bucket = night_bucket_for(now_local, night_start) if in_night else ""
    seen = {normalize_mac(d.get("mac")) for d in devices}
    seen.discard("")
    phone_events: List[Dict[str, Any]] = []

    conn = _conn(unpooled=unpooled)
    if not conn:
        raise RuntimeError("MySQL unavailable")
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        prev_by_mac: Dict[str, Dict[str, Any]] = {}
        if complete:
            cur.execute(
                """
                SELECT mac, alias, hostname, last_ip, device_type, auto_role,
                       owner_category, currently_online, hidden, last_seen_utc
                FROM attendance_devices
                """
            )
            for row in cur.fetchall() or []:
                mac_key = normalize_mac(row.get("mac"))
                if mac_key:
                    prev_by_mac[mac_key] = row

        cur.execute(
            """
            INSERT INTO attendance_polls (polled_utc, source, complete, device_count, error_text)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (now_utc, source, 1 if complete else 0, len(seen), (error or "")[:512]),
        )
        cur.execute(
            "DELETE FROM attendance_polls WHERE polled_utc < %s",
            (now_utc - timedelta(days=14),),
        )

        for item in devices:
            mac = normalize_mac(item.get("mac"))
            if not mac:
                continue
            cur.execute("SELECT * FROM attendance_devices WHERE mac = %s", (mac,))
            row = cur.fetchone()
            prev = prev_by_mac.get(mac) if complete else None
            hidden = bool((prev or row or {}).get("hidden")) if (prev or row) else False
            hostname = prefer_hostname(
                (row.get("hostname") if row else "") or "",
                str(item.get("hostname") or ""),
            )
            ip = str(item.get("ip") or "").strip()[:45]
            connection = str(item.get("connection") or "").strip()[:64]
            guessed = guess_device_type(hostname, connection)
            if row:
                row = _finalize_night_row(row, now_local, night_start, night_end)
                if in_night:
                    if row.get("night_bucket") != bucket:
                        row["night_bucket"] = bucket
                        row["seen_this_night"] = 1
                    else:
                        row["seen_this_night"] = 1
                device_type = row.get("device_type") or ""
                device_type_source = row.get("device_type_source") or "auto"
                if device_type_source != "user" and guessed:
                    device_type = guessed
                    device_type_source = "auto"
                cur.execute(
                    """
                    UPDATE attendance_devices
                    SET hostname = CASE WHEN %s <> '' THEN %s ELSE hostname END,
                        last_ip = CASE WHEN %s <> '' THEN %s ELSE last_ip END,
                        connection = CASE WHEN %s <> '' THEN %s ELSE connection END,
                        device_type = %s,
                        device_type_source = %s,
                        auto_role = %s,
                        nights_present = %s,
                        nights_absent = %s,
                        night_bucket = %s,
                        seen_this_night = %s,
                        last_seen_utc = %s,
                        currently_online = 1
                    WHERE mac = %s
                    """,
                    (
                        hostname,
                        hostname,
                        ip,
                        ip,
                        connection,
                        connection,
                        device_type,
                        device_type_source,
                        row.get("auto_role") or "unknown",
                        int(row.get("nights_present") or 0),
                        int(row.get("nights_absent") or 0),
                        row.get("night_bucket"),
                        int(row.get("seen_this_night") or 0),
                        now_utc,
                        mac,
                    ),
                )
                notify_type = device_type
                notify_role = row.get("auto_role") or "unknown"
                notify_alias = row.get("alias") or ""
                notify_owner = row.get("owner_category") or ""
            else:
                cur.execute(
                    """
                    INSERT INTO attendance_devices (
                        mac, alias, hostname, last_ip, connection, owner_category, device_type,
                        device_type_source, owner_source, auto_role, nights_present, nights_absent,
                        night_bucket, seen_this_night, first_seen_utc, last_seen_utc,
                        currently_online, hidden, notes
                    ) VALUES (
                        %s, '', %s, %s, %s, '', %s, %s, 'auto', 'unknown', 0, 0,
                        %s, %s, %s, %s, 1, 0, ''
                    )
                    """,
                    (
                        mac,
                        hostname,
                        ip,
                        connection,
                        guessed,
                        "auto" if guessed else "auto",
                        bucket or None,
                        1 if in_night else 0,
                        now_utc,
                        now_utc,
                    ),
                )
                notify_type = guessed
                notify_role = "unknown"
                notify_alias = ""
                notify_owner = ""

            cur.execute(
                """
                SELECT id FROM attendance_sessions
                WHERE mac = %s AND ended_utc IS NULL
                ORDER BY started_utc DESC LIMIT 1
                """,
                (mac,),
            )
            open_row = cur.fetchone()
            if open_row:
                cur.execute(
                    "UPDATE attendance_sessions SET last_seen_utc = %s WHERE id = %s",
                    (now_utc, open_row["id"]),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO attendance_sessions (mac, started_utc, last_seen_utc, ended_utc, close_reason)
                    VALUES (%s, %s, %s, NULL, NULL)
                    """,
                    (mac, now_utc, now_utc),
                )
                # Slack join only when a NEW hours-session starts (survived leave grace).
                if (
                    complete
                    and not hidden
                    and is_phone_notify_candidate(notify_type, notify_role, hostname, notify_alias)
                ):
                    phone_events.append(
                        {
                            "action": "join",
                            "mac": mac,
                            "ip": ip or ((prev or {}).get("last_ip") if prev else "") or "",
                            "name": notify_alias or hostname or mac,
                            "hostname": hostname,
                            "owner_category": notify_owner,
                            "time": iso_local(now_utc),
                        }
                    )

        cur.execute(
            "SELECT mac, last_seen_utc, night_bucket, seen_this_night, nights_present, nights_absent, first_seen_utc, auto_role FROM attendance_devices"
        )
        for row in cur.fetchall() or []:
            updated = _finalize_night_row(row, now_local, night_start, night_end)
            if (
                updated.get("night_bucket") != row.get("night_bucket")
                or updated.get("nights_present") != row.get("nights_present")
                or updated.get("nights_absent") != row.get("nights_absent")
                or updated.get("auto_role") != row.get("auto_role")
            ):
                cur.execute(
                    """
                    UPDATE attendance_devices
                    SET auto_role = %s, nights_present = %s, nights_absent = %s,
                        night_bucket = %s, seen_this_night = %s
                    WHERE mac = %s
                    """,
                    (
                        updated.get("auto_role") or "unknown",
                        int(updated.get("nights_present") or 0),
                        int(updated.get("nights_absent") or 0),
                        updated.get("night_bucket"),
                        int(updated.get("seen_this_night") or 0),
                        row["mac"],
                    ),
                )

        if complete:
            # UI Online follows the router list immediately (no "stuck Online" during roam).
            # Slack leave waits for session grace (absent_minutes) so Deco flaps do not spam.
            cutoff = now_utc if immediate_absent else (now_utc - absent_after)
            if seen:
                format_ids = ",".join(["%s"] * len(seen))
                cur.execute(
                    f"""
                    UPDATE attendance_devices
                    SET currently_online = 1
                    WHERE mac IN ({format_ids})
                    """,
                    tuple(seen),
                )
                cur.execute(
                    f"""
                    UPDATE attendance_devices
                    SET currently_online = 0
                    WHERE currently_online = 1 AND mac NOT IN ({format_ids})
                    """,
                    tuple(seen),
                )
            else:
                cur.execute("UPDATE attendance_devices SET currently_online = 0 WHERE currently_online = 1")

            cur.execute(
                """
                SELECT s.id, s.mac, s.last_seen_utc
                FROM attendance_sessions s
                WHERE s.ended_utc IS NULL
                """
            )
            for row in cur.fetchall() or []:
                mac = row["mac"]
                last_seen = row["last_seen_utc"]
                if mac in seen:
                    continue
                if not immediate_absent and last_seen and last_seen > cutoff:
                    continue
                cur.execute(
                    """
                    UPDATE attendance_sessions
                    SET ended_utc = last_seen_utc, close_reason = 'absent'
                    WHERE id = %s AND ended_utc IS NULL
                    """,
                    (row["id"],),
                )
                prev = prev_by_mac.get(mac) or {}
                if prev.get("hidden"):
                    continue
                if not is_phone_notify_candidate(
                    prev.get("device_type") or "",
                    prev.get("auto_role") or "",
                    prev.get("hostname") or "",
                    prev.get("alias") or "",
                ):
                    continue
                phone_events.append(
                    {
                        "action": "leave",
                        "mac": mac,
                        "ip": prev.get("last_ip") or "",
                        "name": (prev.get("alias") or prev.get("hostname") or mac),
                        "hostname": prev.get("hostname") or "",
                        "owner_category": prev.get("owner_category") or "",
                        "time": iso_local(last_seen) or iso_local(now_utc),
                    }
                )
        else:
            # Degraded poll: never check people out. Keep currently_online from last complete view
            # unless we just saw them.
            if seen:
                format_ids = ",".join(["%s"] * len(seen))
                cur.execute(
                    f"UPDATE attendance_devices SET currently_online = 1 WHERE mac IN ({format_ids})",
                    tuple(seen),
                )

        cur.execute("DELETE FROM attendance_sessions WHERE started_utc < %s", (now_utc - timedelta(days=180),))
        conn.commit()
        if phone_events:
            try:
                from py.utilities.slack_notify import notify_phone_wifi_events

                notify_phone_wifi_events(phone_events)
            except Exception:
                pass
        return {
            "ok": True,
            "complete": complete,
            "source": source,
            "device_count": len(seen),
            "error": error,
            "polled_at": iso_local(now_utc),
            "phone_events": len(phone_events),
        }
    except MySQLError as err:
        try:
            conn.rollback()
        except Exception:
            pass
        raise RuntimeError(str(err)) from err
    finally:
        _close(conn, cur)


def known_ips() -> List[str]:
    ensure_schema()
    conn = _conn()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT last_ip FROM attendance_devices WHERE last_ip IS NOT NULL AND last_ip <> ''")
        return [str(row[0]) for row in (cur.fetchall() or []) if row and row[0]]
    finally:
        _close(conn, cur)


def last_poll() -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn = _conn()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT polled_utc, source, complete, device_count, error_text
            FROM attendance_polls
            ORDER BY id DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "polled_at": iso_local(row.get("polled_utc")),
            "source": row.get("source") or "",
            "complete": bool(row.get("complete")),
            "device_count": int(row.get("device_count") or 0),
            "error": row.get("error_text") or "",
        }
    finally:
        _close(conn, cur)


def list_sessions(
    mac: str,
    days: int = 14,
    *,
    start_local: Optional[datetime] = None,
    end_local: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    mac = normalize_mac(mac)
    if not mac:
        return []
    ensure_schema()
    conn = _conn()
    if not conn:
        return []
    cur = None
    if start_local is not None and end_local is not None:
        start = to_utc_naive(start_local)
        end = to_utc_naive(end_local)
    else:
        start = utc_now() - timedelta(days=max(1, min(int(days), 180)))
        end = utc_now() + timedelta(days=1)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, started_utc, last_seen_utc, ended_utc, close_reason
            FROM attendance_sessions
            WHERE mac = %s
              AND last_seen_utc >= %s
              AND started_utc < %s
            ORDER BY started_utc DESC
            """,
            (mac, start, end),
        )
        out = []
        for row in cur.fetchall() or []:
            started = row["started_utc"]
            ended = row["ended_utc"] or utc_now()
            last_seen = row["last_seen_utc"]
            seconds = max(0, int((ended - started).total_seconds())) if started and ended else 0
            out.append(
                {
                    "id": row["id"],
                    "started": iso_local(started),
                    "last_seen": iso_local(last_seen),
                    "ended": iso_local(row["ended_utc"]) if row["ended_utc"] else "",
                    "open": row["ended_utc"] is None,
                    "close_reason": row["close_reason"] or "",
                    "seconds": seconds,
                }
            )
        return out
    finally:
        _close(conn, cur)


def _parse_chart_range(
    month: str = "",
    start: str = "",
    end: str = "",
) -> Tuple[datetime, datetime, str]:
    """Return local start/end datetimes and a display month key YYYY-MM."""
    now_local = datetime.now(TZ).replace(microsecond=0)
    month_key = (month or "").strip()
    start_s = (start or "").strip()
    end_s = (end or "").strip()
    if start_s and end_s:
        try:
            start_d = datetime.strptime(start_s[:10], "%Y-%m-%d").date()
            end_d = datetime.strptime(end_s[:10], "%Y-%m-%d").date()
            if end_d < start_d:
                start_d, end_d = end_d, start_d
            if (end_d - start_d).days > 92:
                end_d = start_d + timedelta(days=92)
            start_local = datetime(start_d.year, start_d.month, start_d.day, tzinfo=TZ)
            end_local = datetime(end_d.year, end_d.month, end_d.day, tzinfo=TZ) + timedelta(days=1)
            return start_local, end_local, start_d.strftime("%Y-%m")
        except ValueError:
            pass
    if month_key:
        try:
            year_s, mon_s = month_key.split("-", 1)
            year, mon = int(year_s), int(mon_s)
            start_local = datetime(year, mon, 1, tzinfo=TZ)
            if mon == 12:
                end_local = datetime(year + 1, 1, 1, tzinfo=TZ)
            else:
                end_local = datetime(year, mon + 1, 1, tzinfo=TZ)
            return start_local, end_local, f"{year:04d}-{mon:02d}"
        except ValueError:
            pass
    start_local = datetime(now_local.year, now_local.month, 1, tzinfo=TZ)
    if now_local.month == 12:
        end_local = datetime(now_local.year + 1, 1, 1, tzinfo=TZ)
    else:
        end_local = datetime(now_local.year, now_local.month + 1, 1, tzinfo=TZ)
    return start_local, end_local, now_local.strftime("%Y-%m")


def chart_payload(
    mac: str,
    days: int = 14,
    *,
    month: str = "",
    start: str = "",
    end: str = "",
) -> Dict[str, Any]:
    start_local, end_local, month_key = _parse_chart_range(month=month, start=start, end=end)
    if month or start or end:
        sessions = list_sessions(mac, start_local=start_local, end_local=end_local)
    else:
        # Legacy days window ending today.
        now_local = datetime.now(TZ).replace(microsecond=0)
        end_local = datetime(now_local.year, now_local.month, now_local.day, tzinfo=TZ) + timedelta(days=1)
        start_local = end_local - timedelta(days=max(1, min(int(days), 92)))
        month_key = start_local.strftime("%Y-%m")
        sessions = list_sessions(mac, start_local=start_local, end_local=end_local)

    now_local = datetime.now(TZ).replace(microsecond=0)
    session_rows = []
    for item in sessions:
        try:
            started = datetime.strptime(item["started"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            end_text = item["ended"] or now_local.strftime("%Y-%m-%d %H:%M:%S")
            ended = datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        session_rows.append((started, ended, item["open"]))

    day = start_local.date()
    end_day = (end_local - timedelta(seconds=1)).date()
    total_hours = 0.0
    day_labels: List[str] = []
    day_display: List[str] = []
    hours: List[float] = []
    day_ranges: List[List[List[float]]] = []
    while day <= end_day:
        window_start = datetime(day.year, day.month, day.day, tzinfo=TZ)
        window_end = window_start + timedelta(days=1)
        total = 0.0
        ranges: List[List[float]] = []
        for started, ended, _open in session_rows:
            overlap = session_overlap_seconds(started, ended, window_start, window_end)
            if overlap <= 0:
                continue
            total += overlap
            seg_start = max(started, window_start)
            seg_end = min(ended, window_end)
            start_h = (
                seg_start.hour
                + seg_start.minute / 60.0
                + seg_start.second / 3600.0
            )
            if seg_end >= window_end:
                end_h = 24.0
            else:
                end_h = (
                    seg_end.hour
                    + seg_end.minute / 60.0
                    + seg_end.second / 3600.0
                )
            if end_h <= start_h:
                end_h = min(24.0, start_h + max(overlap / 3600.0, 1.0 / 60.0))
            ranges.append([round(start_h, 3), round(min(24.0, end_h), 3)])
        hours_val = round(total / 3600.0, 2)
        day_labels.append(day.isoformat())
        day_display.append(day.strftime("%m/%d"))
        hours.append(hours_val)
        day_ranges.append(ranges)
        total_hours += hours_val
        day += timedelta(days=1)

    return {
        "month": month_key,
        "start": start_local.date().isoformat(),
        "end": end_day.isoformat(),
        "days": day_labels,
        "labels": day_display,
        "hours": hours,
        "ranges": day_ranges,
        "total_hours": round(total_hours, 2),
        "sessions": sessions,
    }


def today_window(now_local: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    local = now_local or datetime.now(TZ)
    start = datetime(local.year, local.month, local.day, tzinfo=TZ)
    return to_utc_naive(start), to_utc_naive(start + timedelta(days=1))


def _local_hour_of(dt: datetime) -> float:
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0


def session_hour_ranges_for_day(
    sessions: List[Tuple[datetime, datetime]],
    day_start_local: datetime,
    *,
    now_local: Optional[datetime] = None,
) -> List[List[float]]:
    """Clip local session intervals to one calendar day and return [[start_h, end_h], ...]."""
    day_end = day_start_local + timedelta(days=1)
    now = now_local or datetime.now(TZ)
    ranges: List[List[float]] = []
    for started, ended in sessions:
        end = ended if ended is not None else now
        if end < started:
            end = started
        overlap = session_overlap_seconds(started, end, day_start_local, day_end)
        if overlap <= 0:
            continue
        seg_start = max(started, day_start_local)
        seg_end = min(end, day_end)
        start_h = _local_hour_of(seg_start)
        if seg_end >= day_end:
            end_h = 24.0
        else:
            end_h = _local_hour_of(seg_end)
        if end_h <= start_h:
            end_h = min(24.0, start_h + max(overlap / 3600.0, 1.0 / 60.0))
        ranges.append([round(start_h, 3), round(min(24.0, end_h), 3)])
    ranges.sort(key=lambda pair: pair[0])
    return ranges


def attach_today_bounds(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not devices:
        return devices
    now_local = datetime.now(TZ).replace(microsecond=0)
    day_start_local = datetime(now_local.year, now_local.month, now_local.day, tzinfo=TZ)
    start_utc, end_utc = today_window(now_local)
    macs = [d["mac"] for d in devices]
    ensure_schema()
    conn = _conn()
    if not conn:
        for device in devices:
            device.setdefault("first_today", "")
            device.setdefault("last_today", "")
            device.setdefault("today_ranges", [])
        return devices
    cur = None
    # first/last for labels are clipped to calendar today (not overnight session start).
    bounds: Dict[str, Tuple[datetime, datetime]] = {}
    by_mac: Dict[str, List[Tuple[datetime, datetime]]] = {mac: [] for mac in macs}
    day_end_local = day_start_local + timedelta(days=1)
    try:
        cur = conn.cursor()
        format_ids = ",".join(["%s"] * len(macs))
        cur.execute(
            f"""
            SELECT mac, started_utc, last_seen_utc, ended_utc
            FROM attendance_sessions
            WHERE mac IN ({format_ids})
              AND last_seen_utc >= %s
              AND started_utc < %s
            ORDER BY started_utc ASC
            """,
            tuple(macs) + (start_utc, end_utc),
        )
        for mac, started_utc, last_seen_utc, ended_utc in cur.fetchall() or []:
            started = to_local(started_utc)
            last_seen = to_local(last_seen_utc)
            ended = to_local(ended_utc) if ended_utc else None
            if not started or not last_seen:
                continue
            # Open sessions extend to now so Online rows reach the current-time marker.
            end_local = now_local if ended_utc is None else (ended or last_seen)
            by_mac.setdefault(mac, []).append((started, end_local))
            # Label bounds: only the portion that falls on today's calendar date.
            if session_overlap_seconds(started, end_local, day_start_local, day_end_local) <= 0:
                continue
            seg_start = max(started, day_start_local)
            seg_end = min(end_local, day_end_local)
            first_bound_utc = to_utc_naive(seg_start)
            last_bound_utc = to_utc_naive(seg_end)
            prev = bounds.get(mac)
            if not prev:
                bounds[mac] = (first_bound_utc, last_bound_utc)
            else:
                first_utc, last_utc = prev
                if first_bound_utc < first_utc:
                    first_utc = first_bound_utc
                if last_bound_utc > last_utc:
                    last_utc = last_bound_utc
                bounds[mac] = (first_utc, last_utc)
    finally:
        _close(conn, cur)
    for device in devices:
        first, last = bounds.get(device["mac"], (None, None))
        device["first_today"] = iso_local(first)
        device["last_today"] = iso_local(last)
        device["today_ranges"] = session_hour_ranges_for_day(
            by_mac.get(device["mac"]) or [],
            day_start_local,
            now_local=now_local,
        )
    return devices


def summary_payload() -> Dict[str, Any]:
    devices = attach_today_bounds(list_devices())
    online = [d for d in devices if d["currently_online"] and not d["hidden"]]
    roll = [d for d in devices if d["is_roll"] and not d["hidden"]]
    phones_here = [d for d in roll if d["currently_online"]]
    services = [d for d in devices if d["auto_role"] == "warehouse_service" and not d["hidden"]]
    unknown = [d for d in devices if not d["device_type"] and d["auto_role"] == "unknown" and not d["hidden"]]
    poll = last_poll() or {}
    return {
        "settings": public_settings(),
        "poll": poll,
        "counts": {
            "online": len(online),
            "total": len([d for d in devices if not d["hidden"]]),
            "roll_present": len(phones_here),
            "roll": len(roll),
            "services": len(services),
            "unknown": len(unknown),
        },
        "devices": devices,
        "server_now": iso_local(utc_now()),
    }


def run_poll(
    *,
    sweep: bool = False,
    unpooled: bool = True,
    immediate_absent: bool = False,
    enrich_lan_names: bool = True,
) -> Dict[str, Any]:
    settings = get_settings()
    extra_ips = known_ips()
    if sweep:
        skip = {settings["router_host"], lan_ip_of_this_host()}
        extra_ips = list(dict.fromkeys(extra_ips + subnet_ips(settings.get("subnet") or DEFAULT_SUBNET, skip=skip)))
        ping_hosts(extra_ips)
        extra_ips = None
    snapshot = fetch_office_devices(
        host=settings["router_host"],
        username=settings["router_user"],
        password=settings["router_password"],
        extra_ips=extra_ips,
        # Background polls skip mDNS/reverse-DNS so a stuck name lookup cannot freeze presence.
        enrich_lan_names=enrich_lan_names,
        mdns_timeout=1.5 if enrich_lan_names else 0.0,
    )
    if not snapshot.get("complete") and not settings.get("password_set"):
        snapshot["complete"] = True
        if snapshot.get("source") == "none":
            snapshot["source"] = "arp"
    result = apply_snapshot(
        snapshot.get("devices") or [],
        complete=bool(snapshot.get("complete")),
        source=str(snapshot.get("source") or "none"),
        error=str(snapshot.get("error") or ""),
        settings=settings,
        unpooled=unpooled,
        immediate_absent=immediate_absent,
    )
    result["url"] = snapshot.get("url") or ""
    return result


def test_router_login() -> Dict[str, Any]:
    settings = get_settings()
    snapshot = fetch_office_devices(
        host=settings["router_host"],
        username=settings["router_user"],
        password=settings["router_password"],
    )
    return {
        "ok": bool(snapshot.get("complete")),
        "source": snapshot.get("source"),
        "error": snapshot.get("error") or "",
        "device_count": len(snapshot.get("devices") or []),
        "url": snapshot.get("url") or "",
        "password_set": bool(settings.get("password_set")),
        "arp_count": len(read_arp_table()),
    }
