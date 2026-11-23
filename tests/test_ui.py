import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from baton import policy as pol
from baton import ui
from baton.store import Store

T0 = 1_000_000.0


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "log")
        self.s.init()
        (self.s.home / "policy.toml").write_text(pol.DEFAULT.replace('humans = []', 'humans = ["human:ana"]'),
                                                 encoding="utf-8")
        a = self.s.append
        a("join", "p", {"role": "planner", "parent": "human:ana"}, T0)
        a("join", "o", {"role": "orchestrator", "parent": "p"}, T0 + 1)
        a("join", "w1", {"role": "worker", "parent": "o"}, T0 + 2)
        self.task = a("task", "o", {"title": "fix parser", "paths": ["src"]}, T0 + 3)
        a("claim", "w1", {"task_id": self.task.id, "ttl": 10**6}, T0 + 4)
        self.prop = a("propose", "w1", {"action": "comment", "target": "#7",
                                        "payload": {"repo": "o/r", "body": "<script>x</script>"}}, T0 + 5)
        a("propose", "w1", {"action": "comment", "target": "#7",
                            "payload": {"repo": "o/r", "body": "<script>x</script>"}}, T0 + 6)

    def tearDown(self):
        for srv in getattr(self, "servers", []):
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()

    def start(self, actor):
        srv, token = ui.make_server(self.s, actor, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers = getattr(self, "servers", []) + [srv]
        return srv.server_address[1], token

    def call(self, port, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_snapshot_shows_board_leases_tree_and_gate(self):
        snap = ui.snapshot(self.s, now=T0 + 10)
        self.assertEqual([(t["title"], t["status"], t["holder"]) for t in snap["tasks"]], [("fix parser", "taken", "w1")])
        self.assertEqual([l["actor"] for l in snap["leases"]], ["w1"])
        self.assertEqual(sorted(p["status"] for p in snap["proposals"]), ["duplicate", "pending"])
        self.assertEqual(snap["counts"]["refused"], 1)
        self.assertEqual({a["name"]: a["role"] for a in snap["actors"]}["w1"], "worker")
        self.assertTrue(any("duplicate" == f["result"] for f in snap["feed"]))

    def test_page_and_state_are_served_with_a_strict_policy(self):
        port, _ = self.start("human:ana")
        status, page = self.call(port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Waiting for approval", page)
        status, data = self.call(port, "GET", "/api/state")
        self.assertEqual((status, json.loads(data)["me"]), (200, "human:ana"))

    def test_wrong_host_and_missing_token_are_refused(self):
        port, token = self.start("human:ana")
        self.assertEqual(self.call(port, "GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        req = {"proposal": self.prop.id, "decision": "approve"}
        self.assertEqual(self.call(port, "POST", "/api/decide", req)[0], 403)
        self.assertEqual(self.call(port, "POST", "/api/decide", req, {"X-Baton-Token": "nope"})[0], 403)
        self.assertEqual(self.s.state().proposals[self.prop.id].status, "pending")

    def test_approve_and_reject_from_the_page(self):
        port, token = self.start("human:ana")
        h = {"X-Baton-Token": token}
        status, data = self.call(port, "POST", "/api/decide", {"proposal": self.prop.id[:10], "decision": "approve"}, h)
        self.assertEqual((status, json.loads(data)["status"]), (200, "approved"))
        status, data = self.call(port, "POST", "/api/decide", {"proposal": self.prop.id, "decision": "reject",
                                                               "reason": "changed my mind"}, h)
        self.assertEqual(json.loads(data)["status"], "rejected")
        status, data = self.call(port, "POST", "/api/decide", {"proposal": self.prop.id, "decision": "approve"}, h)
        self.assertEqual(status, 400)
        self.assertIn("proposal is rejected", json.loads(data)["error"])

    def test_read_only_without_a_human(self):
        port, token = self.start(None)
        status, _ = self.call(port, "POST", "/api/decide", {"proposal": self.prop.id, "decision": "approve"},
                              {"X-Baton-Token": token})
        self.assertEqual(status, 403)
        with self.assertRaises(ValueError):
            ui.make_server(self.s, "w1", port=0)       # an agent cannot drive the dashboard


if __name__ == "__main__":
    unittest.main()
