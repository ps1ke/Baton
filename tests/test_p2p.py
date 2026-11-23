import tempfile
import unittest
from pathlib import Path

from baton import crypto, p2p
from baton.store import Store

from helpers import POLICY, SONNET, T0, offer

REPO = "https://github.com/example/importer.git"


class P2P(unittest.TestCase):
    """Two machines, each with its own keys, log copy and TLS identity, talking over a real socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.a = Store(r / "a" / "log", keyring=crypto.Keyring(r / "a" / "keys"))
        self.b = Store(r / "b" / "log", keyring=crypto.Keyring(r / "b" / "keys"))
        for s in (self.a, self.b):
            s.init()
            (s.home / "policy.toml").write_text(POLICY, encoding="utf-8")
        self.pid = self.a.append("project", "human:ana", {"name": "importer", "repo": REPO, "join": "apply"}, T0).id
        self.a.append("task", "human:ana", {"title": "secret plan", "paths": ["src"]}, T0 + 1)
        self.na = p2p.Node(self.a, "human:ana", r / "a" / "tls")
        self.port = self.na.serve("127.0.0.1", 0)
        self.addr = f"127.0.0.1:{self.port}"
        self.nb = p2p.Node(self.b, "m-bob", r / "b" / "tls")

    def tearDown(self):
        self.na.stop()
        self.tmp.cleanup()

    def kinds(self, s):
        return sorted(e.kind for e in s.read()[0].values())

    def test_strangers_are_refused_and_a_wrong_certificate_is_caught(self):
        with self.assertRaises(PermissionError):
            self.nb.sync_with(self.addr, self.na.fp, project=self.pid)
        with self.assertRaises(ConnectionError):
            self.nb.sync_with(self.addr, "0" * 64, project=self.pid)
        self.assertEqual(self.kinds(self.b), [])

    def test_an_invite_shows_the_public_card_and_carries_the_application_back(self):
        token = self.na.invites.new(self.pid, 3600)
        url = p2p.invite_url(self.addr, self.pid, self.na.fp, token)
        inv = p2p.parse_invite(url)
        r = self.nb.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
        self.assertEqual(r["access"], "applicant")
        self.assertEqual(self.kinds(self.b), ["project"])                    # not the board
        self.b.append("apply", "m-bob", {"offer": offer(SONNET), "note": "hi"}, T0 + 5)
        self.b.append("message", "m-bob", {"text": "let me in"}, T0 + 6)    # not a member yet: not carried
        self.nb.sync_with(self.addr, self.na.fp, token=token, project=self.pid)
        st = self.a.state()
        self.assertEqual([x.actor for x in st.applications.values()], ["m-bob"])
        self.assertNotIn("message", self.kinds(self.a))
        self.assertFalse(self.na.invites.valid(self.pid, "guess"))

    def test_once_admitted_everything_flows_both_ways_including_patches(self):
        token = self.na.invites.new(self.pid, 3600)
        self.nb.sync_with(self.addr, self.na.fp, token=token, project=self.pid)
        self.b.append("apply", "m-bob", {"offer": offer(SONNET)}, T0 + 5)
        self.nb.sync_with(self.addr, self.na.fp, token=token, project=self.pid)
        self.a.append("admit", "human:ana", {"applicant": "m-bob"}, T0 + 6)
        r = self.nb.sync_with(self.addr, self.na.fp, project=self.pid)
        self.assertEqual(r["access"], "full")
        self.assertIn("task", self.kinds(self.b))
        patch = self.b.put_blob(b"diff --git a/x b/x\n")
        self.b.append("message", "m-bob", {"text": "patch " + patch}, T0 + 7)
        self.nb.sync_with(self.addr, self.na.fp, project=self.pid)
        self.assertEqual(self.a.blob(patch), b"diff --git a/x b/x\n")
        self.assertEqual(set(self.a.read()[0]), set(self.b.read()[0]))
        self.a.append("revoke", "human:ana", {"actor": "m-bob"}, T0 + 8)
        with self.assertRaises(PermissionError):
            self.nb.sync_with(self.addr, self.na.fp, project=self.pid)

    def test_members_find_each_other_through_their_signed_offers(self):
        self.a.append("project", "human:ana", {"join": "open"}, T0 + 2)
        self.nb.extra[self.addr] = self.na.fp
        token = self.na.invites.new(self.pid, 60)
        self.nb.sync_with(self.addr, self.na.fp, token=token, project=self.pid)   # the public card: join is open
        self.b.append("join", "m-bob", {"offer": {**offer(SONNET), "peer": {"addr": "127.0.0.1:1", "cert": "f" * 64}}}, T0 + 3)
        self.nb.gossip()
        self.assertIn("m-bob", self.a.state().actors)
        self.assertEqual(self.na.peers(), {"127.0.0.1:1": "f" * 64})          # learned from bob's offer
        self.assertFalse(self.na.gossip()["127.0.0.1:1"]["ok"])               # nobody there: reported, not fatal


if __name__ == "__main__":
    unittest.main()
