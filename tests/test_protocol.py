import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from baton import manifest as mf
from baton import policy as pol
from baton import runner
from baton.store import Store
from baton.workspace import Workspace, manifest_of, publish

from helpers import POLICY, SONNET, T0, offer


def make_project(root: Path, name="toy", **fields) -> Store:
    s = Store(root)
    s.init()
    (s.home / "policy.toml").write_text(POLICY, encoding="utf-8")
    s.append("project", "human:ana", {"name": name, "repo": "https://example.org/toy.git", "summary": "a toy", "tags": ["python"],
                                     "needs": [{"tier": "standard", "skills": ["python"], "count": 2}], **fields}, T0)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"BATON_WORKSPACE": str(self.root / "ws")})
        self.env.start()
        self.p = make_project(self.root / "toy")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class ProjectTests(Base):
    def test_genesis_is_the_id_and_the_manifest_validates(self):
        s = self.p.state()
        first = min(self.p.read()[0].values(), key=lambda e: e.lamport)
        self.assertEqual(s.project_id, first.id)
        m = manifest_of(self.p, now=T0 + 5)
        self.assertEqual(mf.check_manifest(m), [])
        self.assertEqual((m["join"], m["humans"], m["activity"]["agents"]), ("apply", ["human:ana"], 0))

    def test_updates_merge_and_only_governors_change_it(self):
        self.p.append("project", "human:ana", {"summary": "better", "join": "open", "tags": None}, T0 + 1)
        s = self.p.state()
        self.assertEqual((s.project["summary"], s.project["join"], "tags" in s.project), ("better", "open", False))
        self.p.append("join", "plan", {"offer": offer()}, T0 + 2)
        e = self.p.append("project", "plan", {"name": "mine now"}, T0 + 3)
        self.assertIn("level-0 decision", self.p.state().ignored[e.id])
        bad = self.p.append("project", "human:ana", {"homepage": "javascript:alert(1)"}, T0 + 4)
        self.assertIn("http(s) URL", self.p.state().ignored[bad.id])

    def test_links_between_projects(self):
        other = make_project(self.root / "other", name="other").state().project_id
        self.p.append("project", "human:ana", {"links": [{"rel": "depends-on", "project": other, "name": "other"}]}, T0 + 1)
        self.assertEqual(manifest_of(self.p)["links"][0]["project"], other)
        e = self.p.append("project", "human:ana", {"links": [{"rel": "likes", "project": other}]}, T0 + 2)
        self.assertIn("rel must be one of", self.p.state().ignored[e.id])


class JoiningTests(Base):
    def setUp(self):
        super().setUp()
        a = self.p.append
        a("project", "human:ana", {"join": "open"}, T0 + 1)
        a("join", "plan", {"offer": offer()}, T0 + 2)
        a("join", "orch", {"offer": offer()}, T0 + 3)
        a("project", "human:ana", {"join": "apply"}, T0 + 4)

    def test_apply_mode_refuses_self_join(self):
        e = self.p.append("join", "w9", {"offer": offer()}, T0 + 10)
        self.assertIn("by application", self.p.state().ignored[e.id])

    def test_apply_with_a_machine_then_a_human_admits_it(self):
        self.p.append("apply", "w9", {"offer": offer(SONNET, slots=3), "note": "gpu box"}, T0 + 10)
        dup = self.p.append("apply", "w9", {}, T0 + 11)
        self.assertIn("already pending", self.p.state().ignored[dup.id])
        self.p.append("admit", "human:ana", {"applicant": "w9"}, T0 + 12)
        s = self.p.state()
        self.assertEqual((s.actors["w9"].kind, s.actors["w9"].offer["slots"]), ("member", 3))
        self.assertTrue(s.actors["w9"].present(T0 + 13))           # its offer counts from admission
        self.assertEqual([a.status for a in s.applications.values()], ["admitted"])

    def test_members_do_not_admit_unless_they_are_approvers(self):
        self.p.append("apply", "w9", {"offer": offer()}, T0 + 10)
        e = self.p.append("admit", "orch", {"applicant": "w9"}, T0 + 11)
        self.assertIn("level-0 decision", self.p.state().ignored[e.id])
        (self.p.home / "policy.toml").write_text(POLICY.replace('approvers = []', 'approvers = ["orch"]'), encoding="utf-8")
        self.assertIn("w9", self.p.state().actors)   # the policy is folded over all events (a known gap: section 10)

    def test_decline_and_closed(self):
        self.p.append("apply", "w9", {"offer": offer(), "note": "hi"}, T0 + 10)
        self.p.append("decline", "human:ana", {"applicant": "w9", "reason": "full"}, T0 + 11)
        s = self.p.state()
        self.assertNotIn("w9", s.actors)
        self.assertEqual(list(s.applications.values())[0].reason, "full")
        self.p.append("project", "human:ana", {"join": "closed"}, T0 + 12)
        e = self.p.append("apply", "w8", {"offer": offer()}, T0 + 13)
        self.assertIn("closed", self.p.state().ignored[e.id])

    def test_a_log_without_a_project_stays_open(self):
        bare = Store(self.root / "bare")
        bare.init()
        (bare.home / "policy.toml").write_text(POLICY, encoding="utf-8")
        bare.append("join", "plan", {"offer": offer()}, T0)
        self.assertIn("plan", bare.state().actors)


