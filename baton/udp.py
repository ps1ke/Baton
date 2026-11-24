"""Peer to peer through home routers, the way torrent clients do it: UDP hole punching with the DHT as introducer.

Each node of a project keeps one UDP socket. From that socket it announces itself on the BitTorrent DHT under the
project's UDP key with `implied_port`, so the DHT records the public address and port the node's router really
gave that socket. Every node looks the key up now and then, and sends small *punch* packets to each address found.
When two nodes do this, the first punches open each router's mapping and the next ones pass: most home routers
allow it (they keep one public port per socket); two routers that give each destination a different port
("symmetric" NAT) cannot be punched, by baton or by anything else.

Over a punched path the two nodes run a small reliable, ordered stream (sequence numbers, cumulative
acknowledgements, retransmission, a send window), TLS on top of it, and then exactly the same authentication,
access rules and exchange as over TCP (p2p.Node). Punch targets, sessions and retries are capped.

Packets: MAGIC + a type byte, then
- PUNCH: nonce (8) + project key (20). The node with the larger nonce acts as the TLS server.
- DATA: connection id (8) + sequence (4) + payload;  ACK: connection id (8) + next expected sequence (4);
- FIN: connection id (8).
DHT messages share the socket; they are bencoded dictionaries, so they start with "d".
"""

from __future__ import annotations

import hashlib
import os
import socket
import ssl
import struct
import threading
import time

from . import dht

MAGIC = b"BTN3"
PUNCH, DATA, ACK, FIN = 1, 2, 3, 4
CHUNK = 1100
WINDOW = 64
MAX_SESSIONS = 16
MAX_TARGETS = 16


def key_of(project_id: str) -> bytes:
    return hashlib.sha256(("baton-v3-udp:" + project_id).encode()).digest()[:20]


class Stream:
    """A reliable, ordered byte stream to one remote address, carried by the hub's socket."""

    def __init__(self, hub, remote, cid: bytes):
        self.hub, self.remote, self.cid = hub, remote, cid
        self.next_seq = 0
        self.unacked: dict[int, list] = {}      # seq -> [payload, last sent, tries]
        self.expected = 0
        self.early: dict[int, bytes] = {}
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.closed = False
        self.timeout: float | None = 30.0

    # called by the hub's reader ---------------------------------------------------------------------------------
    def on_data(self, seq: int, payload: bytes):
        with self.cond:
            if seq >= self.expected and seq < self.expected + 4 * WINDOW:
                self.early[seq] = payload
                while self.expected in self.early:
                    self.buf += self.early.pop(self.expected)
                    self.expected += 1
                self.cond.notify_all()
            ack = self.expected
        self.hub.raw(self.remote, struct.pack(">B", ACK) + self.cid + struct.pack(">I", ack))

    def on_ack(self, ack: int):
        with self.cond:
            for s in [s for s in self.unacked if s < ack]:
                del self.unacked[s]
            self.cond.notify_all()

    def on_fin(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def tick(self, now: float):
        with self.cond:
            for seq, item in list(self.unacked.items())[:WINDOW]:
                rto = min(3.0, 0.4 * (2 ** min(item[2], 3)))
                if now - item[1] > rto:
                    if item[2] > 12:
                        self.closed = True   # the other side is gone
                        self.cond.notify_all()
                        return
                    item[1], item[2] = now, item[2] + 1
                    self.hub.raw(self.remote, struct.pack(">B", DATA) + self.cid + struct.pack(">I", seq) + item[0])

    # socket-like interface for TLS ------------------------------------------------------------------------------
    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data: bytes):
        view = memoryview(data)
        for i in range(0, len(view), CHUNK):
            part = bytes(view[i:i + CHUNK])
            with self.cond:
                end = time.monotonic() + (self.timeout or 3600)
                while len(self.unacked) >= WINDOW and not self.closed:
                    if not self.cond.wait(max(0.01, end - time.monotonic())) and time.monotonic() >= end:
                        raise socket.timeout("send timed out")
                if self.closed:
                    raise ConnectionError("the connection closed")
                seq = self.next_seq
                self.next_seq += 1
                self.unacked[seq] = [part, time.monotonic(), 0]
            self.hub.raw(self.remote, struct.pack(">B", DATA) + self.cid + struct.pack(">I", seq) + part)

    def recv(self, n: int) -> bytes:
        with self.cond:
            end = time.monotonic() + (self.timeout or 3600)
            while not self.buf and not self.closed:
                if not self.cond.wait(max(0.01, end - time.monotonic())) and time.monotonic() >= end:
                    raise socket.timeout("receive timed out")
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

    def flush(self, wait: float = 5.0):
        end = time.monotonic() + wait
        with self.cond:
            while self.unacked and not self.closed and time.monotonic() < end:
                self.cond.wait(0.1)

    def close(self):
        self.flush()
        for _ in range(3):
            self.hub.raw(self.remote, struct.pack(">B", FIN) + self.cid)
        self.on_fin()


