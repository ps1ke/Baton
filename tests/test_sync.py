import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from baton import sync
from baton.store import Store

T0 = 1_000_000.0


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "needs git")
class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(self.remote))
        self.peers = []
        for name in ("a", "b"):
            s = Store(root / name)
            s.init()
            git("init", "-q", "-b", "main", cwd=s.home)
            git("remote", "add", "origin", str(self.remote), cwd=s.home)
            self.peers.append(s)
        self.a, self.b = self.peers

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_peers_converge_through_the_syncer(self):
        self.a.append("join", "p", {"role": "planner", "parent": "human:ana"}, T0)
        self.b.append("join", "q", {"role": "planner", "parent": "human:ana"}, T0)
        lines = []
        syncer = sync.Syncer([self.a.home, self.b.home], every=1, log=lines.append)
        first = syncer.tick(now=T0)
        self.assertTrue(all(st["pulled"] and st["pushed"] for st in first.values()), first)
        self.assertEqual(syncer.tick(now=T0 + 0.5), {})       # not due yet
        syncer.tick(now=T0 + 2)                                 # a now pulls what b pushed
        self.assertEqual(set(self.a.read()[0]), set(self.b.read()[0]))
        self.assertEqual(len(self.a.read()[0]), 2)
        self.assertTrue(any("new event" in l for l in lines), lines)
        self.assertEqual(sync.status_of(self.a.home)["error"], None)
        tracked = subprocess.run(["git", "ls-files"], cwd=self.a.home, capture_output=True, text=True).stdout
        self.assertIn("events/.gitignore", tracked)

    def test_concurrent_pushes_retry_and_nothing_is_lost(self):
        for i in range(5):
            self.a.append("join", f"a{i}", {"role": "planner", "parent": "human:ana"}, T0 + i)
            self.b.append("join", f"b{i}", {"role": "planner", "parent": "human:ana"}, T0 + i)
        results = []
        ts = [threading.Thread(target=lambda s=s: results.append(sync.sync_once(s))) for s in self.peers]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertTrue(all(r["pushed"] for r in results), results)
        for s in self.peers:
            sync.sync_once(s)
        self.assertEqual(len(self.a.read()[0]), 10)
        self.assertEqual(set(self.a.read()[0]), set(self.b.read()[0]))

    def test_a_read_only_remote_still_pulls_and_does_not_back_off(self):
        self.b.append("join", "q", {"role": "planner", "parent": "human:ana"}, T0)
        sync.sync_once(self.b)
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'read only' >&2\nexit 1\n")
        hook.chmod(0o755)
        self.a.append("join", "p", {"role": "planner", "parent": "human:ana"}, T0)
        syncer = sync.Syncer([self.a.home], every=5)
        st = syncer.tick(now=T0)[str(self.a.home.resolve())]
        self.assertTrue(st["pulled"])
        self.assertFalse(st["pushed"])
        self.assertIn("read only", st["error"])
        self.assertEqual(len(self.a.read()[0]), 2)               # b's event arrived anyway
        self.assertEqual(syncer._next[str(self.a.home.resolve())], T0 + 5)

    def test_an_unreachable_remote_backs_off(self):
        git("remote", "set-url", "origin", str(Path(self.tmp.name) / "gone.git"), cwd=self.a.home)
        syncer = sync.Syncer([self.a.home], every=5)
        key = str(self.a.home.resolve())
        self.assertFalse(syncer.tick(now=T0)[key]["pulled"])
        self.assertEqual(syncer._next[key], T0 + 10)
        syncer.tick(now=T0 + 10)
        self.assertEqual(syncer._next[key], T0 + 30)

    def test_logs_without_a_remote_are_skipped(self):
        lone = Store(Path(self.tmp.name) / "lone")
        lone.init()
        self.assertEqual(sync.Syncer([lone.home]).tick(), {})


if __name__ == "__main__":
    unittest.main()
