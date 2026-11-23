import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from baton import manifest as mf
from baton import policy as pol
from baton import ui
from baton.store import Store
from baton.workspace import Workspace, manifest_of, publish

T0 = 1_000_000.0
POLICY = pol.DEFAULT.replace('humans = []', 'humans = ["human:ana"]')


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"BATON_WORKSPACE": str(root / "ws")})
        self.env.start()
        self.ws = Workspace()
        self.s = Store(root / "log")
        self.s.init()
        (self.s.home / "policy.toml").write_text(POLICY, encoding="utf-8")
        a = self.s.append
        a("project", "human:ana", {"name": "toy", "summary": "<b>not bold</b>", "join": "open",
                                   "needs": [{"role": "worker", "skills": ["python"]}]}, T0)
        a("join", "p", {"role": "planner", "parent": "human:ana"}, T0 + 1)
        a("join", "o", {"role": "orchestrator", "parent": "p"}, T0 + 2)
        a("join", "w1", {"role": "worker", "parent": "o"}, T0 + 3)
        a("project", "human:ana", {"join": "apply"}, T0 + 4)
        self.task = a("task", "o", {"title": "fix parser", "paths": ["src"]}, T0 + 5)
        a("claim", "w1", {"task_id": self.task.id, "ttl": 10**6}, T0 + 6)
        self.prop = a("propose", "w1", {"action": "comment", "target": "#7",
                                        "payload": {"repo": "o/r", "body": "<script>x</script>"}}, T0 + 7)
        a("propose", "w1", {"action": "comment", "target": "#7",
                            "payload": {"repo": "o/r", "body": "<script>x</script>"}}, T0 + 8)
        self.pid = self.s.state().project_id
        self.ws.add_project(self.pid, self.s.home, trusted=True)
        # someone else's project, known only from a catalog
        other = Store(root / "other")
        other.init()
        (other.home / "policy.toml").write_text(POLICY, encoding="utf-8")
        other.append("project", "human:bo", {"name": "other"}, T0)   # ignored: bo is not a human there
        other.append("project", "human:ana", {"name": "other", "tags": ["c"]}, T0 + 1)
        self.other = manifest_of(other)
        cat = root / "cat.json"
        publish(cat, self.other)
        self.ws.add_catalog(str(cat))

    def tearDown(self):
        for srv in getattr(self, "servers", []):
            srv.shutdown()
            srv.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def start(self, actor):
        srv, token = ui.make_server(self.ws, actor, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers = getattr(self, "servers", []) + [srv]
        return srv.server_address[1], token

    def call(self, port, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)

    def test_snapshot_shows_board_leases_tree_gate_and_manifest(self):
        snap = ui.snapshot(self.s, now=T0 + 10)
        self.assertEqual([(t["title"], t["status"], t["holder"]) for t in snap["tasks"]], [("fix parser", "taken", "w1")])
        self.assertEqual(sorted(p["status"] for p in snap["proposals"]), ["duplicate", "pending"])
        self.assertEqual((snap["manifest"]["name"], snap["join"]), ("toy", "apply"))
        self.assertEqual(mf.check_manifest(snap["manifest"]), [])

    def test_directory_lists_local_and_catalog_projects(self):
        port, _ = self.start("human:ana")
        status, d = self.call(port, "GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual([(p["manifest"]["name"], p["local"]) for p in d["projects"]], [("toy", True), ("other", False)])
        status, page = self.call(port, "GET", "/")
        self.assertIn(b"Projects", page)
        status, snap = self.call(port, "GET", f"/api/p/{self.pid}/state")
        self.assertTrue(snap["me_here"])
        self.assertEqual(self.call(port, "GET", f"/api/p/{self.other['id']}/state")[0], 404)   # no local log

    def test_wrong_host_and_missing_token_are_refused(self):
        port, token = self.start("human:ana")
        self.assertEqual(self.call(port, "GET", "/api/projects", headers={"Host": "evil.example"})[0], 403)
        req = {"proposal": self.prop.id, "decision": "approve"}
        self.assertEqual(self.call(port, "POST", f"/api/p/{self.pid}/decide", req)[0], 403)
        self.assertEqual(self.call(port, "POST", f"/api/p/{self.pid}/decide", req, {"X-Baton-Token": "nope"})[0], 403)
        self.assertEqual(self.s.state().proposals[self.prop.id].status, "pending")

    def test_approve_reject_and_only_humans_of_the_project(self):
        port, token = self.start("human:ana")
        h = {"X-Baton-Token": token}
        url = f"/api/p/{self.pid}/decide"
        status, d = self.call(port, "POST", url, {"proposal": self.prop.id[:10], "decision": "approve"}, h)
        self.assertEqual((status, d["status"]), (200, "approved"))
        status, d = self.call(port, "POST", url, {"proposal": self.prop.id, "decision": "reject", "reason": "no"}, h)
        self.assertEqual(d["status"], "rejected")
        status, d = self.call(port, "POST", url, {"proposal": self.prop.id, "decision": "approve"}, h)
        self.assertEqual(status, 400)
        port2, token2 = self.start("human:zed")
        status, d = self.call(port2, "POST", url, {"proposal": self.prop.id, "decision": "approve"}, {"X-Baton-Token": token2})
        self.assertEqual(status, 403)

    def test_volunteer_admit_and_link(self):
        port, token = self.start("human:ana")
        h = {"X-Baton-Token": token}
        status, d = self.call(port, "POST", "/api/volunteer", {"project": self.pid, "actor": "helper-1", "role": "worker",
                                                               "parent": "o", "skills": "python, c", "note": "hi"}, h)
        self.assertEqual(status, 200, d)
        self.assertIn("deliver", d)
        app = list(self.s.state().applications.values())[0]
        self.assertEqual((app.actor, app.offer), ("helper-1", {"skills": ["python", "c"]}))
        status, d = self.call(port, "POST", f"/api/p/{self.pid}/decide", {"decision": "admit", "applicant": "helper-1"}, h)
        self.assertEqual(status, 200, d)
        self.assertEqual(self.s.state().actors["helper-1"].parent, "o")
        bad = self.call(port, "POST", "/api/volunteer", {"project": self.pid, "actor": "human:x"}, h)
        self.assertEqual(bad[0], 400)
        status, d = self.call(port, "POST", f"/api/p/{self.pid}/link",
                              {"rel": "depends-on", "project": self.other["id"], "name": "other"}, h)
        self.assertEqual(status, 200, d)
        self.assertEqual(self.s.state().project["links"][0]["project"], self.other["id"])
        status, d = self.call(port, "POST", f"/api/p/{self.pid}/link", {"rel": "likes", "project": "x"}, h)
        self.assertEqual(status, 400)

    def test_catalogs_can_be_added_and_read_only_mode(self):
        port, token = self.start(None)
        status, _ = self.call(port, "POST", "/api/catalogs", {"source": "/x.json"}, {"X-Baton-Token": token})
        self.assertEqual(status, 403)
        port, token = self.start("human:ana")
        status, _ = self.call(port, "POST", "/api/catalogs", {"source": "/nope.json"}, {"X-Baton-Token": token})
        self.assertEqual(status, 200)
        status, d = self.call(port, "GET", "/api/projects?fresh=1")
        self.assertIn("/nope.json", d["catalogs"])
        self.assertTrue(any("nope.json" in p for p in d["problems"]))


if __name__ == "__main__":
    unittest.main()
