import json
import tempfile
import unittest
from pathlib import Path

from baton import crypto, network
from baton import events as ev
from baton.store import Store

from helpers import FABLE, OPUS, POLICY, SONNET, T0, offer

REPO = "https://github.com/example/importer.git"


class Trust(unittest.TestCase):
    """A project: human:ana signs the genesis (the author). Machines join as members; the author grants stewards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = Store(self.root / "log")
        self.s.init()
        (self.s.home / "policy.toml").write_text(POLICY, encoding="utf-8")
        a = self.s.append
        a("project", "human:ana", {"name": "importer", "repo": REPO, "join": "open"}, T0)
        a("join", "lead", {"offer": offer(FABLE, OPUS)}, T0 + 1)
        a("join", "w", {"offer": offer(SONNET)}, T0 + 2)
        self.lead_key = self.s.keyring.public("lead")

    def tearDown(self):
        self.tmp.cleanup()

    def grant(self, to, t, right="steward", by="human:ana"):
        return self.s.append("grant", by, {"to": to, "key": self.s.keyring.public(to), "right": right}, T0 + t)

    def why(self, e):
        st = self.s.state()
        return st.ignored.get(e.id) or st.denied.get(e.id)

    def test_the_genesis_signer_is_the_author_and_a_project_starts_from_a_repo(self):
        st = self.s.state()
        self.assertEqual((st.author, st.actors["human:ana"].right, st.mode), ("human:ana", "author", "project"))
        bare = Store(self.root / "bare")
        e = bare.append("project", "human:x", {"name": "no repo"}, T0)
        self.assertIn("starts from a repository", bare.state().ignored[e.id])

    def test_forged_and_unsigned_events_do_not_count(self):
        other = crypto.Keyring(self.root / "mallory-keys")
        forged = Store(self.s.home, keyring=other).append("approve", "human:ana", {"proposal": "x" * 64}, T0 + 5)
        self.assertIn("not human:ana's", self.why(forged))                   # a valid signature, by the wrong key
        e = ev.make("grant", "human:ana", {"to": "w", "key": "0" * 64}, [], 99, T0 + 6)   # no signature at all
        (self.s.dir / f"{e.id}.json").write_text(json.dumps(e.to_dict()))
        _, _, bad = self.s.load()
        self.assertIn(f"{e.id}.json", bad)
        signed = Store(self.root / "x").append("message", "w", {"text": "hi"}, T0)
        d = signed.to_dict()
        d["body"]["text"] = "changed in transit"
        with self.assertRaises(ev.BadEvent):
            ev.from_dict(d)

    def test_only_the_author_grants_and_sets_the_policy(self):
        self.assertIn("only the author", self.why(self.grant("w", 5, by="lead")))
        self.assertIsNone(self.why(self.grant("lead", 6)))
        self.assertEqual(self.s.state().actors["lead"].right, "steward")
        self.assertIn("only the author", self.why(self.grant("w", 7, by="lead")))   # stewards cannot make stewards
        p = self.s.append("policy", "lead", {"toml": POLICY.replace('max_depth = 3', 'max_depth = 9')}, T0 + 8)
        self.assertIn("only the author", self.why(p))
        self.s.append("policy", "human:ana", {"toml": POLICY.replace('max_depth = 3', 'max_depth = 2')}, T0 + 9)
        self.assertEqual(self.s.state().policy.max_depth, 2)

    def test_members_cannot_make_level0_decisions(self):
        root = self.s.append("task", "w", {"title": "my own plan"}, T0 + 5)
        self.assertIn("children of an orchestrate", self.why(root))
        self.s.append("apply", "newbie", {"offer": offer()}, T0 + 6)
        self.assertIn("level-0 decision", self.why(self.s.append("admit", "w", {"applicant": "newbie"}, T0 + 7)))
        prop = self.s.append("propose", "w", {"action": "comment", "target": "#1", "payload": {"repo": "o/r", "body": "x"}}, T0 + 8)
        self.assertIn("level-0 decision", self.why(self.s.append("approve", "w", {"proposal": prop.id}, T0 + 9)))
        push = self.s.append("propose", "w", {"action": "push", "target": "main", "payload": {"branch": "x"}}, T0 + 10)
        self.assertEqual(self.s.state().proposals[push.id].status, "not_level0")
        self.grant("lead", 11)
        self.assertIsNone(self.why(self.s.append("admit", "lead", {"applicant": "newbie"}, T0 + 12)))
        ok = self.s.append("propose", "lead", {"action": "push", "target": "main", "payload": {"branch": "baton/x"}}, T0 + 13)
        self.assertEqual(self.s.state().proposals[ok.id].status, "pending")
        self.assertIn("level-0 decision", self.why(self.s.append("executed", "w", {"proposal": ok.id}, T0 + 14)))

    def test_level0_waits_for_a_steward_while_deeper_work_goes_on(self):
        root = self.s.append("task", "human:ana", {"title": "importer", "size": "L", "paths": ["src"]}, T0 + 5).id
        small = self.s.append("task", "human:ana", {"title": "fix a typo", "paths": ["docs"]}, T0 + 6).id
        st = self.s.state()
        picks = network.assign(st, st.policy, T0 + 7)
        self.assertNotIn(root, picks)                                          # lead has frontier, but is no steward
        self.assertEqual(picks[small].member, "w")                             # the rest of the work goes on
        self.assertIn("stewards", network.why_not(st, st.policy, T0 + 7, root, "lead"))
        self.grant("lead", 8)
        st = self.s.state()
        self.assertEqual(network.assign(st, st.policy, T0 + 9)[root].member, "lead")

    def test_revoking_a_member_drops_its_events_and_leases(self):
        p = self.s.append("task", "human:ana", {"title": "fix", "paths": ["src"]}, T0 + 5).id
        lease = self.s.append("claim", "w", {"task_id": p}, T0 + 6)
        self.assertIn(lease.id, self.s.state().leases)
        self.grant("lead", 7)
        self.s.append("revoke", "lead", {"actor": "w", "reason": "spam"}, T0 + 8)
        st = self.s.state()
        self.assertFalse(st.leases[lease.id].active(T0 + 9))
        self.assertFalse(st.actors["w"].present(T0 + 9))
        self.assertIn("revoked", self.why(self.s.append("message", "w", {"text": "still here"}, T0 + 10)))
        self.assertIn("only the author revokes a steward",
                      self.why(self.s.append("revoke", "lead", {"actor": "lead"}, T0 + 11)))


if __name__ == "__main__":
    unittest.main()