class TLS:
    """TLS over a Stream (memory BIOs), with the methods p2p's protocol uses."""

    def __init__(self, stream: Stream, ctx: ssl.SSLContext, server: bool):
        self.stream = stream
        self.inc, self.out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self.inc, self.out, server_side=server)
        while True:
            try:
                self.obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                self._flush()
                self._fill()
        self._flush()

    def _flush(self):
        data = self.out.read()
        if data:
            self.stream.sendall(data)

    def _fill(self):
        chunk = self.stream.recv(65536)
        if not chunk:
            raise ConnectionError("the connection closed")
        self.inc.write(chunk)

    def settimeout(self, t):
        self.stream.settimeout(t)

    def getpeercert(self, binary_form=False):
        return self.obj.getpeercert(binary_form)

    def sendall(self, data: bytes):
        view = memoryview(data)
        while view:
            n = self.obj.write(view[:16384])
            view = view[n:]
            self._flush()

    def recv(self, n: int) -> bytes:
        while True:
            try:
                return self.obj.read(n)
            except ssl.SSLWantReadError:
                self._flush()
                try:
                    self._fill()
                except ConnectionError:
                    return b""
            except ssl.SSLZeroReturnError:
                return b""

    def close(self):
        try:
            self.obj.unwrap()
            self._flush()
        except (ssl.SSLError, ConnectionError, OSError):
            pass
        self.stream.close()


