"""Peer to peer: machines exchange a project's events and blobs directly, with no server and no git remote.

Every machine that takes part runs a small node (`baton peer serve`, or `baton swarm --p2p`). A node listens on TCP,
and every few seconds connects to the other members' nodes, whose addresses and certificate fingerprints it learns
from their signed offers in the log (or from an invite, or `baton peer add`). Each connection:

1. TLS. The client pins the server's certificate fingerprint; there is no certificate authority to trust.
2. Both sides prove who they are: the server sends a nonce, the client signs nonce + the server's certificate
   fingerprint with its member key (so a relay in the middle cannot reuse it), and the server signs the client's
   nonce back.
3. Access by trust, decided from the server's own copy of the log:
   - a member, steward or the author (a key bound to an actor that is not revoked) gets everything;
   - the holder of a valid invite gets the project's public events only, and may send its own `apply`;
   - anyone else is refused.
4. Each side sends the ids of the events and blobs it holds; each then sends what the other lacks. Every event is
   checked (id and signature) before it is written; every blob is checked against its SHA-256.

Messages are JSON, each prefixed by its length (4 bytes, big-endian). Nothing in the exchange can make a peer run
anything: events are data that the fold judges, blobs are patches that only an agent's worktree ever applies.
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
MAX_FRAME = 64 * 1024 * 1024
MAX_BLOB = 32 * 1024 * 1024
MAX_EVENTS = 20000
CONTEXT = b"baton-p2p-v3|"
PUBLIC_KINDS = ("project", "policy")   # what an invited, not yet admitted, peer may read


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
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("peer closed the connection")
        buf += chunk
    return buf


def recv(sock):
    (n,) = struct.unpack(">I", _exact(sock, 4))
    if n > MAX_FRAME:
        raise ConnectionError("frame too large")
    return json.loads(_exact(sock, n))


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
    if s.project_id and project == s.project_id and (invites.valid(project, token) or s.join_mode == "open"):
        return "applicant"   # an open project needs no invite to read its card and deliver a join
    return None


def visible(store: Store, access: str) -> dict:
    found, _ = store.read()
    if access == "full":
        return found
    return {i: e for i, e in found.items() if e.kind in PUBLIC_KINDS}


def accept(store: Store, access: str, actor: str, dicts: list) -> int:
    """Write the peer's events that check out. An applicant may only deliver its own `apply` (and `join`)."""
    n = 0
    mine, _ = store.read()
    for d in dicts[:MAX_EVENTS]:
        try:
            e = ev.from_dict(d)
        except ev.BadEvent:
            continue
        if e.id in mine:
            continue
        if access != "full" and not (e.actor == actor and e.kind in ("apply", "join")):
            continue
        store.write(e)
        n += 1
    return n


def accept_blobs(store: Store, blobs: dict) -> int:
    n = 0
    for h, b64 in list(blobs.items())[:1000]:
        data = base64.b64decode(b64)
        if len(data) <= MAX_BLOB and hashlib.sha256(data).hexdigest() == h:
            store.put_blob(data)
            n += 1
    return n


def pack_blobs(store: Store, ids) -> dict:
    out = {}
    for h in ids:
        data = store.blob(h)
        if data is not None:
            out[h] = base64.b64encode(data).decode()
    return out


