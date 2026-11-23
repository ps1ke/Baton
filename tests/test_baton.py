import json
import tempfile
import unittest
from pathlib import Path

from baton import events as ev
from baton import forge, paths
from baton import policy as pol
from baton.store import Store

from helpers import T0, offer, peer, machines


def tree(s: Store, t=T0):
    """Four machines: no roles, the network decides what each does."""
    machines(s, t, p=offer(), o=offer(), w1=offer(), w2=offer())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = peer(self.root, "a")
        tree(self.a)

    def tearDown(self):
        self.tmp.cleanup()


class PathTests(unittest.TestCase):
    def test_prefix_overlap_is_per_segment(self):
        self.assertTrue(paths.overlaps("tools", "tools/x.py"))
        self.assertTrue(paths.overlaps("./tools/", "tools"))
        self.assertFalse(paths.overlaps("tools", "toolsx/a.py"))
        self.assertTrue(paths.overlaps("", "anything/at/all"))
        with self.assertRaises(ValueError):
            paths.norm("../outside")


class EventTests(unittest.TestCase):
    def test_edited_event_is_refused(self):
        e = ev.make("join", "x", {"offer": {"slots": 1}}, [], 1, T0)
        d = e.to_dict()
        d["body"]["offer"]["slots"] = 9
        with self.assertRaises(ev.BadEvent):
            ev.from_dict(d)

    def test_same_content_same_id(self):
        a = ev.make("claim", "x", {"paths": ["a"]}, ["p2", "p1"], 3, T0)
        b = ev.make("claim", "x", {"paths": ["a"]}, ["p1", "p2"], 3, T0)
        self.assertEqual(a.id, b.id)


class MemberTests(Base):
    def test_a_member_is_a_machine_with_an_offer(self):
        s = self.a.state()
        self.assertEqual((s.actors["w1"].kind, s.actors["w1"].offer["models"][0]["id"]),
                         ("member", "claude:claude-sonnet-5-5"))
        self.assertTrue(s.actors["w1"].present(T0 + 100))

    def test_bad_offers_and_human_names_are_refused(self):
        bad = self.a.append("join", "x", {"offer": {"models": [{"id": "nocolon", "tier": "fast"}], "slots": 1}}, T0 + 10)
        self.assertIn("provider:model", self.a.state().ignored[bad.id])
        fake = self.a.append("join", "human:eve", {}, T0 + 11)
        self.assertIn("human:", self.a.state().ignored[fake.id])
        many = self.a.append("offer", "w1", {"models": [], "slots": 65}, T0 + 12)
        self.assertIn("slots", self.a.state().ignored[many.id])

    def test_an_offer_lapses_without_renewal(self):
        short = pol.DEFAULT.replace('humans = []', 'humans = ["human:ana"]')
        s = peer(self.root, "short", short)
        machines(s, T0, m=offer(ttl=60))
        st = s.state()
        self.assertTrue(st.actors["m"].present(T0 + 59))
        self.assertFalse(st.actors["m"].present(T0 + 61))
        late = s.append("claim", "m", {"task": "x", "paths": ["a"]}, T0 + 100)
        self.assertIn("present", s.state().denied[late.id])

    def test_unjoined_actor_does_nothing(self):
        e = self.a.append("claim", "ghost", {"task": "x", "paths": ["a"]}, T0 + 5)
        self.assertEqual(self.a.state().ignored[e.id], "actor has not joined")