class Hub:
    """One project's UDP socket: DHT announce and lookup from it, hole punching, and the streams over it."""

    def __init__(self, node, project_id: str, port: int = 0, bootstrap=None, log=None):
        self.node, self.pid = node, project_id
        self.key = key_of(project_id)
        self.bootstrap = list(bootstrap or dht.BOOTSTRAP)
        self.log = log or (lambda *_: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.port = self.sock.getsockname()[1]
        self.nonce = os.urandom(8)
        self.id = os.urandom(20)
        self.sessions: dict[tuple, Stream] = {}
        self.punched: dict[tuple, float] = {}
        self.pending: dict[bytes, list] = {}
        self.stop = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._timers, daemon=True).start()

    def raw(self, addr, payload: bytes):
        try:
            self.sock.sendto(MAGIC + payload, addr)
        except OSError:
            pass

    # -- reading ------------------------------------------------------------------------------------------------
    def _read(self):
        while not self.stop.is_set():
            try:
                data, src = self.sock.recvfrom(65535)
            except OSError:
                return
            if data[:1] == b"d":
                try:
                    msg = dht.bdecode(data)
                except ValueError:
                    continue
                slot = self.pending.get(msg.get(b"t")) if isinstance(msg, dict) else None
                if slot is not None:
                    slot.append(msg)
                continue
            if data[:4] != MAGIC or len(data) < 5:
                continue
            kind, body = data[4], data[5:]
            if kind == PUNCH and len(body) >= 28:
                self._on_punch(src, body[:8], body[8:28])
            elif kind in (DATA, ACK, FIN) and len(body) >= 8:
                s = self.sessions.get(src)
                if s is None or s.cid != body[:8]:
                    continue
                if kind == DATA and len(body) >= 12:
                    s.on_data(struct.unpack(">I", body[8:12])[0], body[12:])
                elif kind == ACK and len(body) >= 12:
                    s.on_ack(struct.unpack(">I", body[8:12])[0])
                elif kind == FIN:
                    s.on_fin()

    def _timers(self):
        while not self.stop.wait(0.1):
            now = time.monotonic()
            for s in list(self.sessions.values()):
                s.tick(now)

    # -- punching -----------------------------------------------------------------------------------------------
    def _punch_packet(self) -> bytes:
        return struct.pack(">B", PUNCH) + self.nonce + self.key

    def _on_punch(self, src, nonce: bytes, key: bytes):
        if key != self.key or nonce == self.nonce:   # another project, or our own packet come back
            return
        last = self.punched.get(src, 0)
        if time.monotonic() - last > 0.5:
            self.punched[src] = time.monotonic()
            self.raw(src, self._punch_packet())        # answer, so the other side's router opens too
        with self.lock:
            if src in self.sessions or len(self.sessions) >= MAX_SESSIONS:
                return
            cid = hashlib.sha256(min(nonce, self.nonce) + max(nonce, self.nonce)).digest()[:8]
            s = Stream(self, src, cid)
            self.sessions[src] = s
        threading.Thread(target=self._session, args=(s, self.nonce > nonce), daemon=True).start()

    def punch(self, addr, seconds: float = 8.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end and addr not in self.sessions and not self.stop.is_set():
            self.raw(addr, self._punch_packet())
            time.sleep(0.3)

    def _session(self, s: Stream, server: bool):
        """One exchange over a punched path, with the same protocol and rules as over TCP."""
        node = self.node
        try:
            s.settimeout(20)
            if server:
                tls = TLS(s, node._server_ctx(), True)
                node._serve(tls, f"udp:{s.remote[0]}:{s.remote[1]}")
            else:
                tls = TLS(s, node._client_ctx(), False)
                fp = hashlib.sha256(tls.getpeercert(True)).hexdigest()
                w = node._auth_client(tls, fp, project=self.pid)
                r = node._client_sync(tls, w, self.pid if not node.store.state().project_id else None)
                if r.get("received") or r.get("sent") or r.get("blobs"):
                    self.log(f"p2p: {r['peer']} (UDP, hole punched): got {r['received']} event(s), {r['blobs']} "
                             f"blob(s); gave {r['sent']}" + (" (an applicant)" if r.get("limited") else ""))
            tls.close()
        except Exception as err:   # a failed session only ends itself
            if not isinstance(err, (socket.timeout, ConnectionError)):
                self.log(f"p2p: UDP session with {s.remote[0]}: {type(err).__name__}: {err}")
            s.on_fin()
        finally:
            with self.lock:
                if self.sessions.get(s.remote) is s:
                    del self.sessions[s.remote]

    # -- the DHT, from this socket --------------------------------------------------------------------------------
    def _query(self, addrs, q: str, args: dict, timeout: float = 1.5) -> dict:
        slots = {}
        for addr in addrs:
            t = os.urandom(2)
            slots[t] = (addr, [])
            self.pending[t] = slots[t][1]
            try:
                self.sock.sendto(dht.bencode({"t": t, "y": "q", "q": q, "a": {"id": self.id, **args}}), addr)
            except OSError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end and any(not box for _, box in slots.values()):
            time.sleep(0.02)
        out = {}
        for t, (addr, box) in slots.items():
            self.pending.pop(t, None)
            if box and box[0].get(b"y") == b"r" and isinstance(box[0].get(b"r"), dict):
                out[addr] = box[0][b"r"]
        return out

    def _resolve(self):
        out = []
        for host, port in self.bootstrap:
            try:
                out.append((socket.gethostbyname(host), port))
            except OSError:
                pass
        return out

    def lookup(self, rounds: int = 6) -> tuple[list, dict]:
        seen, peers, tokens = set(), [], {}
        frontier = [(None, a) for a in self._resolve()]
        for _ in range(rounds):
            ask = [a for _, a in frontier if a not in seen][:8]
            if not ask:
                break
            seen.update(ask)
            found = []
            for addr, r in self._query(ask, "get_peers", {"info_hash": self.key}).items():
                if isinstance(r.get(b"token"), bytes) and isinstance(r.get(b"id"), bytes):
                    tokens[addr] = (r[b"id"], r[b"token"])
                for p in dht.compact_peers(r.get(b"values")):
                    if p not in peers:
                        peers.append(p)
                if isinstance(r.get(b"nodes"), bytes):
                    found += dht.compact_nodes(r[b"nodes"])
            frontier = sorted(((nid, a) for nid, a in found if a not in seen), key=lambda x: dht.distance(x[0], self.key))
        closest = dict(sorted(tokens.items(), key=lambda kv: dht.distance(kv[1][0], self.key))[:8])
        return peers, closest

    def round(self) -> int:
        """Announce this socket under the project and punch towards everyone announced there. How many targets."""
        peers, tokens = self.lookup()
        for addr, (_, token) in tokens.items():   # implied_port: the DHT records the port our router gave us
            self._query([addr], "announce_peer", {"info_hash": self.key, "port": self.port, "token": token,
                                                  "implied_port": 1}, timeout=1.0)
        targets = [p for p in peers if p not in self.sessions][:MAX_TARGETS]
        for addr in targets:
            threading.Thread(target=self.punch, args=(addr,), daemon=True).start()
        return len(targets)

    def run(self, every: float, stop: threading.Event):
        while not stop.is_set() and not self.stop.is_set():
            try:
                self.round()
            except OSError as err:
                self.log(f"p2p: UDP round: {err}")
            stop.wait(every)

    def close(self):
        self.stop.set()
        self.sock.close()
