"""
Detect whether the current HTTP request is coming from a private / LAN context.

Used to show a sitewide hint and to avoid forcing public hostname behavior when
the app is opened via the server's LAN address (e.g. 192.168.x.x) instead of
the Cloudflare tunnel / public URL.
"""


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
