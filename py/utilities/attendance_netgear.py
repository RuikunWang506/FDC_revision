"""Local office device discovery: NETGEAR R6700v2 SOAP, plus ARP fallback."""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NETGEAR_SESSION_ID = "A7D88AE69687E58D9A00"
SOAP_NS_DEVICE_INFO = "urn:NETGEAR-ROUTER:service:DeviceInfo:1"
SOAP_NS_PARENTAL = "urn:NETGEAR-ROUTER:service:ParentalControl:1"
SOAP_NS_DEVICE_CONFIG = "urn:NETGEAR-ROUTER:service:DeviceConfig:1"

_MAC_RE = re.compile(r"^([0-9a-f]{2}[-:]){5}[0-9a-f]{2}$", re.I)
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_ARP_LINE_RE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-f]{2}(?:[-:][0-9a-f]{2}){5})\s+(\S+)",
    re.I,
)
_WIRELESS_HINTS = frozenset(
    {
        "wireless",
        "wifi",
        "2.4ghz",
        "5ghz",
        "5ghz-1",
        "5ghz-2",
        "6ghz",
        "2.4g",
        "5g",
        "6g",
    }
)


def normalize_mac(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", ":")
    if not raw:
        return ""
    if _MAC_RE.match(raw):
        parts = raw.replace("-", ":").split(":")
        return ":".join(p.zfill(2) for p in parts)
    hex_only = re.sub(r"[^0-9a-f]", "", raw)
    if len(hex_only) == 12:
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return ""


def is_ipv4(value: Any) -> bool:
    text = str(value or "").strip()
    if not _IPV4_RE.match(text):
        return False
    try:
        ipaddress.IPv4Address(text)
        return True
    except ipaddress.AddressValueError:
        return False


def _local_tag(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _inner_xml(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*([^<]*)\s*</{tag}>", block or "", re.I)
    return (match.group(1).strip() if match else "") or ""


def parse_device_xml_blocks(text: str) -> List[Dict[str, str]]:
    """Pull complete <Device> blocks even when the router truncates the SOAP XML."""
    devices: List[Dict[str, str]] = []
    seen = set()
    for block in re.findall(r"<Device>(.*?)</Device>", text or "", re.I | re.S):
        mac = normalize_mac(_inner_xml(block, "MAC") or _inner_xml(block, "MACAddress"))
        if not mac or mac in seen:
            continue
        seen.add(mac)
        devices.append(
            {
                "mac": mac,
                "hostname": _inner_xml(block, "Name") or _inner_xml(block, "DeviceName"),
                "ip": _inner_xml(block, "IP") or _inner_xml(block, "IPAddress"),
                "connection": _inner_xml(block, "ConnectionType") or _inner_xml(block, "Type"),
                "link_rate": _inner_xml(block, "Linkspeed") or _inner_xml(block, "LinkRate"),
                "signal": _inner_xml(block, "SignalStrength") or _inner_xml(block, "Signal"),
            }
        )
    return devices


def parse_semicolon_attached_devices(text: str) -> List[Dict[str, str]]:
    """Parse R6700 GetAttachDevice: count@n;ip;name;mac;type;link;signal;Allow@..."""
    devices: List[Dict[str, str]] = []
    seen = set()
    for chunk in re.findall(r"@\d+;([^@]+)", text or ""):
        fields = chunk.split(";")
        if len(fields) < 4:
            continue
        ip, name, mac_raw = fields[0].strip(), fields[1].strip(), fields[2].strip()
        mac = normalize_mac(mac_raw)
        if not mac or mac in seen:
            continue
        seen.add(mac)
        devices.append(
            {
                "mac": mac,
                "hostname": name,
                "ip": ip if is_ipv4(ip) else "",
                "connection": fields[3].strip() if len(fields) > 3 else "",
                "link_rate": fields[4].strip() if len(fields) > 4 else "",
                "signal": fields[5].strip() if len(fields) > 5 else "",
            }
        )
    return devices


def parse_at_delimited_devices(text: str) -> List[Dict[str, str]]:
    """Parse NETGEAR GetAttachDevice @-delimited payloads across firmware variants."""
    tokens = [t.strip() for t in (text or "").split("@")]
    tokens = [t for t in tokens if t != ""]
    devices: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None

    def _flush() -> None:
        nonlocal current
        if current and current.get("mac"):
            devices.append(current)
        current = None

    for tok in tokens:
        mac = normalize_mac(tok)
        if mac and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            _flush()
            current = {
                "mac": mac,
                "hostname": "",
                "ip": "",
                "connection": "",
                "link_rate": "",
                "signal": "",
            }
            continue
        if current is None:
            continue
        low = tok.lower()
        if is_ipv4(tok) and not current["ip"]:
            current["ip"] = tok
        elif low in _WIRELESS_HINTS or low in {"wired", "ethernet", "unknown"}:
            current["connection"] = tok
        elif tok.isdigit() and not current["link_rate"]:
            current["link_rate"] = tok
        elif tok.isdigit() and not current["signal"]:
            current["signal"] = tok
        elif not current["hostname"] and low not in {"allow", "block", "allowed", "blocked"}:
            current["hostname"] = tok
    _flush()
    return devices


def parse_netgear_device_xml(xml_text: str) -> List[Dict[str, str]]:
    blob = (xml_text or "").replace("<unknown>", "unknown").strip()
    if not blob:
        return []
    devices: List[Dict[str, str]] = []
    devices.extend(parse_device_xml_blocks(blob))
    devices.extend(parse_semicolon_attached_devices(blob))
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        wrapped = f"<root>{blob}</root>"
        try:
            root = ET.fromstring(wrapped)
        except ET.ParseError:
            devices.extend(parse_at_delimited_devices(blob))
            return merge_device_lists(devices)

    for node in root.iter():
        tag = _local_tag(node.tag).lower()
        if tag not in {"device", "attachdevice", "newattachdevice"}:
            continue
        fields = {_local_tag(child.tag).lower(): (child.text or "").strip() for child in list(node)}
        if not fields:
            if (node.text or "").strip():
                devices.extend(parse_at_delimited_devices(node.text or ""))
            continue
        mac = normalize_mac(
            fields.get("mac")
            or fields.get("macaddress")
            or fields.get("deviceid")
            or fields.get("newmacaddress")
        )
        if not mac:
            continue
        ip = (
            fields.get("ip")
            or fields.get("ipaddress")
            or fields.get("ipv4")
            or fields.get("newipaddress")
            or ""
        )
        hostname = (
            fields.get("name")
            or fields.get("devicename")
            or fields.get("hostname")
            or fields.get("newdevicename")
            or ""
        )
        connection = (
            fields.get("connectiontype")
            or fields.get("connection")
            or fields.get("type")
            or fields.get("newconnectiontype")
            or ""
        )
        devices.append(
            {
                "mac": mac,
                "hostname": hostname,
                "ip": ip,
                "connection": connection,
                "link_rate": fields.get("linkrate") or fields.get("linkspeed") or "",
                "signal": fields.get("signalstrength") or fields.get("rssi") or "",
            }
        )
    devices.extend(parse_at_delimited_devices(blob))
    return merge_device_lists(devices)


SOAP_ENVELOPE = """<?xml version="1.0" encoding="utf-8" standalone="no"?>
<SOAP-ENV:Envelope xmlns:SOAPSDK1="http://www.w3.org/2001/XMLSchema"
  xmlns:SOAPSDK2="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:SOAPSDK3="http://schemas.xmlsoap.org/soap/encoding/"
  xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
<SOAP-ENV:Header>
<SessionID>{session_id}</SessionID>
</SOAP-ENV:Header>
{body}
</SOAP-ENV:Envelope>"""

LOGIN_V1_BODY = """<SOAP-ENV:Body>
<Authenticate>
  <NewUsername>{username}</NewUsername>
  <NewPassword>{password}</NewPassword>
</Authenticate>
</SOAP-ENV:Body>"""

CALL_BODY = """<SOAP-ENV:Body>
<M1:{method} xmlns:M1="{service}">
{params}</M1:{method}>
</SOAP-ENV:Body>"""

_OK_CODES = frozenset({"0", "00", "000", "0000", "1", "001", "2", "002", "3", "003"})


def soap_login_v1_body(username: str, password: str) -> str:
    return LOGIN_V1_BODY.format(
        username=xml_escape(username or ""),
        password=xml_escape(password or ""),
    )


def _soap_response_code(xml_text: str) -> str:
    match = re.search(r"<ResponseCode>\s*([^<]+)\s*</ResponseCode>", xml_text or "", re.I)
    return (match.group(1).strip() if match else "") or ""


def _soap_ok(xml_text: str, status_code: int) -> bool:
    if status_code == 401:
        return False
    code = _soap_response_code(xml_text)
    if code in _OK_CODES:
        return True
    if re.search(r">\s*401\s*<", xml_text or ""):
        return False
    return bool(re.search(r"\s0+</", xml_text or ""))


def _extract_tag(xml_text: str, name: str) -> str:
    match = re.search(rf"<{name}[^>]*>\s*(.*?)\s*</{name}>", xml_text or "", re.I | re.S)
    return match.group(1).strip() if match else ""


class NetgearSoapClient:
    """R6700v2 SOAP client matching the Genie / pynetgear login (v2 cookie, then v1)."""

    def __init__(self, host: str, username: str, password: str, timeout: int = 15):
        self.host = (host or "").strip() or "192.168.254.1"
        self.username = (username or "").strip() or "admin"
        self.password = password or ""
        self.timeout = timeout
        self.cookie: Optional[str] = None
        self._http = requests.Session()

    def urls(self) -> List[str]:
        host = self.host
        return [
            f"http://{host}:5000/soap/server_sa/",
            f"http://{host}/soap/server_sa/",
            f"https://{host}/soap/server_sa/",
        ]

    def _headers(self, service: str, method: str, use_cookie: bool = True) -> Dict[str, str]:
        headers = {
            "SOAPAction": f"urn:NETGEAR-ROUTER:service:{service}#{method}",
            "Cache-Control": "no-cache",
            "User-Agent": "SOAP Toolkit 3.0",
            "Content-Type": "multipart/form-data",
        }
        if use_cookie and isinstance(self.cookie, str) and self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _message(self, body: str) -> str:
        return SOAP_ENVELOPE.format(session_id=NETGEAR_SESSION_ID, body=body)

    def _post(self, url: str, service: str, method: str, body: str, use_cookie: bool = True):
        return self._http.post(
            url,
            headers=self._headers(service, method, use_cookie=use_cookie),
            data=self._message(body).encode("utf-8"),
            timeout=self.timeout,
            verify=False,
        )

    def login_v2(self, url: str) -> bool:
        body = CALL_BODY.format(
            method="SOAPLogin",
            service=SOAP_NS_DEVICE_CONFIG,
            params=(
                f"<Username>{xml_escape(self.username)}</Username>"
                f"<Password>{xml_escape(self.password)}</Password>"
            ),
        )
        resp = self._post(url, "DeviceConfig:1", "SOAPLogin", body, use_cookie=False)
        if not _soap_ok(resp.text, resp.status_code):
            return False
        cookie = resp.headers.get("Set-Cookie") or ""
        if not cookie:
            return False
        self.cookie = cookie
        return True

    def login_v1(self, url: str) -> bool:
        body = soap_login_v1_body(self.username, self.password)
        resp = self._post(url, "ParentalControl:1", "Authenticate", body, use_cookie=False)
        if not _soap_ok(resp.text, resp.status_code):
            return False
        self.cookie = True
        return True

    def authenticate(self, url: str) -> Tuple[bool, str]:
        self.cookie = None
        try:
            if self.login_v2(url):
                return True, ""
            if self.login_v1(url):
                return True, ""
        except requests.RequestException as err:
            return False, f"{type(err).__name__}: {err}"
        return False, "Router SOAP login failed (check NETGEAR_USER / NETGEAR_PASSWORD)"

    def attached_devices(self) -> Tuple[Optional[List[Dict[str, str]]], str, str]:
        if not self.password:
            return None, "", "NETGEAR_PASSWORD is empty in nssm-fdcef-environment.env"
        last_error = "Could not reach the R6700v2 SOAP API"
        used_url = ""
        for url in self.urls():
            used_url = url
            try:
                ok, err = self.authenticate(url)
                if not ok:
                    last_error = err or "Router login failed"
                    continue
                found: List[Dict[str, str]] = []
                for method in ("GetAttachDevice2", "GetAttachDevice"):
                    body = CALL_BODY.format(
                        method=method,
                        service=SOAP_NS_DEVICE_INFO,
                        params="",
                    )
                    resp = self._post(url, "DeviceInfo:1", method, body, use_cookie=True)
                    code = _soap_response_code(resp.text)
                    if code in {"401", "501"}:
                        last_error = f"{method} ResponseCode {code}"
                        continue
                    devices = parse_netgear_device_xml(resp.text)
                    if devices:
                        found.extend(devices)
                    else:
                        last_error = f"{method} returned no devices"
                merged = merge_device_lists(found)
                if merged:
                    return merged, url, ""
            except requests.RequestException as err:
                last_error = f"{type(err).__name__}: {err}"
            except Exception as err:
                last_error = f"{type(err).__name__}: {err}"
        return None, used_url, last_error


def parse_arp_table(text: str, subnet_prefix: str = "192.168.254.") -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    seen = set()
    for line in (text or "").splitlines():
        match = _ARP_LINE_RE.match(line)
        if not match:
            continue
        ip, mac_raw, kind = match.group(1), match.group(2), match.group(3).lower()
        if kind == "invalid":
            continue
        if subnet_prefix and not ip.startswith(subnet_prefix):
            continue
        mac = normalize_mac(mac_raw)
        if not mac or mac in seen or mac in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}:
            continue
        seen.add(mac)
        devices.append(
            {
                "mac": mac,
                "hostname": "",
                "ip": ip,
                "connection": "",
                "link_rate": "",
                "signal": "",
            }
        )
    return devices


def read_arp_table(subnet_prefix: str = "192.168.254.") -> List[Dict[str, str]]:
    try:
        completed = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return []
    return parse_arp_table(completed.stdout or "", subnet_prefix=subnet_prefix)


def ping_host(ip: str, timeout_ms: int = 250) -> bool:
    if not is_ipv4(ip):
        return False
    try:
        completed = subprocess.run(
            ["ping", "-n", "1", "-w", str(int(timeout_ms)), ip],
            capture_output=True,
            timeout=max(1, int(timeout_ms / 1000) + 2),
            check=False,
        )
        return completed.returncode == 0
    except Exception:
        return False


def ping_hosts(ips: Iterable[str], workers: int = 32, timeout_ms: int = 250) -> List[str]:
    unique = []
    seen = set()
    for ip in ips:
        text = str(ip or "").strip()
        if not is_ipv4(text) or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    alive: List[str] = []
    if not unique:
        return alive
    with ThreadPoolExecutor(max_workers=max(4, min(workers, 64))) as pool:
        futures = {pool.submit(ping_host, ip, timeout_ms): ip for ip in unique}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    alive.append(ip)
            except Exception:
                continue
    return alive


def subnet_ips(network: str = "192.168.254.0/24", skip: Optional[Iterable[str]] = None) -> List[str]:
    skip_set = {str(x).strip() for x in (skip or []) if str(x).strip()}
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return []
    out = []
    for host in net.hosts():
        ip = str(host)
        if ip in skip_set:
            continue
        out.append(ip)
    return out


def merge_device_lists(*groups: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    by_mac: Dict[str, Dict[str, str]] = {}
    for group in groups:
        for raw in group or []:
            mac = normalize_mac(raw.get("mac"))
            if not mac:
                continue
            current = by_mac.setdefault(
                mac,
                {
                    "mac": mac,
                    "hostname": "",
                    "ip": "",
                    "connection": "",
                    "link_rate": "",
                    "signal": "",
                    "source": "",
                },
            )
            for key in ("ip", "connection", "link_rate", "signal"):
                val = str(raw.get(key) or "").strip()
                if val and not current.get(key):
                    current[key] = val
            host = str(raw.get("hostname") or "").strip()
            if host:
                current["hostname"] = prefer_hostname(current.get("hostname") or "", host)
            src = str(raw.get("source") or "").strip()
            if src:
                current["source"] = src if not current["source"] else current["source"]
    return list(by_mac.values())


_GENERIC_HOSTNAMES = frozenset(
    {
        "iphone",
        "ipad",
        "ipod",
        "android",
        "galaxy",
        "samsung",
        "unknown",
        "localhost",
        "android.local",
        "guest",
        "pc",
        "laptop",
        "desktop",
        "wireless",
        "wired",
    }
)


def clean_lan_hostname(name: Any) -> str:
    text = str(name or "").strip().rstrip(".")
    low = text.lower()
    for suffix in (".local", ".lan", ".home", ".localdomain", ".home.arpa"):
        if low.endswith(suffix):
            text = text[: -len(suffix)].rstrip(".")
            low = text.lower()
            break
    text = text.strip()
    if not text or is_ipv4(text):
        return ""
    if "in-addr.arpa" in low:
        return ""
    return text[:255]


def hostname_quality(name: Any) -> int:
    text = clean_lan_hostname(name)
    if not text:
        return 0
    low = text.lower()
    if low in _GENERIC_HOSTNAMES:
        return 1
    if low.startswith("android-") and len(low) <= 20:
        return 1
    score = 2
    if any(ch in text for ch in "-_ "):
        score += 2
    if any(ch.islower() for ch in text) and any(ch.isupper() for ch in text):
        score += 1
    if len(text) >= 8:
        score += 1
    return score


def prefer_hostname(*names: Any) -> str:
    best = ""
    best_score = -1
    for raw in names:
        cleaned = clean_lan_hostname(raw)
        if not cleaned:
            continue
        score = hostname_quality(cleaned)
        if score > best_score or (score == best_score and len(cleaned) > len(best)):
            best = cleaned
            best_score = score
    return best


def reverse_dns_hostname(ip: str, timeout: float = 0.7) -> str:
    if not is_ipv4(ip):
        return ""
    previous = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(max(0.2, float(timeout)))
        host, _aliases, _addrs = socket.gethostbyaddr(ip)
        return clean_lan_hostname(host)
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(previous)


def browse_mdns_hostnames(
    subnet_prefix: str = "192.168.254.",
    timeout: float = 2.5,
) -> Dict[str, str]:
    """Optional Bonjour browse (Advanced IP Scanner–style names when devices advertise)."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except Exception:
        return {}

    class _Listener(ServiceListener):
        def __init__(self) -> None:
            self.names: Dict[str, str] = {}

        def add_service(self, zc: Any, type_: str, name: str) -> None:
            self._capture(zc, type_, name)

        def update_service(self, zc: Any, type_: str, name: str) -> None:
            self._capture(zc, type_, name)

        def remove_service(self, zc: Any, type_: str, name: str) -> None:
            return

        def _capture(self, zc: Any, type_: str, name: str) -> None:
            try:
                info = zc.get_service_info(type_, name, timeout=800)
            except Exception:
                return
            if not info:
                return
            host = clean_lan_hostname(getattr(info, "server", "") or name)
            if not host:
                return
            try:
                addrs = list(info.parsed_addresses())
            except Exception:
                addrs = []
            for addr in addrs:
                if str(addr).startswith(subnet_prefix):
                    self.names[str(addr)] = prefer_hostname(self.names.get(str(addr), ""), host)

    listener = _Listener()
    zc = None
    try:
        zc = Zeroconf()
        service_types = (
            "_apple-mobdev2._tcp.local.",
            "_companion-link._tcp.local.",
            "_device-info._tcp.local.",
            "_airplay._tcp.local.",
            "_raop._tcp.local.",
            "_hap._tcp.local.",
            "_workstation._tcp.local.",
            "_smb._tcp.local.",
            "_http._tcp.local.",
            "_printer._tcp.local.",
            "_ipp._tcp.local.",
        )
        browsers = [ServiceBrowser(zc, type_, listener) for type_ in service_types]
        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            time.sleep(0.15)
        del browsers
    except Exception:
        return dict(listener.names)
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
    return dict(listener.names)


def enrich_devices_with_lan_names(
    devices: List[Dict[str, str]],
    *,
    subnet_prefix: str = "192.168.254.",
    mdns_timeout: float = 2.5,
) -> List[Dict[str, str]]:
    """Prefer reverse-DNS / Bonjour names over generic router DHCP labels (e.g. IPHONE)."""
    if not devices:
        return devices
    ips = []
    seen = set()
    for item in devices:
        ip = str(item.get("ip") or "").strip()
        if is_ipv4(ip) and ip not in seen:
            seen.add(ip)
            ips.append(ip)

    lan_names: Dict[str, str] = {}
    if ips:
        with ThreadPoolExecutor(max_workers=max(4, min(32, len(ips)))) as pool:
            futures = {pool.submit(reverse_dns_hostname, ip): ip for ip in ips}
            for fut in as_completed(futures):
                ip = futures[fut]
                try:
                    name = fut.result()
                except Exception:
                    name = ""
                if name:
                    lan_names[ip] = name

    for ip, name in browse_mdns_hostnames(subnet_prefix=subnet_prefix, timeout=mdns_timeout).items():
        lan_names[ip] = prefer_hostname(lan_names.get(ip, ""), name)

    for item in devices:
        ip = str(item.get("ip") or "").strip()
        item["hostname"] = prefer_hostname(item.get("hostname") or "", lan_names.get(ip, ""))
    return devices


def enrich_attached_from_arp(
    soap_devices: List[Dict[str, str]],
    arp_devices: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Fill missing SOAP fields from ARP for the same MAC. Never add ARP-only MACs.

    ARP-only entries used to keep phones "online" for several minutes after they
    left Wi-Fi; presence must follow the router's attached-device list only.
    """
    arp_by_mac: Dict[str, Dict[str, str]] = {}
    for raw in arp_devices or []:
        mac = normalize_mac(raw.get("mac"))
        if mac:
            arp_by_mac[mac] = raw
    out: List[Dict[str, str]] = []
    for raw in soap_devices or []:
        item = dict(raw)
        item["source"] = "netgear"
        mac = normalize_mac(item.get("mac"))
        arp = arp_by_mac.get(mac) if mac else None
        if arp:
            for key in ("ip", "connection", "link_rate", "signal"):
                if not str(item.get(key) or "").strip():
                    val = str(arp.get(key) or "").strip()
                    if val:
                        item[key] = val
            item["hostname"] = prefer_hostname(item.get("hostname") or "", arp.get("hostname") or "")
        out.append(item)
    return out


def fetch_office_devices(
    *,
    host: str,
    username: str,
    password: str,
    subnet_prefix: str = "192.168.254.",
    extra_ips: Optional[Iterable[str]] = None,
    enrich_lan_names: bool = True,
    mdns_timeout: float = 2.5,
) -> Dict[str, Any]:
    """SOAP attached list is authoritative for presence. ARP only enriches or degrades."""
    soap_devices = None
    soap_url = ""
    soap_error = ""
    try:
        client = NetgearSoapClient(host, username, password)
        soap_devices, soap_url, soap_error = client.attached_devices()
    except Exception as err:
        soap_error = f"{type(err).__name__}: {err}"

    arp_devices = read_arp_table(subnet_prefix=subnet_prefix)
    if soap_devices is None and extra_ips:
        ping_hosts(extra_ips)
        arp_devices = read_arp_table(subnet_prefix=subnet_prefix)

    if soap_devices is not None:
        devices = enrich_attached_from_arp(soap_devices, arp_devices)
        if enrich_lan_names:
            enrich_devices_with_lan_names(
                devices, subnet_prefix=subnet_prefix, mdns_timeout=mdns_timeout
            )
        return {
            "ok": True,
            "complete": True,
            "source": "netgear",
            "url": soap_url,
            "error": "",
            "devices": devices,
        }

    for item in arp_devices:
        item["source"] = "arp"
    if enrich_lan_names:
        enrich_devices_with_lan_names(
            arp_devices, subnet_prefix=subnet_prefix, mdns_timeout=mdns_timeout
        )
    return {
        "ok": bool(arp_devices),
        "complete": False,
        "source": "arp" if arp_devices else "none",
        "url": soap_url,
        "error": soap_error or "Router SOAP unavailable",
        "devices": arp_devices,
    }


def lan_ip_of_this_host(subnet_prefix: str = "192.168.254.") -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        sock.connect(("192.168.254.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if str(ip).startswith(subnet_prefix):
            return ip
    except Exception:
        pass
    return ""
