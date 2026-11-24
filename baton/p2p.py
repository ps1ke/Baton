"""Peer to peer: machines exchange a project's events and blobs directly, with no server.

Every machine that takes part runs a small node. Nodes find each other through the members' signed offers in the log
(an address and a certificate fingerprint, or the name of a relay), through invites, and, for projects whose author
made them discoverable, through the BitTorrent DHT (dht.py). Reaching each other:

- **direct**: TCP + TLS to the peer's address, the certificate pinned by its fingerprint. A node behind a home router
  first asks the router to forward its port (nat.py);
- **hole punching**: a relay tells two nodes where the other is, as it sees them, and they connect to each other at
  the same moment from those ports, which many home routers let through;
- **relayed**: otherwise through a member that can be reached (a relay) and that both are connected to. A relay only
  bridges members of the same project, who already hold the same log; every event stays signed by its author, so a
  relay can neither forge nor alter anything. Relayed sessions are limited in number, duration and size.

Each connection, however it was made:

1. Authentication. The client sends `hello {v, actor, key, nonce, project, token}`; the server answers `challenge
   {nonce}`; the client signs CONTEXT + that nonce + "|" + the server's certificate fingerprint (binding the proof to
   that TLS identity); the server answers `welcome {access, actor, key, sig}`, signing the client's nonce and key.
2. Access, from the server's own copy of the log: a key bound to an actor that is not revoked gets everything; an
   invite holder, or anyone when the project is open, gets the public events only (the project's card, its policy and
   the author's grants) and may deliver its own `apply` or `join`; anyone else is refused.
3. Exchange. Each side sends the ids of what it holds; each sends the events the other lacks, then the blobs it
   announced, one per frame. Every event is checked (id and signature), and every blob against its SHA-256 and the
   announced list, before it is written.

Before authentication a peer may send only small frames and must finish quickly, and each address may hold only a
few connections at once. Nothing in the exchange can make a node run anything.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import crypto
from . import events as ev
from .store import Store

VERSION = 3
MAX_FRAME = 64 * 1024 * 1024       # an authenticated frame (one blob of up to MAX_BLOB, base64-encoded)
PRE_AUTH = 64 * 1024               # frames before authentication
MAX_BLOB = 46 * 1024 * 1024
MAX_EVENTS = 20000
MAX_CONNECTIONS = 64
PER_IP = 8
HANDSHAKE = 15.0
SESSION = 60.0
BRIDGE_SECONDS = 600
BRIDGE_BYTES = 512 * 1024 * 1024
MAX_BRIDGES = 16
CONTEXT = b"baton-p2p-v3|"
PUBLIC_KINDS = ("project", "policy", "grant")   # what an invited, not yet admitted, peer may read
NETWORK_ERRORS = (OSError, ConnectionError, PermissionError, ValueError, KeyError, ssl.SSLError)


# -- TLS identity ------------------------------------------------------------------------------------------------
def tls_identity(folder: Path) -> tuple[Path, Path, str]:
    """This machine's self-signed certificate: (cert file, key file, SHA-256 fingerprint of the DER certificate)."""
    folder = Path(folder)
    cert, key = folder / "cert.pem", folder / "key.pem"
    if not cert.exists() or not key.exists():
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        _make_cert(cert, key)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return cert, key, hashlib.sha256(der).hexdigest()


def _make_cert(cert: Path, key: Path):
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "baton peer")])
        now = datetime.datetime.now(datetime.timezone.utc)
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=3650)).sign(k, hashes.SHA256()))
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
        cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "3650", "-subj", "/CN=baton peer"],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("cannot make a TLS certificate: install the `cryptography` package or openssl")
        os.chmod(key, 0o600)


# -- framing -----------------------------------------------------------------------------------------------------
def send(sock, obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)


def _exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("peer closed the connection")
        buf += chunk
    return bytes(buf)


def recv(sock, limit: int = MAX_FRAME):
    (n,) = struct.unpack(">I", _exact(sock, 4))
    if n > limit:
        raise ConnectionError("frame too large")
    obj = json.loads(_exact(sock, n))
    if not isinstance(obj, dict):
        raise ConnectionError("expected an object")
    return obj


