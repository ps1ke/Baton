import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from baton import network, runner, wp
from baton.workspace import Workspace

from helpers import FABLE, HAIKU, OPUS, POLICY, SOL, SONNET, T0, agent, fake_probe, machine_cfg, machines, offer, peer


class Net(unittest.TestCase):
    policy = POLICY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = peer(self.root, "log", self.policy)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, t, title="fix parser", by="human:ana", **body):
        return self.s.append("task", by, {"title": title, **body}, T0 + t).id

    def picks(self, t):
        st = self.s.state()
        return {k: (v.member, v.model) for k, v in network.assign(st, st.policy, T0 + t).items()}

    def claim(self, who, pkg, t, **kw):
        e = self.s.append("claim", who, {"task_id": pkg, **kw}, T0 + t)
        st = self.s.state()
        return st.leases.get(e.id), st.denied.get(e.id) or st.ignored.get(e.id)

    def done(self, who, pkg, t, outcome="done", **report):
        body = {"task": pkg, "outcome": outcome, "summary": report.pop("summary", outcome)}
        if report:
            body["report"] = report
        e = self.s.append("done", who, body, T0 + t)
        return self.s.state().ignored.get(e.id)

    def status(self, pkg, t):
        st = self.s.state()
        return st.task_status(st.tasks[pkg], T0 + t)


class Assignment(Net):
    def test_the_cheapest_good_enough_model_and_the_least_loaded_machine(self):
        machines(self.s, T0, big=offer(OPUS, SONNET, slots=1), small=offer(HAIKU, slots=4))
        chore = self.post(10, "rename a variable", needs={"tier": "fast"}, kind="investigate", paths=["a"])
        work = self.post(11, "fix parser", paths=["b"])                         # implement: standard
        self.assertEqual(self.picks(12), {chore: ("small", "claude:claude-haiku-5-5"),
                                          work: ("big", "claude:claude-sonnet-5-5")})
        hard = self.post(13, "redesign storage", kind="implement", needs={"tier": "strong"}, paths=["c"])
        self.claim("big", work, 14)
        self.assertNotIn(hard, self.picks(15))                                  # big's one slot is busy

    def test_hardware_needs_are_matched(self):
        machines(self.s, T0, cpu=offer(SONNET), gpu=offer(SONNET, tags=["gpu", "gpu:nvidia"]))
        p = self.post(10, "train the model", needs={"hardware": ["gpu"]})
        self.assertEqual(self.picks(11)[p][0], "gpu")

    def test_only_the_assigned_machine_may_claim_it(self):
        machines(self.s, T0, a=offer(SONNET), b=offer(HAIKU))
        p = self.post(10)
        lease, why = self.claim("b", p, 11, model="claude:claude-haiku-5-5")
        self.assertIsNone(lease)
        self.assertIn("needs a standard model", why)
        lease, why = self.claim("a", p, 12, model="claude:claude-haiku-5-5")
        self.assertIn("standard or better", why)                                # a cheaper model is refused too
        lease, why = self.claim("a", p, 13)
        self.assertEqual((lease.actor, lease.model), ("a", "claude:claude-sonnet-5-5"))

    def test_absent_machines_get_nothing_and_humans_may_take_anything(self):
        machines(self.s, T0, gone=offer(SONNET, ttl=30))
        p = self.post(100)
        self.assertEqual(self.picks(101), {})
        lease, why = self.claim("gone", p, 102)
        self.assertIn("not present", why)
        lease, _ = self.claim("human:ana", p, 103)
        self.assertIsNotNone(lease)

    def test_a_package_nobody_can_serve_steps_down_a_tier_after_waiting(self):
        machines(self.s, T0, m=offer(SONNET, HAIKU))
        root = self.post(10, "build the importer", size="L")                   # orchestrate, level 0: frontier
        st = self.s.state()
        self.assertEqual((st.tasks[root].kind, st.tasks[root].tier), ("orchestrate", "frontier"))
        self.assertEqual(self.picks(20), {})
        self.assertEqual(self.picks(10 + 600 + 1), {})                          # strong: still nobody
        self.assertEqual(self.picks(10 + 1200 + 1), {root: ("m", "claude:claude-sonnet-5-5")})

    def test_the_full_tier_is_kept_while_anyone_present_offers_it(self):
        machines(self.s, T0, top=offer(FABLE, slots=1), mid=offer(SONNET))
        root = self.post(10, "plan", size="XL")
        other = self.post(11, "plan more", size="XL")
        self.claim("top", root, 12)
        self.assertNotIn(other, self.picks(10 + 3000))                          # waits for top, does not step down


