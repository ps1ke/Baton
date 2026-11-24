"""Reaching machines behind routers: port mapping, relays, hole punching and DHT discovery, all on localhost."""

import http.server
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from baton import crypto, dht, nat, p2p
from baton.store import Store

from helpers import POLICY, SONNET, T0, offer

REPO = "https://github.com/example/app.git"


class FakePMP(threading.Thread):
    """A router that speaks NAT-PMP on localhost."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.maps = {}

    def run(self):
        while True:
            try:
                data, src = self.sock.recvfrom(64)
            except OSError:
                return
            if data[:2] == b"\x00\x00":
                self.sock.sendto(struct.pack(">BBHI", 0, 128, 0, 1) + socket.inet_aton("203.0.113.9"), src)
            elif data[1] == 2:
                _, _, _, internal, ext, life = struct.unpack(">BBHHHI", data[:12])
                self.maps[internal] = life
                self.sock.sendto(struct.pack(">BBHIHHI", 0, 130, 0, 1, internal, internal + 1000 if life else 0, life), src)


class Mapping(unittest.TestCase):
    def test_natpmp_maps_and_unmaps_only_our_port(self):
        r = FakePMP()
        r.start()
        m = nat.natpmp_map(7420, gw="127.0.0.1", pmp_port=r.port)
        self.assertEqual((m.method, m.address, m.lease), ("natpmp", "203.0.113.9:8420", 3600))
        nat.natpmp_unmap(m, pmp_port=r.port)
        time.sleep(0.1)
        self.assertEqual(r.maps[7420], 0)                                     # lease 0: removed
        self.assertIsNone(nat.natpmp_map(7420, gw="8.8.8.8"))                 # never a public "gateway"
        r.sock.close()

    def test_upnp_reads_the_router_and_asks_for_a_mapping(self):
        calls = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = f"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>
                <service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/ctl</controlURL></service></serviceList></device></root>""".encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                action = self.headers["SOAPAction"].split("#")[1].strip('"')
                calls.append(action)
                self.rfile.read(int(self.headers["Content-Length"]))
                out = "<NewExternalIPAddress>198.51.100.4</NewExternalIPAddress>" if action == "GetExternalIPAddress" else ""
                self.send_response(200); self.end_headers(); self.wfile.write(f"<r>{out}</r>".encode())

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            loc = f"http://127.0.0.1:{srv.server_address[1]}/desc.xml"
            with mock.patch.object(nat, "private", lambda ip: ip == "127.0.0.1"):
                m = nat.upnp_map(7420, location=loc)
                self.assertEqual((m.method, m.address), ("upnp", "198.51.100.4:7420"))
                nat.upnp_unmap(m)
            self.assertEqual(calls, ["AddPortMapping", "GetExternalIPAddress", "DeletePortMapping"])
            self.assertIsNone(nat.upnp_map(7420, location="http://93.184.216.34/desc.xml"))   # not on our network
        finally:
            srv.shutdown()
            srv.server_close()