def _expect(obj, t):
    if obj.get("t") != t:
        raise ConnectionError(obj.get("error") or f"expected {t}, got {obj.get('t')}")
    return obj


# -- invites -------------------------------------------------------------------------------------------------------
class Invites:
    """Invites this machine handed out: only a hash of each token is kept, with its project and expiry."""

    def __init__(self, folder: Path):
        self.file = Path(folder) / "invites.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def new(self, project: str, ttl: float) -> str:
        token = secrets.token_urlsafe(18)
        d = {k: v for k, v in self._load().items() if v["expires"] > time.time()}
        d[hashlib.sha256(token.encode()).hexdigest()] = {"project": project, "expires": time.time() + ttl}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(d))
        os.chmod(self.file, 0o600)
        return token

    def valid(self, project: str, token: str | None) -> bool:
        if not token:
            return False
        hit = self._load().get(hashlib.sha256(token.encode()).hexdigest())
        return bool(hit) and hit["project"] == project and hit["expires"] > time.time()


def invite_url(addr: str, project: str, cert_fp: str, token: str) -> str:
    return f"baton://{addr}/{project}?cert={cert_fp}&token={token}"


def parse_invite(url: str) -> dict:
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url)
    if u.scheme != "baton" or not u.netloc or not u.path.strip("/"):
        raise ValueError("an invite looks like baton://host:port/<project id>?cert=...&token=...")
    q = parse_qs(u.query)
    return {"addr": u.netloc, "project": u.path.strip("/"), "cert": q.get("cert", [""])[0], "token": q.get("token", [""])[0]}


# -- what each side may see and send ---------------------------------------------------------------------------------
def access_of(store: Store, actor: str, key: str, project: str | None, token: str | None, invites: Invites) -> str | None:
    s = store.state()
    a = s.actors.get(actor)
    if a is not None and not a.revoked and a.key and a.key == key:
        return "full"
    if s.project_id and project == s.project_id and s.join_mode != "closed" and \
            (invites.valid(project, token) or s.join_mode == "open" or s.project.get("discoverable")):
        return "applicant"   # open or discoverable projects need no invite to read the card and deliver a join/apply
    return None


def visible(store: Store, access: str) -> dict:
    found, _ = store.read()
    if access == "full":
        return found
    return {i: e for i, e in found.items() if e.kind in PUBLIC_KINDS}


def check_events(dicts: list) -> list:
    out = []
    for d in dicts[:MAX_EVENTS] if isinstance(dicts, list) else []:
        try:
            out.append(ev.from_dict(d))
        except (ev.BadEvent, TypeError, AttributeError):
            continue
    return out


def accept(store: Store, access: str, actor: str, dicts: list) -> int:
    """Write the peer's events that check out. An applicant may only deliver its own `apply` (and `join`)."""
    n = 0
    mine, _ = store.read()
    for e in check_events(dicts):
        if e.id in mine:
            continue
        if access != "full" and not (e.actor == actor and e.kind in ("apply", "join")):
            continue
        store.write(e)
        n += 1
    return n


def vouched(events: list, project: str, key: str) -> bool:
    """For a newcomer that reached a node without a pinned certificate: do these events hold the project's genesis,
    and is `key` the author's, or a key the author granted?"""
    genesis = next((e for e in events if e.id == project and e.kind == "project"), None)
    if genesis is None:
        return False
    if genesis.key == key:
        return True
    return any(e.kind == "grant" and e.key == genesis.key and e.body.get("key") == key for e in events)


