"""Open this machine's peer port on the home router, so other machines can reach it.

Two standard protocols, tried in order:

- NAT-PMP (RFC 6886): one UDP packet to the default gateway on port 5351. Apple routers, many others, and PCP-capable
  routers speak it.
- UPnP Internet Gateway Device: find the router with SSDP on the local network, read its description, then ask its
  WANIPConnection (or WANPPPConnection) service for a port mapping and for the external address.

Safety: baton maps only its own port, TCP only, for a limited lease (renewed while it runs, removed when it stops),
and talks only to devices on the local network (a router that names a public address in its description is ignored).
The router description is size-limited XML from the local network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.etree import ElementTree

LEASE = 3600   # seconds; renewed at half-life while the node runs


@dataclass
class Mapping:
    method: str            # natpmp | upnp
    external_ip: str
    external_port: int
    internal_port: int
    lease: int
    gateway: str = ""
    control: str = ""      # upnp: the control URL
    service: str = ""      # upnp: the service type
    location: str = ""     # upnp: the router description it came from

    @property
    def address(self) -> str:
        return f"{self.external_ip}:{self.external_port}"


def private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_link_local


def gateway() -> str | None:
    """The default gateway's IPv4 address."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except OSError:
        pass
    if sys.platform == "darwin":
        r = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True)
        m = re.search(r"gateway:\s*([\d.]+)", r.stdout)
        if m:
            return m.group(1)
    return None


def local_ip(towards: str = "192.0.2.1") -> str:
    """The address this machine uses on the way to `towards` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((towards, 9))
        return s.getsockname()[0]


# -- NAT-PMP -------------------------------------------------------------------------------------------------------
def _natpmp(gw: str, request: bytes, size: int, port: int = 5351, timeout: float = 0.25) -> bytes | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        wait = timeout
        for _ in range(4):   # RFC 6886: retransmit with doubling waits
            s.settimeout(wait)
            s.sendto(request, (gw, port))
            try:
                data, src = s.recvfrom(64)
            except socket.timeout:
                wait *= 2
                continue
            if src[0] == gw and len(data) >= size:
                return data
        return None


def natpmp_map(port: int, gw: str | None = None, lease: int = LEASE, pmp_port: int = 5351) -> Mapping | None:
    gw = gw or gateway()
    if not gw or not private(gw):
        return None
    r = _natpmp(gw, struct.pack(">BBHHHI", 0, 2, 0, port, port, lease), 16, pmp_port)
    if not r:
        return None
    _, op, code, _, internal, external, granted = struct.unpack(">BBHIHHI", r[:16])
    if op != 130 or code != 0 or internal != port:
        return None
    a = _natpmp(gw, b"\x00\x00", 12, pmp_port)
    if not a:
        return None
    _, op, code, _ = struct.unpack(">BBHI", a[:8])
    if op != 128 or code != 0:
        return None
    return Mapping("natpmp", socket.inet_ntoa(a[8:12]), external, port, granted, gateway=gw)


def natpmp_unmap(m: Mapping, pmp_port: int = 5351):
    _natpmp(m.gateway, struct.pack(">BBHHHI", 0, 2, 0, m.internal_port, 0, 0), 16, pmp_port)


# -- UPnP IGD ------------------------------------------------------------------------------------------------------
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")


def ssdp_discover(timeout: float = 2.0) -> list[str]:
    """LOCATION URLs of internet gateway devices that answer on the local network."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            s.sendto(msg, ("239.255.255.250", 1900))
            while True:
                data, src = s.recvfrom(4096)
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    url = m.group(1).decode("ascii", "replace")
                    if url not in found:
                        found.append(url)
        except (socket.timeout, OSError):
            pass
    return found


def _local_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return urllib.parse.urlparse(url).scheme == "http" and private(host)


def _get(url: str, timeout: float = 3.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(65536)


def find_service(location: str) -> tuple[str, str] | None:
    """(control URL, service type) of the router's WAN connection service."""
    if not _local_url(location):
        return None
    root = ElementTree.fromstring(_get(location))
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    base = root.findtext("d:URLBase", default="", namespaces=ns).strip() or location
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        st = (svc.findtext("d:serviceType", default="", namespaces=ns) or "").strip()
        if st in SERVICES:
            control = urllib.parse.urljoin(base, (svc.findtext("d:controlURL", default="", namespaces=ns) or "").strip())
            if _local_url(control):
                return control, st
    return None


def _soap(control: str, service: str, action: str, args: dict, timeout: float = 4.0) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(control, data=env, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(65536).decode("utf-8", "replace")


def upnp_map(port: int, location: str | None = None, lease: int = LEASE) -> Mapping | None:
    for loc in [location] if location else ssdp_discover():
        try:
            svc = find_service(loc)
            if not svc:
                continue
            control, service = svc
            me = local_ip(urllib.parse.urlparse(control).hostname or "192.0.2.1")
            _soap(control, service, "AddPortMapping", {
                "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP", "NewInternalPort": port,
                "NewInternalClient": me, "NewEnabled": 1, "NewPortMappingDescription": "baton peer", "NewLeaseDuration": lease})
            ext = re.search(r"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>",
                            _soap(control, service, "GetExternalIPAddress", {}))
            ip = ext.group(1).strip() if ext else ""
            if not ip:
                continue
            return Mapping("upnp", ip, port, port, lease, control=control, service=service, location=loc)
        except (OSError, ElementTree.ParseError, ValueError):
            continue
    return None


def upnp_unmap(m: Mapping):
    try:
        _soap(m.control, m.service, "DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": m.external_port,
                                                           "NewProtocol": "TCP"})
    except OSError:
        pass


# -- one call for the node -------------------------------------------------------------------------------------------
def open_port(port: int, lease: int = LEASE) -> Mapping | None:
    """Try NAT-PMP, then UPnP. None when the router speaks neither (or there is no router)."""
    return natpmp_map(port, lease=lease) or upnp_map(port, lease=lease)


def close_port(m: Mapping):
    (natpmp_unmap if m.method == "natpmp" else upnp_unmap)(m)


def renew(m: Mapping) -> Mapping | None:
    if m.method == "natpmp":
        return natpmp_map(m.internal_port, gw=m.gateway, lease=m.lease)
    return upnp_map(m.internal_port, location=m.location or None, lease=m.lease)
