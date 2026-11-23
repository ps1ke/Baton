import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from baton import swarm
from baton.workspace import Workspace

from helpers import agent, fake_probe, peer

# One stand-in agent for every kind of work, driven by its prompt, calling baton like a real agent would.
AGENT = r"""
import os, re, subprocess, sys
prompt = sys.stdin.read()
b = [sys.executable, '-m', 'baton']
def baton(*args):
    r = subprocess.run(b + list(args), capture_output=True, text=True)
    if r.returncode not in (0, 3):
        print('baton failed:', args, r.stdout, r.stderr, file=sys.stderr)
    return r.stdout
task = os.environ.get('BATON_TASK', '')
model = sys.argv[1] if len(sys.argv) > 1 else '?'
if 'asked questions' in prompt:
    for mid in re.findall(r'message id ([0-9a-f]{12})', prompt):
        baton('message', 'use ISO 8601', '--reply', mid)
elif 'All the children of this package have ended' in prompt:
    baton('done', task, '--summary', 'integrated')
elif 'You orchestrate' in prompt:
    baton('task', 'dates', '--parent', task, '--kind', 'implement', '--path', 'src/dates', '--check', 'unit=true')
    baton('task', 'ask about formats', '--parent', task, '--kind', 'implement', '--path', 'src/fmt', '--check', 'unit=true')
    baton('done', task, '--outcome', 'split', '--summary', 'two parts')
elif '(implement,' in prompt:
    said = ''
    if 'ask about formats' in prompt:
        said = baton('message', 'which format?', '--ask', '--wait', '60s').strip().splitlines()[-1]
    baton('done', task, '--summary', 'built ' + said.split('] ')[-1], '--change', 'src/x.py', '--check', 'unit=passed:true')
elif '(test,' in prompt:
    baton('done', task, '--summary', '1 passed', '--check', 'unit=passed:true')
elif '(review,' in prompt:
    baton('done', task, '--summary', 'reviewed with ' + model, '--verdict', 'passed')
"""

ALL = ["claude:claude-haiku-5-5", "claude:claude-sonnet-5-5", "claude:claude-opus-5-5", "claude:claude-fable-5-2"]


class Swarm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"BATON_WORKSPACE": str(self.root / "ws")})
        self.env.start()
        self.s = peer(self.root, "log")
        self.lines = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cfg(self, allowed, slots=2, name="m1"):
        return {**Workspace().machine(), "name": name, "allowed": allowed, "slots": slots,
                "commands": {"claude": agent(AGENT) + ["{model}"], "codex": agent(AGENT) + ["{model}"]}}

    def swarm(self, name, allowed, providers=("claude",), **kw):
        self.s.append("join", name, {})
        kw.setdefault("once", True)
        kw.setdefault("interval", 0.1)
        kw.setdefault("timeout", 90)
        return swarm.Swarm(self.s, name, self.cfg(allowed, name=name), fake_probe(*providers),
                           transcripts=self.root / "runs" / name, log=self.lines.append, **kw)

    def root_package(self):
        return self.s.append("task", "human:ana", {"title": "date handling", "size": "L", "paths": ["src"],
                                                   "goal": "dates parse and print"}).id

    def test_one_machine_does_every_level_and_check_and_answers_its_own_workers(self):
        root = self.root_package()
        results = self.swarm("m1", ALL).run()
        s = self.s.state()
        self.assertEqual(s.tasks[root].outcome, "done", "\n".join(self.lines))
        self.assertEqual(s.tasks[root].summary, "integrated")
        kids = [s.tasks[c] for c in s.tasks[root].subtasks]
        self.assertTrue(all(k.outcome == "done" and len(k.checks) == 2 for k in kids), [(k.title, k.outcome) for k in kids])
        asker = next(k for k in kids if k.title == "ask about formats")
        self.assertEqual(asker.summary, "built use ISO 8601")                 # asked, was answered, used it
        self.assertEqual(s.tasks[root].runs[0]["model"], "claude:claude-fable-5-2")   # level 0: frontier
        self.assertEqual(kids[0].runs[0]["model"], "claude:claude-sonnet-5-5")        # implement: standard
        self.assertEqual(s.tasks[kids[0].checks[0]].runs[0]["model"], "claude:claude-haiku-5-5")   # test: fast
        kinds = sorted(k for k, _, _ in results)
        self.assertIn("answer", kinds)
        self.assertTrue(any(f.name.endswith("orchestrate.log") for f in (self.root / "runs" / "m1").iterdir()))

    def test_two_machines_split_the_work_and_reviews_cross_families(self):
        root = self.root_package()
        m1 = self.swarm("m1", ALL)
        m2 = self.swarm("m2", ["codex:gpt-6.1-sol", "codex:gpt-6.1-luna"], providers=("codex",))
        out = {}
        threads = [threading.Thread(target=lambda sw=sw: out.setdefault(sw.me, sw.run())) for sw in (m1, m2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        s = self.s.state()
        self.assertEqual(s.tasks[root].outcome, "done", "\n".join(self.lines))
        reviews = [t for t in s.tasks.values() if t.kind == "review"]
        self.assertEqual(len(reviews), 2)
        for r in reviews:
            writer = s.tasks[r.of].runs[-1]["model"].split(":")[0]
            self.assertNotEqual(r.runs[-1]["model"].split(":")[0], writer, r.summary)   # another family reviewed
        self.assertTrue(out["m1"] and out["m2"])                                       # both did work

    def test_max_runs_stops_it_and_a_machine_without_models_is_refused(self):
        for i in range(4):
            self.s.append("task", "human:ana", {"title": f"t{i}", "paths": [f"p{i}"], "kind": "investigate"})
        results = self.swarm("m1", ALL, max_runs=2, once=False).run()
        self.assertEqual(len(results), 2)
        self.assertTrue(any("reached 2 runs" in l for l in self.lines))
        with self.assertRaises(ValueError):
            self.swarm("m3", [])
        with self.assertRaises(ValueError):
            swarm.Swarm(self.s, "human:ana", self.cfg(ALL), fake_probe())


if __name__ == "__main__":
    unittest.main()