class Machines(unittest.TestCase):
    """R is a reachable steward that relays; A and B sit behind routers and reach only R."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.stores, self.nodes = {}, {}
        for name in ("R", "A", "B", "X"):
            s = Store(r / name / "log", keyring=crypto.Keyring(r / name / "keys"))
            s.init()
            (s.home / "policy.toml").write_text(POLICY, encoding="utf-8")
            self.stores[name] = s
        R = self.stores["R"]
        self.pid = R.append("project", "human:ana", {"name": "app", "repo": REPO, "join": "apply"}, T0).id
        self.nodes["R"] = p2p.Node(R, "m-r", r / "R" / "tls")
        self.rport = self.nodes["R"].serve("127.0.0.1", 0)
        self.raddr = f"127.0.0.1:{self.rport}"
        R.append("grant", "human:ana", {"to": "m-r", "key": R.keyring.public("m-r"), "right": "steward"}, T0 + 1)
        R.append("offer", "m-r", {**offer(SONNET), "peer": {"addr": self.raddr, "cert": self.nodes["R"].fp, "relay": True}}, T0 + 2)
        for name, actor in (("A", "m-a"), ("B", "m-b")):
            s = self.stores[name]
            self.nodes[name] = p2p.Node(s, actor, r / name / "tls")
            R.append("grant", "human:ana", {"to": actor, "key": s.keyring.public(actor), "right": "member"}, T0 + 3)
        for name in ("A", "B"):
            self.nodes[name].sync_with(self.raddr, self.nodes["R"].fp)
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()
        for n in self.nodes.values():
            n.stop()
        self.tmp.cleanup()

    def register(self, name):
        n = self.nodes[name]
        threading.Thread(target=n.register, args=("m-r", self.raddr, self.nodes["R"].fp, self.stop), daemon=True).start()
        for _ in range(100):
            if n.via:
                return
            time.sleep(0.05)
        self.fail(f"{name} did not register")

    def test_two_unreachable_machines_meet_through_the_relay(self):
        self.register("B")
        a, b = self.stores["A"], self.stores["B"]
        b.append("message", "m-b", {"text": "hello from behind a router"}, T0 + 10)
        b.put_blob(b"patch from B")
        r = self.nodes["A"].sync_via(self.raddr, self.nodes["R"].fp, "m-b", self.nodes["B"].fp)
        self.assertTrue(r["via"])
        self.assertIn("hello from behind a router", [m.text for m in a.state().messages.values()])
        self.assertEqual(a.blob(__import__("hashlib").sha256(b"patch from B").hexdigest()), b"patch from B")

    def test_the_relay_refuses_strangers_and_wrong_targets(self):
        self.register("B")
        x = p2p.Node(self.stores["X"], "m-x", Path(self.tmp.name) / "X" / "tls")
        with self.assertRaises(PermissionError):
            x.sync_via(self.raddr, self.nodes["R"].fp, "m-b", self.nodes["B"].fp)
        with self.assertRaises((ConnectionError, PermissionError)):   # B is not who we pinned: refused
            self.nodes["A"].sync_via(self.raddr, self.nodes["R"].fp, "m-b", "0" * 64)

    def test_hole_punching_then_gossip_falls_back_to_the_relay(self):
        self.register("B")
        self.stores["B"].append("message", "m-b", {"text": "punched"}, T0 + 10)
        r = self.nodes["A"].punch(self.raddr, self.nodes["R"].fp, "m-b", self.nodes["B"].fp)
        if r is None:   # some systems do not allow simultaneous open even on localhost: the relay still works
            r = self.nodes["A"].sync_via(self.raddr, self.nodes["R"].fp, "m-b", self.nodes["B"].fp)
        self.assertTrue(r)
        # B announces "reach me through m-r"; A's gossip uses that, without any address for B
        self.stores["B"].append("offer", "m-b", {**offer(SONNET), "peer": {"via": "m-r", "cert": self.nodes["B"].fp}}, T0 + 11)
        self.nodes["B"].sync_with(self.raddr, self.nodes["R"].fp)
        self.nodes["A"].sync_with(self.raddr, self.nodes["R"].fp)
        self.stores["B"].append("message", "m-b", {"text": "found through my offer"}, T0 + 12)
        out = self.nodes["A"].gossip()
        self.assertTrue(out["m-b"]["ok"], out)
        self.assertIn("found through my offer", [m.text for m in self.stores["A"].state().messages.values()])


class FakeDHT:
    """Two DHT nodes on localhost: the first only knows the second, the second stores announcements."""

    def __init__(self):
        self.socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
        for s in self.socks:
            s.bind(("127.0.0.1", 0))
        self.ids = [b"\x01" * 20, b"\x02" * 20]
        self.peers = []
        for i in range(2):
            threading.Thread(target=self.serve, args=(i,), daemon=True).start()

    def addr(self, i):
        return self.socks[i].getsockname()

    def serve(self, i):
        s = self.socks[i]
        while True:
            try:
                data, src = s.recvfrom(4096)
            except OSError:
                return
            q = dht.bdecode(data)
            r = {b"id": self.ids[i]}
            if q[b"q"] == b"get_peers":
                if i == 0:
                    ip, port = self.addr(1)
                    r[b"nodes"] = self.ids[1] + socket.inet_aton(ip) + struct.pack(">H", port)
                else:
                    r[b"token"] = b"tok"
                    if self.peers:
                        r[b"values"] = [socket.inet_aton(ip) + struct.pack(">H", p) for ip, p in self.peers]
            elif q[b"q"] == b"announce_peer" and i == 1 and q[b"a"][b"token"] == b"tok":
                port = src[1] if q[b"a"].get(b"implied_port") else q[b"a"][b"port"]   # BEP 5: the sender's port
                if (src[0], port) not in self.peers:
                    self.peers.append((src[0], port))
            s.sendto(dht.bencode({b"t": q[b"t"], b"y": b"r", b"r": r}), src)

    def close(self):
        for s in self.socks:
            s.close()


class LocalNetworks(unittest.TestCase):
    def test_a_beacon_answers_only_for_its_project(self):
        from baton import lan
        if not lan.interfaces():
            self.skipTest("no network interface besides loopback")
        b = lan.Beacon("ab" * 32, 7999, "f" * 64).start()
        try:
            time.sleep(0.3)
            found = lan.find("ab" * 32, wait=2)
            if not found:
                self.skipTest("multicast is not delivered on this machine")
            self.assertTrue(all(a.endswith(":7999") and c == "f" * 64 for a, c in found))
            self.assertEqual(lan.find("cd" * 32, wait=0.5), [])
        finally:
            b.close()


class Discovery(unittest.TestCase):
    def test_bencode_round_trips(self):
        x = {b"a": [1, b"two", {b"c": -3}], b"z": b""}
        self.assertEqual(dht.bdecode(dht.bencode(x)), x)
        with self.assertRaises(ValueError):
            dht.bdecode(b"999999:x")

    def test_announce_then_find_by_project_id(self):
        net = FakeDHT()
        try:
            boot = [net.addr(0)]
            self.assertEqual(dht.find("ab" * 32, bootstrap=boot, announce_port=7420), [])
            self.assertEqual(dht.find("ab" * 32, bootstrap=boot), [("127.0.0.1", 7420)])
            self.assertEqual(len(dht.key_of("ab" * 32)), 20)
        finally:
            net.close()

    def test_an_unpinned_node_must_prove_it_belongs_to_the_project(self):
        tmp = tempfile.TemporaryDirectory()
        r = Path(tmp.name)
        real = Store(r / "real", keyring=crypto.Keyring(r / "k1"))
        pid = real.append("project", "human:ana", {"name": "app", "repo": REPO, "join": "open"}, T0).id
        fake = Store(r / "fake", keyring=crypto.Keyring(r / "k2"))
        fake.append("project", "human:eve", {"name": "app", "repo": REPO, "join": "open"}, T0)   # another genesis
        nodes = []
        try:
            for s, actor in ((real, "human:ana"), (fake, "human:eve")):
                n = p2p.Node(s, actor, r / f"tls-{actor[-3:]}")
                n.port = n.serve("127.0.0.1", 0)
                nodes.append(n)
            newcomer = Store(r / "new", keyring=crypto.Keyring(r / "k3"))
            me = p2p.Node(newcomer, "m-new", r / "tls-new")
            with self.assertRaises((ConnectionError, PermissionError)):
                me.sync_with(f"127.0.0.1:{nodes[1].port}", None, project=pid)   # a DHT address that lies
            self.assertEqual(newcomer.read()[0], {})
            me.sync_with(f"127.0.0.1:{nodes[0].port}", None, project=pid)
            self.assertEqual(newcomer.state().project_id, pid)
            # a project that takes applications, but is discoverable: a stranger may read the card and apply
            real.append("project", "human:ana", {"join": "apply", "discoverable": True}, T0 + 1)
            other = Store(r / "new2", keyring=crypto.Keyring(r / "k4"))
            o = p2p.Node(other, "m-other", r / "tls-other")
            first = o.sync_with(f"127.0.0.1:{nodes[0].port}", None, project=pid)
            self.assertEqual(first["access"], "applicant")
            other.append("apply", "m-other", {"offer": offer(SONNET)}, T0 + 2)
            o.sync_with(f"127.0.0.1:{nodes[0].port}", first["cert"], project=pid)
            self.assertEqual([x.actor for x in real.state().applications.values()], ["m-other"])
            self.assertNotIn("task", {e.kind for e in other.read()[0].values()})
        finally:
            for n in nodes:
                n.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class Punching(unittest.TestCase):
    """Two machines that only know a project id meet through the DHT and talk over punched UDP."""

    def setUp(self):
        from baton import udp
        self.udp = udp
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.net = FakeDHT()
        self.steward_store = Store(r / "s", keyring=crypto.Keyring(r / "ks"))
        self.steward_store.init()
        (self.steward_store.home / "policy.toml").write_text(POLICY, encoding="utf-8")
        s = self.steward_store
        self.pid = s.append("project", "human:ana", {"name": "app", "repo": REPO, "join": "apply", "discoverable": True}, T0).id
        s.append("grant", "human:ana", {"to": "m-s", "key": s.keyring.public("m-s"), "right": "steward"}, T0 + 1)
        self.joiner_store = Store(r / "j", keyring=crypto.Keyring(r / "kj"))
        self.joiner_store.init()
        self.joiner_store.append("apply", "m-j", {"offer": offer(SONNET), "note": "from far away"}, T0 + 2)
        self.sn = p2p.Node(s, "m-s", r / "ts")
        self.jn = p2p.Node(self.joiner_store, "m-j", r / "tj")
        self.jn.pending_project = self.pid
        boot = [self.net.addr(0)]
        self.hubs = [udp.Hub(self.sn, self.pid, bootstrap=boot), udp.Hub(self.jn, self.pid, bootstrap=boot)]

    def tearDown(self):
        for h in self.hubs:
            h.close()
        self.net.close()
        self.tmp.cleanup()

    def wait_for(self, cond, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    def test_an_applicant_and_a_steward_meet_by_project_id_alone(self):
        for h in self.hubs:   # both announce, then both look each other up and punch
            h.round()
        for h in self.hubs:
            h.round()
        self.assertTrue(self.wait_for(lambda: self.steward_store.state().applications), "the application did not arrive")
        self.assertEqual([a.actor for a in self.steward_store.state().applications.values()], ["m-j"])
        self.assertTrue(self.wait_for(lambda: self.joiner_store.state().project_id == self.pid))
        self.assertNotIn("task", {e.kind for e in self.joiner_store.read()[0].values()})   # the card only

    def test_the_stream_survives_packet_loss(self):
        import random
        s = self.steward_store
        s.append("grant", "human:ana", {"to": "m-j", "key": self.joiner_store.keyring.public("m-j"), "right": "member"}, T0 + 3)
        self.joiner_store.merge_from(s)
        self.jn.pending_project = None
        blob = s.put_blob(random.randbytes(300_000))
        for h in self.hubs:   # one in five packets is lost
            orig = h.raw
            h.raw = (lambda o: lambda addr, payload: None if random.random() < 0.2 else o(addr, payload))(orig)
        for h in self.hubs:
            h.round()
        for h in self.hubs:
            h.round()
        self.assertTrue(self.wait_for(lambda: self.joiner_store.blob(blob) is not None, 60), "the blob did not arrive")