class Levels(Net):
    def setUp(self):
        super().setUp()
        machines(self.s, T0, o=offer(FABLE, OPUS, SONNET, slots=3), w=offer(SONNET, HAIKU, slots=3))
        self.root = self.post(10, "port the build", size="L", paths=["build"])
        lease, why = self.claim("o", self.root, 11)
        self.assertIsNotNone(lease, why)

    def test_children_are_one_level_down_and_inside_the_parent(self):
        child = self.post(12, "linux part", by="o", parent=self.root, paths=["build/linux"], kind="orchestrate")
        st = self.s.state()
        self.assertEqual((st.tasks[child].level, st.tasks[child].tier), (1, "strong"))
        outside = self.s.append("task", "o", {"title": "x", "parent": self.root, "paths": ["src"]}, T0 + 13)
        self.assertIn("inside its parent", self.s.state().ignored[outside.id])
        stray = self.s.append("task", "w", {"title": "my own idea", "parent": self.root}, T0 + 14)
        self.assertIn("holding the parent", self.s.state().ignored[stray.id])
        root_by_member = self.s.append("task", "o", {"title": "new root"}, T0 + 15)
        self.assertIn("children of an orchestrate", self.s.state().ignored[root_by_member.id])

    def test_orchestration_stops_at_max_depth(self):
        l1 = self.post(12, "l1", by="o", parent=self.root, kind="orchestrate", paths=["build/a"])
        self.done("o", self.root, 13, "split")
        who1 = self.picks(14)[l1][0]
        self.claim(who1, l1, 14)
        l2 = self.post(15, "l2", by=who1, parent=l1, kind="orchestrate", paths=["build/a/b"])
        self.done(who1, l1, 16, "split")
        who2 = self.picks(17)[l2][0]
        self.claim(who2, l2, 17)
        l3 = self.s.append("task", who2, {"title": "l3", "parent": l2, "kind": "orchestrate", "paths": ["build/a/b"]}, T0 + 18)
        self.assertIn("deepest orchestration level", self.s.state().ignored[l3.id])
        ok = self.s.append("task", who2, {"title": "l3 work", "parent": l2, "kind": "implement", "paths": ["build/a/b"]}, T0 + 19)
        self.assertEqual(self.s.state().tasks[ok.id].level, 3)

    def test_fan_out_is_capped(self):
        for i in range(9):
            self.s.append("task", "o", {"title": f"c{i}", "parent": self.root, "paths": [f"build/{i}"]}, T0 + 12 + i)
        st = self.s.state()
        self.assertEqual(len(st.tasks[self.root].subtasks), 8)

    def test_split_waits_and_comes_back_to_integrate(self):
        self.assertIn("children are running", self.done("o", self.root, 12, "split"))
        a = self.post(13, "a", by="o", parent=self.root, paths=["build/a"], kind="investigate")
        b = self.post(14, "b", by="o", parent=self.root, paths=["build/b"], kind="investigate",
                      depends_on=[a])
        self.assertIsNone(self.done("o", self.root, 15, "split"))
        self.assertEqual(self.status(self.root, 16), "waiting")
        self.assertEqual(self.status(b, 16), "waiting")                         # depends on a
        self.claim("w", a, 17)
        self.done("w", a, 18, summary="found it")
        self.assertEqual(self.status(b, 19), "open")
        self.claim("w", b, 20)
        self.done("w", b, 21)
        self.assertEqual(self.status(self.root, 22), "open")                    # integrate
        self.assertIn(self.root, self.picks(22))
        st = self.s.state()
        prompt = runner.prompt_for(st, st.tasks[self.root], "o", st.policy)
        self.assertIn("All the children of this package have ended", prompt)
        self.assertIn("found it", prompt)
        self.claim(self.picks(23)[self.root][0], self.root, 23)
        holder = self.s.state().leases[self.s.state().tasks[self.root].lease].actor
        self.assertIsNone(self.done(holder, self.root, 24, summary="ported"))
        self.assertEqual(self.status(self.root, 25), "done")


