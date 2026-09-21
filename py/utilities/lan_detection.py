"""
Detect whether the current HTTP request is coming from a private / LAN context.

Used to show a sitewide hint and to avoid forcing public hostname behavior when
the app is opened via the server's LAN address (e.g. 192.168.x.x) instead of
the Cloudflare tunnel / public URL.

Office Wi-Fi is 192.168.254.0/24. HTTP never carries a MAC; for local clients we
resolve MAC from the attendance table (router poll) or the server ARP cache.

Logins on https://www.fdcef.com only see the office WAN IP. The login page pings
http://192.168.254.30:5555 (same trick as Trade Show Tool) so Flask can record the
real 192.168.254.x peer before the password POST.
"""

from __future__ import annotations

import re
import threading
import time


def _ipv4_octets(ip: str):
    if not ip:
        return None
    ip = ip.strip().split('%')[0]  # zone id
    parts = ip.split('.')
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


def ipv4_is_private(ip: str) -> bool:
    """True for RFC1918, loopback, and link-local."""
    o = _ipv4_octets(ip)
    if not o:
        return False
    a, b = o[0], o[1]
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 127:
        return True
    if a == 169 and b == 254:
        return True
    return False


def host_is_private_lan(hostname: str) -> bool:
    """True if Host header is localhost or a private IPv4 literal."""
    if not hostname:
        return False
    h = hostname.strip().lower().partition(':')[0]
    if h in ('localhost', '::1'):
        return True
    return ipv4_is_private(h)


def get_effective_client_ip(request) -> str:
    xff = request.headers.get('X-Forwarded-For') if request else None
    if xff:
        return xff.split(',')[0].strip()
    try:
        return (request.remote_addr or '').strip()
    except RuntimeError:
        return ''


def client_ip_is_private_lan(request) -> bool:
    """Heuristic: first X-Forwarded-For or remote_addr is a private IPv4."""
    return ipv4_is_private(get_effective_client_ip(request))


OFFICE_LAN_PREFIX = "192.168.254."


def _first_ip(value: str) -> str:
    return (value or "").split(",")[0].strip().split("%")[0]


def is_office_lan_ip(ip: str) -> bool:
    """True for the office subnet 192.168.254.1–254."""
    o = _ipv4_octets(_first_ip(ip))
    if not o:
        return False
    return o[0] == 192 and o[1] == 168 and o[2] == 254 and 1 <= o[3] <= 254


_PROBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_LAN_PROBE_TTL_SEC = 300
_LAN_PROBES = {}
_LAN_PROBE_LOCK = threading.Lock()


def _is_loopback_ip(ip: str) -> bool:
    ip = _first_ip(ip).lower()
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    o = _ipv4_octets(ip)
    return bool(o and o[0] == 127)


def direct_peer_ip(request) -> str:
    """TCP peer before ProxyFix rewrites it to CF-Connecting-IP / X-Forwarded-For."""
    orig = ""
    try:
        orig = str(request.environ.get("werkzeug.proxy_fix.orig_remote_addr") or "").strip()
    except Exception:
        orig = ""
    if orig:
        return _first_ip(orig)
    try:
        return _first_ip(request.remote_addr or "")
    except RuntimeError:
        return ""


