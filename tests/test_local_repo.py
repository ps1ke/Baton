"""A project started from a folder on the author's machine: other machines get the code as a bundle, and the
finished work comes back into the folder as a baton/... branch."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from baton import app, codeflow, crypto
from baton.store import Store
from baton.workspace import Workspace

from helpers import POLICY


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class LocalRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.folder = r / "my project"
        self.folder.mkdir()
        git("init", "-q", "-b", "main", cwd=self.folder)
        (self.folder / "app.py").write_text("print('v1')\n")
        git("add", "-A", cwd=self.folder)
        git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "v1", cwd=self.folder)
        self.ws_a = Workspace(r / "A")
        self.ws_b = Workspace(r / "B")
        self.a = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_folder_becomes_a_project_and_its_code_travels_as_a_bundle(self):
        with self.assertRaises(ValueError):
            codeflow.as_repo_url(str(Path(self.tmp.name)))                    # not a repository
        url = codeflow.as_repo_url(str(self.folder))
        self.assertEqual(url, "file://" + str(self.folder.resolve()).replace(" ", "%20"))
        with mock.patch("baton.crypto.default_keyring", lambda: crypto.Keyring(self.ws_a.dir / "keys")):
            pid = app.create_project(self.ws_a, "human:ana", str(self.folder))
            a = Store(self.ws_a.projects()[pid]["home"])
            (a.home / "policy.toml").write_text(POLICY, encoding="utf-8")
            tid = app.post_package(self.ws_a, a, "human:ana", {"title": "say v2", "size": "S", "paths": "app.py"})
        t = a.state().tasks[tid]
        self.assertEqual(t.base, git("rev-parse", "HEAD", cwd=self.folder))
        self.assertIsNotNone(a.blob(t.bundle))
        # machine B has the log and the blobs, but not the folder
        b = Store(Path(self.tmp.name) / "B-log", keyring=crypto.Keyring(self.ws_b.dir / "keys"))
        b.init()
        b.merge_from(a)
        b.put_blob(a.blob(t.bundle))
        with mock.patch.object(codeflow, "local_path", lambda repo: None):
            repo_b = codeflow.checkout(self.ws_b, b)
            self.assertFalse(codeflow.have_commit(repo_b, t.base))
            s = b.state()
            where, base = codeflow.prepare(b, s, s.tasks[tid], repo_b, Path(self.tmp.name) / "wt")
        self.assertEqual((where / "app.py").read_text(), "print('v1')\n")    # the code arrived without the folder
        (where / "app.py").write_text("print('v2')\n")
        report = codeflow.capture(b, s.tasks[tid], where, base)
        codeflow.cleanup(repo_b, where)
        a.put_blob(b.blob(report["patch"]))
        # back on the author's machine, a steward pushes it into the folder as a branch
        repo_a = codeflow.checkout(self.ws_a, a)
        out = codeflow.push(a, repo_a, {"branch": f"baton/{tid[:12]}", "base": base, "patch": report["patch"],
                                        "title": "say v2", "package": tid})
        self.assertEqual(git("show", f"baton/{tid[:12]}:app.py", cwd=self.folder), "print('v2')")
        self.assertEqual((self.folder / "app.py").read_text(), "print('v1')\n")   # the checked-out branch is untouched
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.folder), "main")
        self.assertTrue(out["commit"])


if __name__ == "__main__":
    unittest.main()