class Pipeline(Net):
    def setUp(self):
        super().setUp()
        machines(self.s, T0, dev=offer(SONNET, HAIKU, OPUS, slots=2), rev=offer(SOL, HAIKU, slots=2))
        self.impl = self.post(10, "parse dates", paths=["src"],
                              acceptance=[{"check": "unit", "run": "python3 -m unittest"}])
        lease, why = self.claim("dev", self.impl, 11)
        self.assertIsNotNone(lease, why)

    def checks(self):
        return self.s.state().tasks[self.impl].checks

    def test_tested_then_reviewed_by_another_family_then_done(self):
        self.done("dev", self.impl, 12, changes=[{"path": "src/dates.py"}])
        self.assertEqual(self.status(self.impl, 13), "checking")
        test = self.checks()[0]
        st = self.s.state()
        self.assertEqual((st.tasks[test].kind, st.tasks[test].tier, st.tasks[test].of), ("test", "fast", self.impl))
        self.assertEqual(self.picks(13)[test][0], "rev")                          # someone else tests it
        self.claim("rev", test, 14)
        self.done("rev", test, 15, verification={"status": "passed", "checks": [{"name": "unit", "status": "passed"}]})
        review = self.checks()[1]
        self.assertEqual(self.picks(16)[review], ("rev", "codex:gpt-6.1-sol"))     # strong, and not Claude
        self.claim("rev", review, 17)
        self.done("rev", review, 18, verdict="passed", findings=[{"severity": "low", "text": "naming"}])
        self.assertEqual(self.status(self.impl, 19), "done")

    def test_failed_checks_send_it_back_with_findings_then_escalate(self):
        for round_ in range(3):
            t = 100 * round_ + 20
            if round_:
                lease, why = self.claim("dev", self.impl, t)
                self.assertIsNotNone(lease, why)
            self.done("dev", self.impl, t + 1)
            test = self.checks()[-1]
            self.claim(self.picks(t + 2)[test][0], test, t + 2)
            holder = self.s.state().leases[self.s.state().tasks[test].lease].actor
            self.done(holder, test, t + 3, summary="1 failing",
                      verification={"status": "failed", "checks": [{"name": "unit", "status": "failed",
                                                                    "command": "python3 -m unittest"}]})
        st = self.s.state()
        impl = st.tasks[self.impl]
        self.assertEqual((impl.rework, impl.outcome), (3, "escalated"))
        self.assertIn("check unit: failed", impl.feedback[0])
        self.assertIn("came back from its checks", runner.prompt_for(st, impl, "dev", st.policy))

    def test_not_run_is_not_a_pass_and_an_escalated_check_escalates(self):
        self.done("dev", self.impl, 12)
        test = self.checks()[0]
        self.claim("rev", test, 13)
        self.done("rev", test, 14, verification={"status": "not_run"})
        self.assertEqual(self.s.state().tasks[self.impl].rework, 1)
        self.claim("dev", self.impl, 15)
        self.done("dev", self.impl, 16)
        test2 = self.checks()[-1]
        self.claim(self.picks(17)[test2][0], test2, 17)
        holder = self.s.state().leases[self.s.state().tasks[test2].lease].actor
        self.done(holder, test2, 18, "escalated", summary="no python here")
        self.assertEqual(self.status(self.impl, 19), "escalated")

    def test_a_blocking_finding_fails_a_review_that_says_passed(self):
        self.assertFalse(wp.review_passed({"verdict": "passed", "findings": [{"severity": "high", "text": "x"}]}))
        bad = self.done("dev", self.impl, 12, verification={"status": "passed",
                                                            "checks": [{"name": "u", "status": "failed"}]})
        self.assertIn("cannot pass", bad)

    def test_runs_without_reports_stall_the_package(self):
        for i in range(3):
            self.s.append("release", "dev", {"lease": self.s.state().tasks[self.impl].lease}, T0 + 20 + i * 10)
            if i < 2:
                lease, why = self.claim("dev", self.impl, 25 + i * 10)
                self.assertIsNotNone(lease, why)
        self.assertEqual(self.status(self.impl, 60), "stalled")
        self.assertNotIn(self.impl, self.picks(61))