class LeaseTests(Base):
    def test_overlap_is_denied_and_expiry_frees_the_path(self):
        first = self.a.append("claim", "w1", {"task": "fix cmake", "paths": ["build/CMakeLists.txt"], "ttl": 60}, T0 + 10)
        second = self.a.append("claim", "w2", {"task": "also cmake", "paths": ["runtime"]}, T0 + 20)
        s = self.a.state()
        self.assertIn(first.id, s.leases)
        self.assertIn("held by w1", s.denied[second.id])
        third = self.a.append("claim", "w2", {"task": "later", "paths": ["runtime"]}, T0 + 71)
        self.assertIn(third.id, self.a.state().leases)

    def test_topics_collide_too(self):
        self.a.append("claim", "w1", {"task": "x", "topics": ["BUG-1042"]}, T0 + 10)
        e = self.a.append("claim", "w2", {"task": "y", "topics": ["BUG-1042"]}, T0 + 11)
        self.assertIn(e.id, self.a.state().denied)

    def test_renew_extends_and_only_holder_or_a_human_releases(self):
        l = self.a.append("claim", "w1", {"task": "x", "paths": ["a"], "ttl": 60}, T0 + 10)
        self.a.append("renew", "w1", {"lease": l.id}, T0 + 50)
        self.assertTrue(self.a.state().leases[l.id].active(T0 + 100))
        nope = self.a.append("release", "w2", {"lease": l.id}, T0 + 60)
        self.assertIn("only the holder", self.a.state().ignored[nope.id])
        self.a.append("release", "human:ana", {"lease": l.id}, T0 + 61)
        self.assertFalse(self.a.state().leases[l.id].active(T0 + 62))

    def test_ttl_is_capped(self):
        l = self.a.append("claim", "w1", {"task": "x", "paths": ["a"], "ttl": 10**9}, T0 + 10)
        self.assertEqual(self.a.state().leases[l.id].ttl, pol.duration("8h"))

    def test_backdating_does_not_beat_a_seen_lease(self):
        self.a.append("claim", "w1", {"task": "x", "paths": ["a"]}, T0 + 100)
        late = self.a.append("claim", "w2", {"task": "y", "paths": ["a"]}, T0 - 5000)  # clock says earlier
        self.assertIn(late.id, self.a.state().denied)


class PeerTests(Base):
    def test_concurrent_claims_converge_on_one_winner(self):
        b = peer(self.root, "b")
        b.merge_from(self.a)
        # Both peers claim the same file without having seen each other's claim.
        ca = self.a.append("claim", "w1", {"task": "from a", "paths": ["build.py"]}, T0 + 10)
        cb = b.append("claim", "w2", {"task": "from b", "paths": ["build.py"]}, T0 + 10)
        self.assertIn(ca.id, self.a.state().leases)    # each thinks it won, locally
        self.assertIn(cb.id, b.state().leases)
        self.a.merge_from(b)
        b.merge_from(self.a)
        sa, sb = self.a.state(), b.state()
        winners_a = {l.id for l in sa.active_leases(T0 + 11)}
        winners_b = {l.id for l in sb.active_leases(T0 + 11)}
        self.assertEqual(winners_a, winners_b)
        self.assertEqual(len(winners_a), 1)
        self.assertEqual(set(sa.denied), set(sb.denied))

    def test_event_with_missing_parent_waits(self):
        b = peer(self.root, "b")
        b.merge_from(self.a)
        e1 = b.append("claim", "w1", {"task": "x", "paths": ["a"]}, T0 + 10)
        e2 = b.append("release", "w1", {"lease": e1.id}, T0 + 11)
        c = peer(self.root, "c")
        c.merge_from(self.a)
        c.write(e2)                      # the child arrives before its parent
        _, held, _ = c.load()
        self.assertIn(e2.id, held)
        c.write(e1)
        _, held, _ = c.load()
        self.assertNotIn(e2.id, held)

    def test_tampered_file_is_reported_not_folded(self):
        e = self.a.append("claim", "w1", {"task": "x", "paths": ["a"]}, T0 + 10)
        f = self.a.dir / f"{e.id}.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        d["body"]["paths"] = [""]
        f.write_text(json.dumps(d), encoding="utf-8")
        _, _, bad = self.a.load()
        self.assertIn(f.name, bad)
        self.assertNotIn(e.id, self.a.state().leases)


