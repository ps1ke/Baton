import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from baton import policy as pol
from baton import swarm
from baton.store import Store

T0 = 1_000_000.0
BATON = f"import subprocess, sys, json\nb = [sys.executable, '-m', 'baton']\n"

# A worker that asks its orchestrator which file to change, waits for the answer and reports it.
ASKS = BATON + """r = subprocess.run(b + ['message', 'which file?', '--wait', '60s'], capture_output=True, text=True)
answer = r.stdout.strip().splitlines()[-1]
subprocess.run(b + ['done', __import__('os').environ['BATON_TASK'], '--summary', 'used ' + answer.split('] ')[-1]])
"""
# A worker that gives up on anything it does not like.
ESCALATES = BATON + """import os
task = os.environ['BATON_TASK']
out = subprocess.run(b + ['thread', task], capture_output=True, text=True).stdout
if 'too big' in out:
    subprocess.run(b + ['done', task, '--outcome', 'escalated', '--summary', 'needs splitting'])
else:
    print('fixed it')
"""
# An orchestrator that answers every question and splits every escalated task in two.
ORCH = BATON + """import re
prompt = sys.stdin.read()
for m in json.loads(subprocess.run(b + ['inbox', '--json'], capture_output=True, text=True).stdout):
    subprocess.run(b + ['message', 'parser.py', '--reply', m['id']])
for tid in re.findall(r'task ([0-9a-f]{12}) .* ended escalated', prompt):
    for part in ('half one', 'half two'):
        subprocess.run(b + ['task', part, '--parent', tid, '--role', 'worker'])
"""


def policy(worker: str, orch: str) -> str:
    text = pol.DEFAULT.replace('humans = []', 'humans = ["human:ana"]')
    text = text.replace('can = ["claim", "propose"]',
                        'can = ["claim", "propose"]\nrunner = ' + json.dumps([sys.executable, "-c", worker]))
    text = text.replace('can = ["plan", "claim", "propose", "admit"]',
                        'can = ["plan", "claim", "propose", "admit"]\nrunner = ' + json.dumps([sys.executable, "-c", orch]))
    return text


class Swarm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BATON_WORKSPACE": str(Path(self.tmp.name) / "ws")})
        self.env.start()
        self.s = Store(Path(self.tmp.name) / "log")
        self.s.init()
        a = self.s.append
        a("join", "p", {"role": "planner", "parent": "human:ana"}, T0)
        a("join", "o", {"role": "orchestrator", "parent": "p"}, T0 + 1)
        a("join", "w1", {"role": "worker", "parent": "o"}, T0 + 2)
        a("join", "w2", {"role": "worker", "parent": "o"}, T0 + 3)
        self.lines = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def make(self, worker, orch=ORCH, **kw):
        (self.s.home / "policy.toml").write_text(policy(worker, orch), encoding="utf-8")
        kw.setdefault("once", True)
        kw.setdefault("interval", 0.1)
        kw.setdefault("timeout", 60)
        return swarm.Swarm(self.s, kw.pop("seats", ["o", "w1", "w2"]), transcripts=Path(self.tmp.name) / "runs",
                           log=self.lines.append, **kw)

    def test_plan_attends_first_and_keeps_planners_off_chores(self):
        sw = self.make("print(1)")
        chore = self.s.append("task", "o", {"title": "chore"}, T0 + 5).id
        q = self.s.append("message", "w1", {"text": "?", "to": "o"}, T0 + 6)
        jobs = sw.plan(self.s.state(), T0 + 7)
        kinds = {j.seat: (j.kind, j.task.id if j.task else [i["id"] for i in j.items]) for j in jobs}
        self.assertEqual(kinds["o"], ("attend", [q.id]))
        self.assertEqual(kinds["w1"], ("work", chore))
        self.assertNotIn("w2", kinds)                        # one task, one taker
        sw2 = self.make("print(1)", seats=["p"])
        self.assertEqual(sw2.plan(self.s.state(), T0 + 7), [])   # the planner leaves the unrestricted chore alone

    def test_parallel_and_attempt_limits(self):
        sw = self.make("print(1)", parallel=1, max_attempts=1)
        a = self.s.append("task", "o", {"title": "a", "paths": ["a"]}, T0 + 5).id
        self.s.append("task", "o", {"title": "b", "paths": ["b"]}, T0 + 6)
        self.assertEqual(len([j for j in sw.plan(self.s.state(), T0 + 7) if j.kind == "work"]), 1)
        sw.attempts[a] = 1
        self.assertEqual([j.task.title for j in sw.plan(self.s.state(), T0 + 7)], ["b"])

    def test_a_worker_asks_and_the_orchestrator_answers(self):
        tid = self.s.append("task", "o", {"title": "fix the parser", "paths": ["src"]}).id
        results = self.make(ASKS).run()
        s = self.s.state()
        self.assertEqual(s.tasks[tid].outcome, "done", self.lines)
        self.assertEqual(s.tasks[tid].summary, "used parser.py")
        self.assertEqual([m.text for m in s.thread(tid)], ["which file?", "parser.py"])
        self.assertIn(("o", "attend", "done"), results)
        self.assertTrue(any(f.name.endswith("attend.log") for f in (Path(self.tmp.name) / "runs").iterdir()))

    def test_escalated_work_is_split_and_finished(self):
        big = self.s.append("task", "o", {"title": "rewrite everything", "detail": "too big", "role": "worker"}).id
        self.s.append("message", "o", {"text": "too big for one run", "task": big})
        results = self.make(ESCALATES).run()
        s = self.s.state()
        self.assertEqual(s.tasks[big].outcome, "escalated", self.lines)
        halves = [s.tasks[c] for c in s.tasks[big].subtasks]
        self.assertEqual(sorted(t.title for t in halves), ["half one", "half two"])
        self.assertTrue(all(t.outcome == "done" for t in halves), self.lines)
        self.assertEqual(swarm.attention(s, "o"), [])            # nothing left for o
        self.assertEqual(sum(1 for r in results if r[1] == "work"), 3)

    def test_max_runs_stops_the_swarm(self):
        for i in range(5):
            self.s.append("task", "o", {"title": f"t{i}", "paths": [f"p{i}"]})
        results = self.make("print('ok')", max_runs=3, once=False).run()
        self.assertEqual(len(results), 3)
        self.assertTrue(any("reached 3 runs" in l for l in self.lines))
        self.assertEqual(len(self.s.state().open_tasks(10**12)), 2)

    def test_unknown_or_human_seats_are_refused(self):
        with self.assertRaises(KeyError):
            self.make("print(1)", seats=["ghost"])
        (self.s.home / "policy.toml").write_text(policy("print(1)", ORCH), encoding="utf-8")
        with self.assertRaises(ValueError):
            swarm.Swarm(self.s, ["human:ana"])


if __name__ == "__main__":
    unittest.main()