class Runner(Net):
    """Agents are real subprocesses that call baton with the wall clock, so these tests live in real time."""

    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"BATON_WORKSPACE": str(self.root / "ws")})
        self.env.start()
        self.ws = Workspace()
        self.now = time.time()
        machines(self.s, self.now - 5, m=offer(SONNET, HAIKU))
        self.probe = fake_probe("claude")

    def post(self, t, title="fix parser", by="human:ana", **body):
        return self.s.append("task", by, {"title": title, **body}, self.now - 4).id

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def run_with(self, script, pkg, extra=()):
        machine_cfg(self.ws, agent(script) + list(extra), ["claude:claude-sonnet-5-5", "claude:claude-haiku-5-5"])
        st = self.s.state()
        pick = network.assign(st, st.policy, time.time())[pkg]
        r = runner.Run(self.s, "m", pkg, pick.model, self.probe, self.ws.machine())
        return r, r.go(log=lambda *_: None)

    def test_an_unreported_implement_counts_by_exit_status_and_enters_its_checks(self):
        p = self.post(10, "works")
        r, out = self.run_with("print('ok')", p)
        self.assertEqual(out, "checking")
        st = self.s.state()
        self.assertEqual(st.tasks[p].summary, "ok")
        self.assertFalse(st.tasks[p].evidence["reported"])

    def test_an_unreported_check_is_given_back(self):
        p = self.post(10, "works")
        self.run_with("print('ok')", p)
        test = self.s.state().tasks[p].checks[0]
        r, out = self.run_with("print('forgot to report')", test)
        self.assertEqual(out, "open")
        self.assertEqual(len(self.s.state().tasks[test].runs), 1)

    def test_the_agent_gets_model_effort_and_the_package_and_reports_itself(self):
        p = self.post(10, "investigate the crash", kind="investigate", goal="find why it crashes", paths=["src"])
        r, out = self.run_with(
            "import os, subprocess, sys; data = sys.stdin.read();"
            "assert sys.argv[1:] == ['claude-sonnet-5-5', 'medium'] or sys.argv[1:] == ['claude-haiku-5-5', 'low'], sys.argv;"
            "assert 'find why it crashes' in data and 'Do not change any file' in data;"
            "subprocess.run([sys.executable, '-m', 'baton', 'done', os.environ['BATON_TASK'], '--summary', 'null deref',"
            " '--finding', 'info:x is None@src/a.py:3'], check=True)", p, ["{model}", "{effort}"])
        self.assertEqual(out, "done")
        rep = self.s.state().tasks[p].report
        self.assertEqual(rep["findings"], [{"severity": "info", "text": "x is None", "path": "src/a.py", "line": 3}])

    def test_a_model_the_user_did_not_allow_cannot_run(self):
        p = self.post(10)
        machine_cfg(self.ws, agent("print(1)"), ["claude:claude-haiku-5-5"])
        with self.assertRaises(PermissionError):
            runner.Run(self.s, "m", p, "claude:claude-sonnet-5-5", self.probe, self.ws.machine())


if __name__ == "__main__":
    unittest.main()