class GateTests(Base):
    def propose(self, actor, t, target="#1", body="hi", action="comment", paths_=None):
        payload = {"repo": "o/r", "body": body}
        if paths_:
            payload["paths"] = paths_
        return self.a.append("propose", actor, {"action": action, "target": target, "payload": payload}, t)

    def test_duplicates_and_budgets(self):
        p1 = self.propose("w1", T0 + 10)
        dup = self.propose("w2", T0 + 11)
        self.propose("w1", T0 + 12, target="#2")
        self.propose("w1", T0 + 13, target="#3")
        over = self.propose("w1", T0 + 14, target="#4")      # per actor 3/1h
        s = self.a.state()
        self.assertEqual(s.proposals[p1.id].status, "pending")
        self.assertEqual(s.proposals[dup.id].status, "duplicate")
        self.assertEqual(s.proposals[over.id].status, "over_budget")
        later = self.propose("w1", T0 + 3700, target="#5")    # the window has moved on
        self.assertEqual(self.a.state().proposals[later.id].status, "pending")

    def test_rejected_proposal_can_be_made_again(self):
        p1 = self.propose("w1", T0 + 10)
        self.a.append("reject", "human:ana", {"proposal": p1.id, "reason": "noise"}, T0 + 11)
        again = self.propose("w2", T0 + 12)
        self.assertEqual(self.a.state().proposals[again.id].status, "pending")

    def test_only_a_human_approves_and_never_their_own(self):
        p1 = self.propose("w1", T0 + 10)
        by_planner = self.a.append("approve", "p", {"proposal": p1.id}, T0 + 11)
        s = self.a.state()
        self.assertIn("for humans and approvers", s.ignored[by_planner.id])
        self.assertEqual(s.proposals[p1.id].status, "pending")
        self.a.append("approve", "human:ana", {"proposal": p1.id}, T0 + 12)
        self.assertEqual(self.a.state().proposals[p1.id].status, "approved")
        own = self.a.append("propose", "human:ana", {"action": "comment", "target": "#9",
                                                     "payload": {"repo": "o/r", "body": "x"}}, T0 + 13)
        selfie = self.a.append("approve", "human:ana", {"proposal": own.id}, T0 + 14)
        self.assertIn("own proposal", self.a.state().ignored[selfie.id])

    def test_executed_only_once_and_only_after_approval(self):
        p1 = self.propose("w1", T0 + 10)
        early = self.a.append("executed", "human:ana", {"proposal": p1.id}, T0 + 11)
        self.assertIn("not approved", self.a.state().ignored[early.id])
        self.a.append("approve", "human:ana", {"proposal": p1.id}, T0 + 12)
        self.a.append("executed", "human:ana", {"proposal": p1.id, "result": {"url": "u"}}, T0 + 13)
        twice = self.a.append("executed", "human:ana", {"proposal": p1.id}, T0 + 14)
        s = self.a.state()
        self.assertEqual(s.proposals[p1.id].status, "executed")
        self.assertIn(twice.id, s.ignored)

    def test_pr_needs_a_lease_on_its_paths(self):
        bare = self.propose("w1", T0 + 10, action="pr", target="", body="b")
        self.assertEqual(self.a.state().proposals[bare.id].status, "unleased")
        out = self.propose("w1", T0 + 11, action="pr", target="", body="b", paths_=["tools/a.py"])
        self.assertEqual(self.a.state().proposals[out.id].status, "unleased")
        self.a.append("claim", "w1", {"task": "a", "paths": ["tools"]}, T0 + 12)
        ok = self.propose("w1", T0 + 13, action="pr", target="", body="c", paths_=["tools/a.py"])
        self.assertEqual(self.a.state().proposals[ok.id].status, "pending")

    def test_unknown_action_is_refused(self):
        e = self.propose("w1", T0 + 10, action="merge")
        self.assertEqual(self.a.state().proposals[e.id].status, "unknown_action")


class ForgeTests(unittest.TestCase):
    def test_touching_finds_overlapping_pull_requests(self):
        pulls = [{"number": 1, "title": "a", "files": [{"path": "build/CMakeLists.txt"}], "author": {"login": "x"}},
                 {"number": 2, "title": "b", "files": [{"path": "docs/x.md"}], "author": {"login": "y"}}]
        self.assertEqual([p["number"] for p in forge.touching(pulls, ["runtime"])], [1])

    def test_execute_sends_the_body_on_stdin(self):
        seen = {}

        class R:
            returncode, stdout, stderr = 0, "https://x/1#c\n", ""

        def run(cmd, **kw):
            seen.update(cmd=cmd, input=kw["input"])
            return R()

        out = forge.execute("comment", "#12", {"repo": "o/r", "body": "hello `x`"}, run=run)
        self.assertEqual(out, {"url": "https://x/1#c"})
        self.assertEqual(seen["cmd"][:4], ["gh", "pr", "comment", "12"])
        self.assertEqual(seen["input"], "hello `x`")
        with self.assertRaises(NotImplementedError):
            forge.execute("merge", "#1", {}, run=run)


if __name__ == "__main__":
    unittest.main()
