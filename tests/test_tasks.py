import json
import sys
import tempfile
import unittest
from pathlib import Path

from baton import policy as pol
from baton import runner
from baton.store import Store

T0 = 1_000_000.0


def agent(script: str) -> list[str]:
    """A stand-in agent: a Python one-liner, so the tests need no real model."""
    return [sys.executable, "-c", script]


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "log")
        self.s.init()
        self.set_runner(agent("print('ok')"))
        a = self.s.append
        a("join", "p", {"role": "planner", "parent": "human:ana"}, T0)
        a("join", "o", {"role": "orchestrator", "parent": "p"}, T0 + 1)
        a("join", "w1", {"role": "worker", "parent": "o"}, T0 + 2)
        a("join", "w2", {"role": "worker", "parent": "o"}, T0 + 3)

    def set_runner(self, cmd):
        text = pol.DEFAULT.replace('humans = []', 'humans = ["human:ana"]')
        text = text.replace('cmd = ["claude", "-p", "--model", "{model}", "--effort", "{effort}"]',
                            "cmd = " + json.dumps(cmd))
        (self.s.home / "policy.toml").write_text(text, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, title, t, **body):
        return self.s.append("task", "o", {"title": title, **body}, t)


class TaskStateTests(Board):
    def test_take_finish_and_lapse(self):
        t = self.task("fix parser", T0 + 10, paths=["src/parser"])
        st = self.s.state()
        self.assertEqual([x.id for x in st.open_tasks(T0 + 11)], [t.id])
        take = self.s.append("claim", "w1", {"task_id": t.id, "ttl": 60}, T0 + 12)
        st = self.s.state()
        self.assertEqual(st.task_status(st.tasks[t.id], T0 + 13), "taken")
        again = self.s.append("claim", "w2", {"task_id": t.id}, T0 + 14)
        self.assertIn("task is taken", self.s.state().denied[again.id])
        # the lease lapses with no `done`: the task is open again and someone else may take it
        st = self.s.state()
        self.assertEqual(st.task_status(st.tasks[t.id], T0 + 100), "open")
        late = self.s.append("done", "w1", {"task": t.id}, T0 + 101)
        self.assertIn("holding the task's lease", self.s.state().ignored[late.id])
        retake = self.s.append("claim", "w2", {"task_id": t.id}, T0 + 102)
        self.assertIn(retake.id, self.s.state().leases)
        self.s.append("done", "w2", {"task": t.id, "outcome": "done", "summary": "fixed"}, T0 + 103)
        st = self.s.state()
        self.assertEqual(st.task_status(st.tasks[t.id], T0 + 104), "done")
        self.assertFalse(st.leases[retake.id].active(T0 + 104))
        self.assertTrue(take.id in st.leases)

    def test_task_without_paths_is_still_exclusive(self):
        t = self.task("write release notes", T0 + 10)
        self.s.append("claim", "w1", {"task_id": t.id}, T0 + 11)
        e = self.s.append("claim", "w2", {"task_id": t.id}, T0 + 12)
        self.assertIn(e.id, self.s.state().denied)

    def test_role_restriction_and_who_may_post(self):
        t = self.task("plan the release", T0 + 10, role="orchestrator")
        e = self.s.append("claim", "w1", {"task_id": t.id}, T0 + 11)
        self.assertIn("for a orchestrator", self.s.state().denied[e.id])
        self.assertEqual(self.s.state().open_tasks(T0 + 12, "worker"), [])
        by_worker = self.s.append("task", "w1", {"title": "my own idea"}, T0 + 13)
        self.assertIn("cannot plan", self.s.state().ignored[by_worker.id])

    def test_split_task_waits_for_its_subtasks(self):
        big = self.task("port the build", T0 + 10, paths=["build"])
        a = self.task("linux part", T0 + 11, paths=["build/linux"], parent=big.id)
        self.task("windows part", T0 + 12, paths=["build/windows"], parent=big.id)
        st = self.s.state()
        self.assertEqual(st.tasks[big.id].subtasks, [a.id, st.tasks[big.id].subtasks[1]])
        self.assertNotIn(big.id, [x.id for x in st.open_tasks(T0 + 13)])

    def test_busy_paths_hide_a_task(self):
        self.s.append("claim", "w1", {"task": "ad hoc", "paths": ["src"]}, T0 + 10)
        self.task("fix parser", T0 + 11, paths=["src/parser"])
        self.assertEqual(self.s.state().open_tasks(T0 + 12), [])


class RunnerTests(Board):
    def test_exit_status_becomes_the_outcome(self):
        t1 = self.task("works", T0 + 10)
        self.assertEqual(runner.Run(self.s, "w1").go(log=lambda *_: None), "done")
        self.set_runner(agent("import sys; print('boom'); print('noise', file=sys.stderr); sys.exit(3)"))
        t2 = self.task("breaks", T0 + 11)
        self.assertEqual(runner.Run(self.s, "w1").go(log=lambda *_: None), "failed")
        st = self.s.state()
        self.assertEqual(st.tasks[t1.id].outcome, "done")
        self.assertEqual((st.tasks[t2.id].summary, st.tasks[t2.id].evidence["exit"]), ("boom", 3))

    def test_agent_sees_its_tier_and_can_report_itself(self):
        self.set_runner(agent(
            "import os, subprocess, sys; assert sys.argv[1:] == ['claude-haiku-5-5', 'low'];"
            "subprocess.run([sys.executable, '-m', 'baton', 'done', os.environ['BATON_TASK'],"
            " '--outcome', 'escalated', '--summary', 'needs a decision'], check=True)") + ["{model}", "{effort}"])
        t = self.task("ambiguous", T0 + 10)
        self.assertEqual(runner.Run(self.s, "w2").go(log=lambda *_: None), "escalated")
        self.assertEqual(self.s.state().tasks[t.id].summary, "needs a decision")

    def test_prompt_names_the_task_scope_and_the_gate(self):
        self.task("fix parser", T0 + 10, paths=["src/parser"], detail="the tokenizer drops tabs")
        self.set_runner(["agent", "--model", "{model}", "{prompt}"])
        cmd = runner.Run(self.s, "w1").command()
        self.assertEqual(cmd[:3], ["agent", "--model", "claude-haiku-5-5"])
        for needle in ("src/parser", "the tokenizer drops tabs", "-m baton propose", "-m baton done"):
            self.assertIn(needle, cmd[3])

    def test_prompt_goes_on_stdin_without_a_slot(self):
        self.set_runner(agent("import sys; data = sys.stdin.read(); sys.exit(0 if 'ship it' in data else 5)"))
        self.task("ship it", T0 + 10)
        self.assertEqual(runner.Run(self.s, "w1").go(log=lambda *_: None), "done")

    def test_no_task_is_an_error(self):
        with self.assertRaises(LookupError):
            runner.Run(self.s, "w1")


if __name__ == "__main__":
    unittest.main()