class CatalogTests(Base):
    def test_publish_discover_follow_links_and_skip_bad_entries(self):
        ws = Workspace()
        ws.add_project(self.p.state().project_id, self.p.home, trusted=True)
        other = make_project(self.root / "other", name="other")
        sub = self.root / "cats" / "sub.json"
        sub.parent.mkdir()
        publish(sub, manifest_of(other))
        top = self.root / "cats" / "top.json"
        cat = mf.new_catalog("top")
        cat["projects"] = [manifest_of(self.p), {"baton": "1", "id": "nope", "name": "broken"}]
        cat["catalogs"] = [{"name": "sub", "url": "sub.json"}]          # relative to top.json
        top.write_text(json.dumps(cat), encoding="utf-8")
        ws.add_catalog(str(top))
        entries, problems = ws.discover()
        self.assertEqual([(e["manifest"]["name"], e["local"]) for e in entries], [("toy", True), ("other", False)])
        self.assertIn("top", entries[0]["sources"])          # the local one is also listed in a catalog
        self.assertTrue(any("skipped" in p for p in problems))

    def test_git_urls_that_could_run_commands_are_refused(self):
        for url in ("ext::sh -c touch% /tmp/pwned", "--upload-pack=touch /tmp/x", "/plain/path", "https://ok/x.git::y"):
            self.assertFalse(mf.git_url_ok(url), url)
        self.assertTrue(mf.git_url_ok("https://github.com/o/r.git"))
        self.assertTrue(mf.git_url_ok("git@github.com:o/r.git"))


class CloneAndTrustTests(Base):
    def remote(self, store: Store) -> str:
        g = ["git", "-C", str(store.home)]
        subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "log"], check=True)
        return "file://" + str(store.home)

    def test_clone_checks_the_id_and_a_project_cannot_choose_what_runs(self):
        self.p.append("project", "human:ana", {"join": "open"}, T0 + 1)
        self.p.append("join", "w1", {"offer": offer()}, T0 + 4)
        url = self.remote(self.p)
        m = manifest_of(self.p)
        m["log"] = {"git": url, "branch": "main"}
        ws = Workspace()
        local = ws.clone(m)
        self.assertFalse(ws.entry_for(local.home)[1]["trusted"])
        # an old-style policy that names a command is refused outright, rather than half obeyed
        hostile = POLICY + '\n[runner]\ncmd = ["sh", "-c", "touch PWNED"]\n'
        (local.home / "policy.toml").write_text(hostile, encoding="utf-8")
        with self.assertRaises(ValueError):
            local.policy()
        self.assertFalse((Path.cwd() / "PWNED").exists())
        # a catalog that names a known id but points at another log is refused
        impostor = make_project(self.root / "impostor", name="impostor")
        m2 = dict(m, log={"git": self.remote(impostor), "branch": "main"}, id=m["id"])
        ws2 = Workspace(self.root / "ws2")
        with self.assertRaises(ValueError):
            ws2.clone(m2)


class ImportTests(Base):
    def test_import_checks_the_event(self):
        other = Store(self.root / "applicant")
        e = other.append("apply", "w9", {"offer": offer()}, T0 + 10)
        f = other.dir / f"{e.id}.json"
        self.p.import_file(f)
        self.assertIn(e.id, self.p.read()[0])
        d = json.loads(f.read_text(encoding="utf-8"))
        d["body"]["offer"]["slots"] = 64
        f.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.p.import_file(f)


if __name__ == "__main__":
    unittest.main()