# -- the node ------------------------------------------------------------------------------------------------------
class Node:
    """One machine's peer for one project log, acting as `actor`."""

    def __init__(self, store: Store, actor: str, tls_dir: Path, invites: Invites | None = None, log=None,
                 relay: bool = True):
        self.store, self.actor = store, actor
        self.cert, self.key, self.fp = tls_identity(tls_dir)
        self.invites = invites or Invites(tls_dir)
        self.log = log or (lambda *_: None)
        self.relay = relay
        self.server = None
        self.addr = None
        self.extra: dict[str, str] = {}      # addr -> cert fingerprint, added by hand or from invites
        self.status: dict[str, dict] = {}    # peer -> last outcome
        self.lock = threading.Lock()
        self.active = 0
        self.per_ip: dict[str, int] = {}
        self.registered: dict[str, tuple] = {}     # (relay) actor -> (control socket, observed "ip:port", lock)
        self.sessions: dict[str, dict] = {}        # (relay) session id -> waiting bridge
        self.bridges = 0
        self.via: tuple | None = None              # (relay actor, addr, fp) this node is registered with
        self.observed: str | None = None           # this node's address as its relay sees it
        self.punch_port: int | None = None         # the local port of the registration, reused to punch

    # -- TLS -----------------------------------------------------------------------------------------------------
    def _server_ctx(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert, self.key)
        return ctx

    @staticmethod
    def _client_ctx():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # no CA: certificates are pinned by fingerprint
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def _open(self, addr: str, cert_fp: str | None, bind: int | None = None, timeout: float = 15):
        """A TLS connection to `addr`; `cert_fp` pins it (None: the caller checks who answered)."""
        host, _, port = addr.rpartition(":")
        host = host.strip("[]")
        raw = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        try:
            if bind is not None:
                _reuse(raw)
                raw.bind(("", bind))
            raw.settimeout(timeout)
            raw.connect((host, int(port)))
            sock = self._client_ctx().wrap_socket(raw)
        except BaseException:
            raw.close()
            raise
        fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
        if cert_fp and not secrets.compare_digest(fp, cert_fp):
            sock.close()
            raise ConnectionError(f"{addr} is not the peer we expected (certificate {fp[:16]}…)")
        sock.settimeout(SESSION)
        return sock, fp

    # -- authentication ------------------------------------------------------------------------------------------
    def _auth_server(self, sock, hello: dict, bind_fp: str):
        """Prove both keys; (access, actor, key), or None after telling the peer why."""
        if hello.get("t") != "hello" or hello.get("v") != VERSION:
            send(sock, {"t": "error", "error": "protocol version"})
            return None
        nonce = secrets.token_hex(16)
        send(sock, {"t": "challenge", "nonce": nonce, "project": self.store.state().project_id})
        auth = recv(sock, PRE_AUTH)
        key = str(hello.get("key", ""))
        if not crypto.verify(key, CONTEXT + nonce.encode() + b"|" + bind_fp.encode(), str(auth.get("sig", ""))):
            send(sock, {"t": "error", "error": "the signature does not match the key"})
            return None
        actor = str(hello.get("actor", ""))
        access = access_of(self.store, actor, key, hello.get("project"), hello.get("token"), self.invites)
        if access is None:
            send(sock, {"t": "error", "error": "not a member of this project and no valid invite"})
            return None
        my_key, my_sig = self.store.keyring.sign(self.actor, CONTEXT + b"server|" + str(hello.get("nonce", "")).encode()
                                                 + b"|" + key.encode())
        send(sock, {"t": "welcome", "access": access, "actor": self.actor, "key": my_key, "sig": my_sig})
        return access, actor, key

    def _auth_client(self, sock, bind_fp: str, project: str | None = None, token: str | None = None) -> dict:
        my_key = self.store.keyring.public(self.actor)
        nonce = secrets.token_hex(16)
        send(sock, {"t": "hello", "v": VERSION, "actor": self.actor, "key": my_key, "nonce": nonce,
                    "project": project or self.store.state().project_id, "token": token})
        ch = _expect(recv(sock), "challenge")
        _, sig = self.store.keyring.sign(self.actor, CONTEXT + str(ch["nonce"]).encode() + b"|" + bind_fp.encode())
        send(sock, {"t": "auth", "sig": sig})
        w = recv(sock)
        if w.get("t") != "welcome":
            raise PermissionError(w.get("error", "refused"))
        if not crypto.verify(w["key"], CONTEXT + b"server|" + nonce.encode() + b"|" + my_key.encode(), w["sig"]):
            raise ConnectionError("the peer could not prove its key")
        st = self.store.state()
        known = st.actors.get(w["actor"])
        if st.project_id and known is not None and known.key and known.key != w["key"]:
            raise ConnectionError(f"the peer claims to be {w['actor']} with another key")
        return w

    # -- exchange ------------------------------------------------------------------------------------------------
    def _send_blobs(self, sock, ids):
        for h in ids:
            data = self.store.blob(h)
            if data is not None and len(data) <= MAX_BLOB:
                send(sock, {"t": "blob", "id": h, "data": base64.b64encode(data).decode()})
        send(sock, {"t": "blobs-done"})

    def _recv_blobs(self, sock, announced: set) -> int:
        n = 0
        while True:
            f = recv(sock)
            if f.get("t") == "blobs-done":
                return n
            h = f.get("id")
            if f.get("t") != "blob" or h not in announced:
                continue
            data = base64.b64decode(f.get("data", ""))
            if len(data) <= MAX_BLOB and hashlib.sha256(data).hexdigest() == h:
                self.store.put_blob(data)
                n += 1

    def _server_sync(self, sock, have: dict, access: str, actor: str):
        mine = visible(self.store, access)
        theirs = set(have.get("events", []))
        held = self.store.read()[0]
        full = access == "full"
        my_blobs = set(self.store.blob_ids()) if full else set()
        their_blobs = set(have.get("blobs", [])) if full else set()
        sending = sorted(my_blobs - their_blobs)
        send(sock, {"t": "data", "events": [e.to_dict() for i, e in mine.items() if i not in theirs], "sending": sending,
                    "want": {"events": [i for i in theirs if i not in held], "blobs": sorted(their_blobs - my_blobs)}})
        self._send_blobs(sock, sending)
        back = _expect(recv(sock), "data")
        got = accept(self.store, access, actor, back.get("events", []))
        gotb = self._recv_blobs(sock, set(back.get("sending", [])) & (their_blobs - my_blobs))
        send(sock, {"t": "bye", "accepted": got})
        if got or gotb:
            self.log(f"p2p: {actor} sent {got} event(s), {gotb} blob(s)")

    def _client_sync(self, sock, w: dict, newcomer_project: str | None = None) -> dict:
        mine, _ = self.store.read()
        my_blobs = set(self.store.blob_ids())
        send(sock, {"t": "have", "events": list(mine), "blobs": sorted(my_blobs)})
        data = _expect(recv(sock), "data")
        evs = check_events(data.get("events", []))
        announced = set(data.get("sending", [])) - my_blobs
        gotb = self._recv_blobs(sock, announced)
        if newcomer_project and not self.store.state().project_id and not vouched(evs, newcomer_project, w["key"]):
            raise ConnectionError("that node could not show it belongs to this project")
        got = 0
        for e in evs:
            if e.id not in mine:
                self.store.write(e)   # signed events: any peer may relay them
                got += 1
        want = data.get("want", {})
        give = [h for h in want.get("blobs", []) if h in my_blobs]
        send(sock, {"t": "data", "events": [mine[i].to_dict() for i in want.get("events", []) if i in mine],
                    "sending": give})
        self._send_blobs(sock, give)
        bye = _expect(recv(sock), "bye")
        return {"peer": w["actor"], "access": w["access"], "received": got, "blobs": gotb,
                "sent": bye.get("accepted", 0)}

    # -- server --------------------------------------------------------------------------------------------------
    def _serve(self, sock, peer: str):
        """One incoming connection, from its hello on."""
        sock.settimeout(HANDSHAKE)
        got = self._auth_server(sock, recv(sock, PRE_AUTH), self.fp)
        if got is None:
            return
        access, actor, _ = got
        sock.settimeout(SESSION)
        first = recv(sock)
        t = first.get("t")
        if t == "have":
            return self._server_sync(sock, first, access, actor)
        if access != "full" or not self.relay:
            return send(sock, {"t": "error", "error": "this node does not relay for you"})
        if t == "relay-register":
            return self._control(sock, actor, peer)
        if t == "relay-accept":
            s = self.sessions.get(str(first.get("session")))
            if s and s["to"] == actor and s["sock"] is None:
                s["sock"] = sock
                s["ready"].set()
                s["done"].wait(BRIDGE_SECONDS + 10)   # the bridge owns this socket until it ends
            return
        if t in ("relay-connect", "punch"):
            return self._introduce(sock, actor, peer, first)
        send(sock, {"t": "error", "error": "unknown request"})

    def serve(self, host: str = "0.0.0.0", port: int = 7420) -> int:
        ctx = self._server_ctx()
        node = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                ip = self.client_address[0]
                with node.lock:
                    if node.active >= MAX_CONNECTIONS or node.per_ip.get(ip, 0) >= PER_IP:
                        return
                    node.active += 1
                    node.per_ip[ip] = node.per_ip.get(ip, 0) + 1
                try:
                    self.request.settimeout(HANDSHAKE)
                    with ctx.wrap_socket(self.request, server_side=True) as s:
                        node._serve(s, f"{ip}:{self.client_address[1]}")
                except (OSError, ValueError, ConnectionError, KeyError, ssl.SSLError):
                    pass
                finally:
                    with node.lock:
                        node.active -= 1
                        node.per_ip[ip] -= 1
                        if not node.per_ip[ip]:
                            del node.per_ip[ip]

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = S((host, port), H)
        self.addr = f"{host}:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        for sock, _, _ in list(self.registered.values()):
            try:
                sock.close()
            except OSError:
                pass

    # -- relaying (on a node others can reach) -------------------------------------------------------------------
    def _control(self, sock, actor: str, observed: str):
        """A member that cannot be reached keeps this connection open; we tell it when someone wants it."""
        lk = threading.Lock()
        self.registered[actor] = (sock, observed, lk)
        with lk:
            send(sock, {"t": "registered", "observed": observed})
        self.log(f"p2p: relaying for {actor} ({observed})")
        try:
            sock.settimeout(120)
            while True:
                if recv(sock, PRE_AUTH).get("t") == "ping":
                    with lk:
                        send(sock, {"t": "pong"})
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            if self.registered.get(actor, (None,))[0] is sock:
                del self.registered[actor]

    def _introduce(self, sock, actor: str, peer: str, msg: dict):
        target = str(msg.get("to", ""))
        reg = self.registered.get(target)
        t_actor = self.store.state().actors.get(target)
        if reg is None or t_actor is None or t_actor.revoked:
            return send(sock, {"t": "error", "error": f"{target} is not connected to this relay"})
        csock, observed, lk = reg
        if msg.get("t") == "punch":   # tell each side where the other is; they connect at the same moment
            with lk:
                send(csock, {"t": "punch", "from": actor, "at": peer})
            return send(sock, {"t": "punch-ok", "at": observed})
        if self.bridges >= MAX_BRIDGES:
            return send(sock, {"t": "error", "error": "this relay is busy"})
        sid = secrets.token_hex(8)
        sess = {"ready": threading.Event(), "done": threading.Event(), "sock": None, "to": target}
        self.sessions[sid] = sess
        try:
            with lk:
                send(csock, {"t": "dial", "session": sid, "from": actor})
            if not sess["ready"].wait(10):
                return send(sock, {"t": "error", "error": f"{target} did not answer"})
            other = sess["sock"]
            send(sock, {"t": "relay-ok"})
            send(other, {"t": "relay-ok", "from": actor})
            self.bridges += 1
            try:
                _bridge(sock, other)
            finally:
                self.bridges -= 1
        finally:
            sess["done"].set()
            self.sessions.pop(sid, None)

    # -- being relayed (on a node nobody can reach) ------------------------------------------------------------------
    def register(self, relay_actor: str, addr: str, fp: str, stop: threading.Event):
        """Stay connected to a relay, and answer the dials and punches it passes on, until `stop`."""
        while not stop.is_set():
            try:
                port = _free_port()
                sock, _ = self._open(addr, fp, bind=port)
                self.punch_port = port
                with sock:
                    self._auth_client(sock, fp)
                    send(sock, {"t": "relay-register"})
                    r = _expect(recv(sock), "registered")
                    self.via, self.observed = (relay_actor, addr, fp), r.get("observed")
                    self.log(f"p2p: reachable through {relay_actor} (seen as {self.observed})")
                    sock.settimeout(25)
                    while not stop.is_set():
                        try:
                            m = recv(sock, PRE_AUTH)
                        except socket.timeout:
                            send(sock, {"t": "ping"})
                            continue
                        if m.get("t") == "dial":
                            threading.Thread(target=self._answer_dial, args=(addr, fp, str(m.get("session"))),
                                             daemon=True).start()
                        elif m.get("t") == "punch":
                            threading.Thread(target=self._answer_punch, args=(str(m.get("from")), str(m.get("at"))),
                                             daemon=True).start()
            except NETWORK_ERRORS:
                pass
            self.via = None
            stop.wait(3)

    def _answer_dial(self, addr, fp, sid):
        try:
            sock, _ = self._open(addr, fp)
            with sock:
                self._auth_client(sock, fp)
                send(sock, {"t": "relay-accept", "session": sid})
                _expect(recv(sock), "relay-ok")
                self._serve(sock, "relayed")   # the far end now talks to us, end to end, through the relay
        except NETWORK_ERRORS:
            pass

    def _answer_punch(self, other: str, at: str):
        peer = self.store.state().actors.get(other)
        fp = ((peer.offer or {}).get("peer") or {}).get("cert") if peer else None
        if fp:
            self._punched(_simultaneous(self.punch_port, at), other, fp)

    def _punched(self, raw, other: str, other_fp: str) -> dict | None:
        """TLS over a punched connection: the actor whose name sorts first is the server."""
        if raw is None:
            return None
        try:
            if self.actor < other:
                with self._server_ctx().wrap_socket(raw, server_side=True) as tls:
                    self._serve(tls, "punched")
                return {"peer": other, "punched": True, "received": 0, "sent": 0, "blobs": 0}
            with self._client_ctx().wrap_socket(raw) as tls:
                fp = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                if fp != other_fp:
                    return None
                tls.settimeout(SESSION)
                r = self._client_sync(tls, self._auth_client(tls, fp))
                r["punched"] = True
                return r
        except NETWORK_ERRORS:
            return None
        finally:
            try:
                raw.close()
            except OSError:
                pass

    # -- client: three ways to reach a peer ------------------------------------------------------------------------
    def sync_with(self, addr: str, cert_fp: str | None, token: str | None = None, project: str | None = None) -> dict:
        """Directly. Without `cert_fp` (an address from the DHT), the node must prove it belongs to the project."""
        sock, fp = self._open(addr, cert_fp)
        with sock:
            w = self._auth_client(sock, fp, project, token)
            s = self.store.state()
            if cert_fp is None and s.project_id and not any(a.key == w["key"] and not a.revoked for a in s.actors.values()):
                raise ConnectionError(f"{addr} is not a member of this project")
            r = self._client_sync(sock, w, project if cert_fp is None else None)
            r["cert"] = fp
            return r

    def sync_via(self, relay_addr: str, relay_fp: str, target: str, target_fp: str) -> dict:
        """Through a relay both of us reach; the target still proves its own key to us, end to end."""
        sock, _ = self._open(relay_addr, relay_fp)
        with sock:
            self._auth_client(sock, relay_fp)
            send(sock, {"t": "relay-connect", "to": target})
            _expect(recv(sock), "relay-ok")
            w = self._auth_client(sock, target_fp)
            if w["actor"] != target:
                raise ConnectionError("the relay connected us to someone else")
            r = self._client_sync(sock, w)
            r["via"] = True
            return r

    def punch(self, relay_addr: str, relay_fp: str, target: str, target_fp: str, timeout: float = 4.0) -> dict | None:
        """Ask the relay to introduce us, then connect at the same moment. None if it did not work."""
        port = _free_port()
        try:
            sock, _ = self._open(relay_addr, relay_fp, bind=port)
            with sock:
                self._auth_client(sock, relay_fp)
                send(sock, {"t": "punch", "to": target})
                at = _expect(recv(sock), "punch-ok")["at"]
        except NETWORK_ERRORS:
            return None
        return self._punched(_simultaneous(port, at, timeout), target, target_fp)

    # -- who to talk to ------------------------------------------------------------------------------------------
    def peers(self) -> dict[str, dict]:
        """Other members' nodes from their signed offers, plus ones added by hand: name -> {addr|via, cert}."""
        out = {f"addr:{a}": {"addr": a, "cert": fp} for a, fp in self.extra.items()}
        s = self.store.state()
        for a in s.actors.values():
            p = (a.offer or {}).get("peer") or {}
            if a.name == self.actor or a.revoked or not isinstance(p, dict) or not p.get("cert"):
                continue
            if p.get("cert") == self.fp or (p.get("addr") and p.get("addr") == self.addr):
                continue
            out[a.name] = {"actor": a.name, "addr": p.get("addr"), "via": p.get("via"), "cert": p["cert"]}
        if self.addr:
            out.pop(f"addr:{self.addr}", None)
        return {k: v for k, v in out.items() if v.get("cert") != self.fp}

    def relays(self) -> list[tuple[str, str, str]]:
        """Members that offer to relay and can be reached: (actor, addr, cert), stewards first."""
        s = self.store.state()
        out = []
        for a in s.actors.values():
            p = (a.offer or {}).get("peer") or {}
            if a.name != self.actor and not a.revoked and p.get("relay") and p.get("addr") and p.get("cert"):
                out.append((a.name, p["addr"], p["cert"]))
        return sorted(out, key=lambda r: (s.actors[r[0]].right not in ("author", "steward"), r[0]))

    def gossip(self) -> dict:
        results = {}
        s = self.store.state()
        for name, p in self.peers().items():
            try:
                if p.get("addr"):
                    r = self.sync_with(p["addr"], p["cert"])
                elif p.get("via"):
                    relay = s.actors.get(p["via"])
                    rp = ((relay.offer or {}).get("peer") or {}) if relay else {}
                    if (relay is None or relay.name == self.actor) and self.server:
                        if name not in self.registered:
                            raise ConnectionError(f"{name} is not connected to this relay")
                        rp = {"addr": f"127.0.0.1:{self.server.server_address[1]}", "cert": self.fp}
                    if not rp.get("addr"):
                        raise ConnectionError(f"its relay {p['via']} cannot be reached")
                    r = self.punch(rp["addr"], rp["cert"], name, p["cert"]) or \
                        self.sync_via(rp["addr"], rp["cert"], name, p["cert"])
                else:
                    continue
                results[name] = {"ok": True, **r}
                if r.get("received") or r.get("sent") or r.get("blobs"):
                    how = " (hole punched)" if r.get("punched") else " (relayed)" if r.get("via") else ""
                    self.log(f"p2p: {r['peer']}{how}: got {r['received']} event(s), {r['blobs']} blob(s); gave {r['sent']}")
            except NETWORK_ERRORS as err:
                results[name] = {"ok": False, "error": str(err)}
                if self.status.get(name, {}).get("error") != str(err):
                    self.log(f"p2p: {name}: {err}")
            self.status[name] = {**results[name], "at": time.time()}
        return results

    def run(self, every: float, stop: threading.Event):
        while not stop.is_set():
            self.gossip()
            stop.wait(every)


def _reuse(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _reuse(s)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _simultaneous(local_port: int | None, at: str, timeout: float = 4.0):
    """TCP simultaneous open from `local_port` to `at` (host:port): retry until it connects or time is up."""
    if not local_port or not at or ":" not in at:
        return None
    host, _, port = at.rpartition(":")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _reuse(s)
        try:
            s.bind(("", local_port))
            s.settimeout(0.5)
            s.connect((host.strip("[]"), int(port)))
            s.settimeout(SESSION)
            return s
        except OSError:
            s.close()
            time.sleep(0.05)
    return None


def _bridge(a, b):
    """Copy bytes both ways between two connections until one closes, within the relay's limits."""
    a.settimeout(BRIDGE_SECONDS)
    b.settimeout(BRIDGE_SECONDS)
    total = [0]
    end = time.monotonic() + BRIDGE_SECONDS

    def pump(src, dst):
        try:
            while time.monotonic() < end and total[0] < BRIDGE_BYTES:
                data = src.recv(1 << 16)
                if not data:
                    break
                total[0] += len(data)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join(5)