# -- the node ------------------------------------------------------------------------------------------------------
class Node:
    """One machine's peer for one project log, acting as `actor`."""

    def __init__(self, store: Store, actor: str, tls_dir: Path, invites: Invites | None = None, log=None):
        self.store, self.actor = store, actor
        self.cert, self.key, self.fp = tls_identity(tls_dir)
        self.invites = invites or Invites(tls_dir)
        self.log = log or (lambda *_: None)
        self.server = None
        self.addr = None
        self.extra: dict[str, str] = {}      # addr -> cert fingerprint, added by hand or from invites
        self.status: dict[str, dict] = {}    # addr -> last outcome

    # server side -----------------------------------------------------------------------------------------------
    def _handle(self, sock):
        hello = recv(sock)
        if hello.get("v") != VERSION:
            return send(sock, {"t": "error", "error": "protocol version"})
        nonce = secrets.token_hex(16)
        send(sock, {"t": "challenge", "nonce": nonce, "project": self.store.state().project_id})
        auth = recv(sock)
        msg = CONTEXT + nonce.encode() + b"|" + self.fp.encode()
        if not crypto.verify(hello.get("key", ""), msg, auth.get("sig", "")):
            return send(sock, {"t": "error", "error": "the signature does not match the key"})
        access = access_of(self.store, hello.get("actor", ""), hello.get("key", ""), hello.get("project"),
                           hello.get("token"), self.invites)
        if access is None:
            return send(sock, {"t": "error", "error": "not a member of this project and no valid invite"})
        my_key, my_sig = self.store.keyring.sign(self.actor, CONTEXT + b"server|" + str(hello.get("nonce", "")).encode()
                                                 + b"|" + hello["key"].encode())
        send(sock, {"t": "welcome", "access": access, "actor": self.actor, "key": my_key, "sig": my_sig})
        have = recv(sock)
        mine = visible(self.store, access)
        theirs = set(have.get("events", []))
        give = [e.to_dict() for i, e in mine.items() if i not in theirs]
        held = self.store.read()[0]
        want_events = [i for i in theirs if i not in held]
        blobs = {}
        want_blobs = []
        if access == "full":
            my_blobs = set(self.store.blob_ids())
            blobs = pack_blobs(self.store, [h for h in my_blobs if h not in set(have.get("blobs", []))])
            want_blobs = [h for h in have.get("blobs", []) if h not in my_blobs]
        send(sock, {"t": "data", "events": give, "blobs": blobs,
                    "want": {"events": list(want_events), "blobs": want_blobs}})
        back = recv(sock)
        got = accept(self.store, access, hello["actor"], back.get("events", []))
        gotb = accept_blobs(self.store, back.get("blobs", {})) if access == "full" else 0
        send(sock, {"t": "bye", "accepted": got})
        if got or gotb:
            self.log(f"p2p: {hello['actor']} sent {got} event(s), {gotb} blob(s)")

    def serve(self, host: str = "0.0.0.0", port: int = 7420):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert, self.key)
        node = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with ctx.wrap_socket(self.request, server_side=True) as s:
                        s.settimeout(60)
                        node._handle(s)
                except (OSError, ValueError, ConnectionError, KeyError, ssl.SSLError):
                    pass

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

    # client side -----------------------------------------------------------------------------------------------
    def sync_with(self, addr: str, cert_fp: str, token: str | None = None, project: str | None = None) -> dict:
        host, _, port = addr.rpartition(":")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # no CA: the certificate is pinned by its fingerprint below
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.create_connection((host, int(port)), timeout=20) as raw, ctx.wrap_socket(raw) as sock:
            got_fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
            if not cert_fp or not secrets.compare_digest(got_fp, cert_fp):
                raise ConnectionError(f"{addr} is not the peer we expected (certificate {got_fp[:16]}…)")
            my_key = self.store.keyring.public(self.actor)
            nonce = secrets.token_hex(16)
            send(sock, {"t": "hello", "v": VERSION, "actor": self.actor, "key": my_key, "nonce": nonce,
                        "project": project or self.store.state().project_id, "token": token})
            ch = recv(sock)
            if ch.get("t") != "challenge":
                raise ConnectionError(ch.get("error", "unexpected answer"))
            _, sig = self.store.keyring.sign(self.actor, CONTEXT + ch["nonce"].encode() + b"|" + got_fp.encode())
            send(sock, {"t": "auth", "sig": sig})
            w = recv(sock)
            if w.get("t") != "welcome":
                raise PermissionError(w.get("error", "refused"))
            if not crypto.verify(w["key"], CONTEXT + b"server|" + nonce.encode() + b"|" + my_key.encode(), w["sig"]):
                raise ConnectionError("the peer could not prove its key")
            st = self.store.state()
            known = st.actors.get(w["actor"])
            if st.project_id and known is not None and known.key and known.key != w["key"]:
                raise ConnectionError(f"{addr} claims to be {w['actor']} with another key")
            mine, _ = self.store.read()
            send(sock, {"t": "have", "events": list(mine), "blobs": self.store.blob_ids()})
            data = recv(sock)
            got = accept(self.store, "full", w["actor"], data.get("events", []))   # signed events: any peer may relay
            gotb = accept_blobs(self.store, data.get("blobs", {}))
            want = data.get("want", {})
            send(sock, {"t": "data", "events": [mine[i].to_dict() for i in want.get("events", []) if i in mine],
                        "blobs": pack_blobs(self.store, want.get("blobs", []))})
            bye = recv(sock)
            return {"peer": w["actor"], "access": w["access"], "received": got, "blobs": gotb,
                    "sent": bye.get("accepted", 0)}

    def peers(self) -> dict[str, str]:
        """addr -> cert fingerprint: other members' nodes, from their signed offers, plus ones added by hand."""
        out = dict(self.extra)
        s = self.store.state()
        for a in s.actors.values():
            p = (a.offer or {}).get("peer") or {}
            if a.name != self.actor and not a.revoked and isinstance(p, dict) and p.get("addr") and p.get("cert"):
                out[str(p["addr"])] = str(p["cert"])
        if self.addr:
            out.pop(self.addr, None)
        return {k: v for k, v in out.items() if v != self.fp}

    def gossip(self) -> dict:
        results = {}
        for addr, fp in self.peers().items():
            try:
                r = self.sync_with(addr, fp)
                results[addr] = {"ok": True, **r}
                if r["received"] or r["sent"] or r["blobs"]:
                    self.log(f"p2p: {r['peer']} at {addr}: got {r['received']} event(s), {r['blobs']} blob(s); "
                             f"gave {r['sent']}")
            except (OSError, ConnectionError, PermissionError, ValueError, KeyError, ssl.SSLError) as err:
                results[addr] = {"ok": False, "error": str(err)}
                if self.status.get(addr, {}).get("error") != str(err):
                    self.log(f"p2p: {addr}: {err}")
            self.status[addr] = {**results[addr], "at": time.time()}
        return results

    def run(self, every: float, stop: threading.Event):
        while not stop.is_set():
            self.gossip()
            stop.wait(every)