def _prune_lan_probes(now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    expired = [key for key, item in _LAN_PROBES.items() if item[1] <= stamp]
    for key in expired:
        _LAN_PROBES.pop(key, None)


def remember_lan_probe(probe_id: str, lan_ip: str) -> None:
    if not _PROBE_ID_RE.match(probe_id or "") or not is_office_lan_ip(lan_ip):
        return
    with _LAN_PROBE_LOCK:
        _prune_lan_probes()
        _LAN_PROBES[probe_id] = (_first_ip(lan_ip), time.time() + _LAN_PROBE_TTL_SEC)


def consume_lan_probe(probe_id: str) -> str:
    if not _PROBE_ID_RE.match(probe_id or ""):
        return ""
    with _LAN_PROBE_LOCK:
        _prune_lan_probes()
        item = _LAN_PROBES.pop(probe_id, None)
    if not item:
        return ""
    lan_ip, exp = item
    if exp <= time.time() or not is_office_lan_ip(lan_ip):
        return ""
    return lan_ip


def record_lan_beacon_from_request(request, probe_id: str) -> str:
    """Record the office LAN IP from a direct hit on :5555 (not the Cloudflare host)."""
    if not _PROBE_ID_RE.match(probe_id or ""):
        return ""
    host = ""
    try:
        host = request.host or ""
    except Exception:
        host = ""
    if not host_is_private_lan(host):
        return ""
    peer = direct_peer_ip(request)
    lan_ip = ""
    if is_office_lan_ip(peer):
        lan_ip = _first_ip(peer)
    elif _is_loopback_ip(peer):
        try:
            from py.utilities.attendance_netgear import lan_ip_of_this_host

            lan_ip = lan_ip_of_this_host(OFFICE_LAN_PREFIX)
        except Exception:
            lan_ip = ""
    if not is_office_lan_ip(lan_ip):
        return ""
    remember_lan_probe(probe_id, lan_ip)
    return lan_ip


def consume_lan_probe_from_request(request) -> str:
    probe_id = ""
    try:
        probe_id = str((request.form.get("lan_probe_id") or "")).strip()
    except Exception:
        probe_id = ""
    if not probe_id:
        try:
            if getattr(request, "is_json", False):
                body = request.get_json(silent=True) or {}
                probe_id = str(body.get("lan_probe_id") or "").strip()
        except Exception:
            probe_id = ""
    return consume_lan_probe(probe_id)


def reported_office_lan_ip(request) -> str:
    """Client-supplied office LAN IP (login form / JSON). Ignore anything else."""
    raw = ""
    try:
        raw = str((request.form.get("lan_ip") if request.form else "") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        try:
            if getattr(request, "is_json", False):
                body = request.get_json(silent=True) or {}
                raw = str(body.get("lan_ip") or "").strip()
        except Exception:
            raw = ""
    return _first_ip(raw) if is_office_lan_ip(raw) else ""


def iter_request_ips(request) -> list:
    """Unique IPs from proxy headers, remote_addr, and a trusted office lan_ip field."""
    seen = set()
    ips = []

    def add(raw):
        for part in str(raw or "").split(","):
            ip = part.strip().split("%")[0]
            if not ip or ip in seen:
                continue
            seen.add(ip)
            ips.append(ip)

    if request is None:
        return ips
    for header in (
        "CF-Connecting-IP",
        "True-Client-IP",
        "X-Real-IP",
        "X-Forwarded-For",
    ):
        try:
            add(request.headers.get(header))
        except Exception:
            continue
    try:
        add(request.remote_addr)
    except RuntimeError:
        pass
    try:
        for hop in getattr(request, "access_route", None) or []:
            add(hop)
    except Exception:
        pass
    add(reported_office_lan_ip(request))
    return ips


def office_lan_ip_from_request(request) -> str:
    """Office LAN IP from the :5555 beacon, then headers / reported lan_ip."""
    probed = consume_lan_probe_from_request(request)
    if probed:
        return probed
    for ip in iter_request_ips(request):
        if is_office_lan_ip(ip):
            return _first_ip(ip)
    return ""


def lookup_office_mac(lan_ip: str, arp_rows=None) -> str:
    """MAC for an office LAN IP from attendance devices, then ARP."""
    ip = _first_ip(lan_ip)
    if not is_office_lan_ip(ip):
        return ""
    try:
        from py.utilities.attendance_store import get_device_by_lan_ip

        device = get_device_by_lan_ip(ip)
        mac = str((device or {}).get("mac") or "").strip()
        if mac:
            return mac
    except Exception:
        pass
    rows = arp_rows
    if rows is None:
        try:
            from py.utilities.attendance_netgear import read_arp_table

            rows = read_arp_table(OFFICE_LAN_PREFIX)
        except Exception:
            rows = []
    for row in rows or []:
        if str((row or {}).get("ip") or "").strip() != ip:
            continue
        mac = str((row or {}).get("mac") or "").strip()
        if mac:
            return mac
    return ""


def resolve_office_lan_identity(request) -> tuple:
    """(lan_ip, mac) when the login is from the office subnet; else empty strings."""
    lan_ip = office_lan_ip_from_request(request)
    if not lan_ip:
        return "", ""
    return lan_ip, lookup_office_mac(lan_ip)


def office_lan_log_fragment(client_ip: str, lan_ip: str, mac: str) -> str:
    """Insert after 'IP: ...; ' in the login log line."""
    bits = []
    public = _first_ip(client_ip)
    if lan_ip and lan_ip != public:
        bits.append(f"LAN: {lan_ip}")
    if mac:
        bits.append(f"MAC: {mac}")
    if not bits:
        return ""
    return "; ".join(bits) + "; "
