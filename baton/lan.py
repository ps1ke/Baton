"""Find a project's machines on the local networks this machine is on: the LAN, and VPNs such as ZeroTier or
Tailscale (like BitTorrent's Local Service Discovery).

A node of a discoverable project answers queries, and announces itself now and then, by UDP multicast on group
239.255.73.42 port 7419, on each network interface. A message names only a hash of the project id (as on the DHT),
the node's peer port and its certificate fingerprint; whoever finds it must still prove it belongs to the project
before anything it sends is kept (p2p.vouched).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
import threading
import time

GROUP, PORT = "239.255.73.42", 7419
SKIP = ("docker", "br-", "veth", "virbr", "lo")


def key_of(project_id: str) -> str:
    return hashlib.sha256(("baton-lan-v3:" + project_id).encode()).hexdigest()[:32]


def interfaces() -> list[tuple[str, str]]:
    """(name, IPv4 address) of the interfaces others could reach this machine on: LAN and VPN, not loopback or
    container bridges."""
    out = []
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for m in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/", text, re.M):
            out.append((m.group(1), m.group(2)))
    except (OSError, subprocess.SubprocessError):
        pass
    if not out and sys.platform == "darwin":
        try:
            text = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            name = None
            for line in text.splitlines():
                if line and not line[0].isspace():
                    name = line.split(":")[0]
                m = re.search(r"\binet ([\d.]+)", line)
                if m and name:
                    out.append((name, m.group(1)))
        except (OSError, subprocess.SubprocessError):
            pass
    good = []
    for name, ip in out:
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_link_local or name.startswith(SKIP):
            continue
        good.append((name, ip))
    return good


def candidates(port: int) -> list[str]:
    """host:port on every interface, for an offer's `addrs`."""
    return [f"{ip}:{port}" for _, ip in interfaces()]


def _sender(ip: str):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    return s


class Beacon:
    """Answer queries for one project, and announce it every `every` seconds, on every interface."""

    def __init__(self, project_id: str, port: int, cert: str, every: float = 30.0):
        self.key, self.port, self.cert, self.every = key_of(project_id), port, cert, every
        self.stop = threading.Event()

    def message(self) -> bytes:
        return json.dumps({"t": "baton-here", "v": 3, "key": self.key, "port": self.port, "cert": self.cert}).encode()

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._announce, daemon=True).start()
        return self

    def _announce(self):
        while not self.stop.is_set():
            for _, ip in interfaces():
                try:
                    with _sender(ip) as s:
                        s.sendto(self.message(), (GROUP, PORT))
                except OSError:
                    pass
            self.stop.wait(self.every)

    def _listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            s.bind(("", PORT))
        except OSError:
            return
        for _, ip in interfaces():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(ip))
            except OSError:
                pass
        s.settimeout(1.0)
        while not self.stop.is_set():
            try:
                data, src = s.recvfrom(2048)
                q = json.loads(data)
            except (socket.timeout, OSError, ValueError):
                continue
            if isinstance(q, dict) and q.get("t") == "baton-where" and q.get("key") == self.key:
                try:
                    s.sendto(self.message(), src)   # answer the asker directly
                except OSError:
                    pass
        s.close()

    def close(self):
        self.stop.set()


def find(project_id: str, wait: float = 3.0) -> list[tuple[str, str]]:
    """(host:port, cert) of the project's nodes that answer on the local networks within `wait` seconds."""
    key = key_of(project_id)
    found: dict[str, str] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    q = json.dumps({"t": "baton-where", "v": 3, "key": key}).encode()
    for _, ip in interfaces():
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.sendto(q, (GROUP, PORT))
        except OSError:
            pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        s.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            data, src = s.recvfrom(2048)
            m = json.loads(data)
        except (socket.timeout, OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("t") == "baton-here" and m.get("key") == key and isinstance(m.get("port"), int):
            found[f"{src[0]}:{m['port']}"] = str(m.get("cert", ""))
    s.close()
    return list(found.items())
