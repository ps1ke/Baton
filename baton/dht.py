"""Find a project's machines on the BitTorrent DHT, the way a magnet link finds a torrent's peers.

A project's DHT key is the first 20 bytes of SHA-256("baton-v3:" + project id). A reachable node announces its peer
port under that key (BEP 5 `announce_peer`), and anyone who knows the project id can look the key up (`get_peers`)
to learn addresses to try. That is all the DHT gives: **where** a machine might be. **Who** it is is still proven
by the protocol: for a member, by the certificate pinned in its signed offer; for a newcomer, by the server's key
being the author's or a steward's, read from the project's own signed events.

Privacy: announcing tells anyone who knows the project id that this IP takes part in it, so it is off unless the
project's author sets `discoverable` in the project. Looking a project up reveals only that someone looked.

This is a client only (it does not answer other nodes' queries); lookups and announces are bounded in time and in
the number of nodes asked.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import socket
import struct
import time

BOOTSTRAP = [("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881), ("router.utorrent.com", 6881)]
K = 8


# -- bencode ---------------------------------------------------------------------------------------------------------
def bencode(x) -> bytes:
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in x.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"cannot bencode {type(x).__name__}")


def bdecode(data: bytes):
    def item(i, depth=0):
        if depth > 32:
            raise ValueError("too deep")
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = item(i, depth + 1)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = item(i, depth + 1)
                v, i = item(i, depth + 1)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            j = data.index(b":", i)
            n = int(data[i:j])
            if n > len(data):
                raise ValueError("bad length")
            return data[j + 1:j + 1 + n], j + 1 + n
        raise ValueError("bad bencode")

    v, end = item(0)
    return v


# -- helpers ---------------------------------------------------------------------------------------------------------
def key_of(project_id: str) -> bytes:
    return hashlib.sha256(("baton-v3:" + project_id).encode()).digest()[:20]


def distance(a: bytes, b: bytes) -> int:
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


def compact_nodes(blob: bytes) -> list[tuple[bytes, tuple[str, int]]]:
    out = []
    for i in range(0, len(blob) - len(blob) % 26, 26):
        nid, ip, port = blob[i:i + 20], socket.inet_ntoa(blob[i + 20:i + 24]), struct.unpack(">H", blob[i + 24:i + 26])[0]
        if port:
            out.append((nid, (ip, port)))
    return out


def compact_peers(values) -> list[tuple[str, int]]:
    out = []
    for v in values if isinstance(values, list) else []:
        if isinstance(v, bytes) and len(v) == 6:
            ip, port = socket.inet_ntoa(v[:4]), struct.unpack(">H", v[4:])[0]
            if port and not ipaddress.ip_address(ip).is_unspecified:
                out.append((ip, port))
    return out


class Client:
    """A DHT client: get_peers lookups and announce_peer, over one UDP socket."""

    def __init__(self, bootstrap=None, timeout: float = 1.0):
        self.id = os.urandom(20)
        self.bootstrap = list(bootstrap or BOOTSTRAP)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))

    def close(self):
        self.sock.close()

    def _query(self, addrs, q: str, args: dict) -> dict:
        """Send one query to each address; collect the answers that come back within the timeout."""
        pending = {}
        for addr in addrs:
            t = secrets.token_bytes(2)
            pending[t] = addr
            try:
                self.sock.sendto(bencode({"t": t, "y": "q", "q": q, "a": {"id": self.id, **args}}), addr)
            except OSError:
                pending.pop(t, None)
        answers = {}
        deadline = time.monotonic() + self.timeout
        while pending and time.monotonic() < deadline:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                data, src = self.sock.recvfrom(4096)
                msg = bdecode(data)
            except (socket.timeout, OSError, ValueError):
                continue
            if not isinstance(msg, dict) or msg.get(b"y") != b"r":
                continue
            t = msg.get(b"t")
            if t in pending and isinstance(msg.get(b"r"), dict):
                answers[pending.pop(t)] = msg[b"r"]
        return answers

    def _resolve(self, addrs):
        out = []
        for host, port in addrs:
            try:
                out.append((socket.gethostbyname(host), port))
            except OSError:
                continue
        return out

    def lookup(self, key: bytes, rounds: int = 8) -> tuple[list[tuple[str, int]], dict]:
        """Peers announced under `key`, and the closest nodes that gave a token (for announcing)."""
        seen, peers, tokens = set(), [], {}
        frontier = [(None, a) for a in self._resolve(self.bootstrap)]
        for _ in range(rounds):
            ask = [a for _, a in frontier if a not in seen][:K]
            if not ask:
                break
            seen.update(ask)
            answers = self._query(ask, "get_peers", {"info_hash": key})
            found = []
            for addr, r in answers.items():
                if isinstance(r.get(b"token"), bytes) and isinstance(r.get(b"id"), bytes):
                    tokens[addr] = (r[b"id"], r[b"token"])
                for p in compact_peers(r.get(b"values")):
                    if p not in peers:
                        peers.append(p)
                if isinstance(r.get(b"nodes"), bytes):
                    found += compact_nodes(r[b"nodes"])
            frontier = sorted(((nid, a) for nid, a in found if a not in seen), key=lambda x: distance(x[0], key))
        closest = dict(sorted(tokens.items(), key=lambda kv: distance(kv[1][0], key))[:K])
        return peers, closest

    def announce(self, key: bytes, port: int, tokens: dict) -> int:
        """Announce our TCP peer port to the closest nodes; how many accepted."""
        n = 0
        for addr, (_, token) in tokens.items():
            if self._query([addr], "announce_peer", {"info_hash": key, "port": port, "token": token, "implied_port": 0}):
                n += 1
        return n


def find(project_id: str, bootstrap=None, announce_port: int | None = None) -> list[tuple[str, int]]:
    """Addresses announced for the project; also announce ours when `announce_port` is given."""
    c = Client(bootstrap)
    try:
        key = key_of(project_id)
        peers, tokens = c.lookup(key)
        if announce_port:
            c.announce(key, announce_port, tokens)
        return peers
    finally:
        c.close()
