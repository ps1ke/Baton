"""The web app as people use it: a session from the printed link, then everything through HTTP."""

import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from baton import app, machine as mc, p2p, ui
from baton.store import Store
from baton.workspace import Workspace

from helpers import fake_probe


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class App(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.forge = r / "forge.git"
        git("init", "-q", "--bare", "-b", "main", str(self.forge))
        seed = r / "seed"
        git("clone", "-q", str(self.forge), str(seed))
        (seed / "README.md").write_text("hi\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "start", cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        self.ws = Workspace(r / "ws")
        self.ws.set_machine(allowed=["claude:claude-sonnet-5-5"], name="m-author")
        for target in (mc, app):
            p = mock.patch.object(target if target is mc else app.mc, "probe", lambda paths=(), limits=False: fake_probe("claude"))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ui, "_probe", lambda fresh=False, limits=False: fake_probe("claude"))
        p.start()
        self.addCleanup(p.stop)
        self.srv, self.token = ui.make_server(self.ws, None, port=0, token="t0k3n")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.cookie = None

    def tearDown(self):
        self.srv.runs.stop_all()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        set_cookie = r.getheader("Set-Cookie")
        c.close()
        return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data), set_cookie

    def test_the_link_gives_a_session_and_a_person_starts_a_project_from_a_repository(self):
        status, d, _ = self.call("POST", "/api/me", {"name": "ana"})
        self.assertEqual(status, 403)                                         # no session yet
        status, _, cookie = self.call("POST", "/api/session", headers={"X-Baton-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _, cookie = self.call("POST", "/api/session", headers={"X-Baton-Token": "t0k3n"})
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.cookie = cookie.split(";")[0]
        self.assertEqual(self.call("POST", "/api/me", {"name": "ana"})[1], {"me": "human:ana"})
        self.assertEqual(self.call("POST", "/api/me", {"name": "a b"})[0], 400)
        status, d, _ = self.call("POST", "/api/project/create", {"repo": "file://" + str(self.forge), "summary": "a test"})
        self.assertEqual(status, 200, d)
        store = Store(self.ws.projects()[d["project"]]["home"])
        s = store.state()
        self.assertEqual((s.author, s.project["name"], s.actors["m-author"].right), ("human:ana", "forge", "steward"))
        self.assertIsNotNone(s.policy_from)                                   # the policy travels in the log
        status, out, _ = self.call("POST", f"/api/p/{d['key']}/task", {"title": "Add a changelog", "size": "S",
                                                                       "paths": "docs, CHANGELOG.md", "checks": "test -f CHANGELOG.md"})
        self.assertEqual(status, 200, out)
        t = store.state().tasks[out["id"]]
        self.assertEqual((t.kind, t.paths, t.base), ("implement", ("docs", "CHANGELOG.md"), git("rev-parse", "main", cwd=self.forge)))
        status, snap, _ = self.call("GET", f"/api/p/{d['key']}/state")
        self.assertTrue(snap["me_author"])
        self.assertEqual(snap["run"]["running"], False)

    def test_the_update_button_checks_github_and_refuses_to_install_into_a_checkout(self):
        self.cookie = self.call("POST", "/api/session", headers={"X-Baton-Token": "t0k3n"})[2].split(";")[0]
        from baton import update
        with mock.patch.object(update, "latest", lambda branch="main": "f" * 40):
            status, d, _ = self.call("POST", "/api/update", {"action": "check"})
            self.assertEqual((status, d["available"], d["can_install"]), (200, True, False))
            status, d, _ = self.call("POST", "/api/update", {"action": "install"})
            self.assertEqual(status, 400)
            self.assertIn("development checkout", d["error"])
        self.cookie = None
        self.assertEqual(self.call("POST", "/api/update", {"action": "check"})[0], 403)   # needs the session

    def test_an_invite_link_brings_a_second_machine_in(self):
        self.cookie = self.call("POST", "/api/session", headers={"X-Baton-Token": "t0k3n"})[2].split(";")[0]
        self.call("POST", "/api/me", {"name": "ana"})
        status, d, _ = self.call("POST", "/api/invite", {})
        self.assertEqual(status, 404 if isinstance(d, dict) and "error" in d else status)
        pid = self.call("POST", "/api/project/create", {"repo": "file://" + str(self.forge)})[1]["project"]
        status, d, _ = self.call("POST", f"/api/p/{pid}/invite", {})
        self.assertIn("address", d["error"])                                  # others need a way to reach us
        self.call("POST", "/api/machine", {"address": "127.0.0.1"})
        url = self.call("POST", f"/api/p/{pid}/invite", {})[1]["url"]
        inv = p2p.parse_invite(url)
        # the author's node (normally the running swarm) answers on the project's port
        from baton import crypto as _c
        store = Store(self.ws.projects()[pid]["home"], keyring=_c.default_keyring())   # this machine's keys, fixed now
        node = p2p.Node(store, "m-author", self.ws.dir / "tls")
        node.serve("127.0.0.1", int(inv["addr"].rsplit(":", 1)[1]))
        try:
            other = Workspace(Path(self.tmp.name) / "ws2")
            other.set_machine(allowed=["claude:claude-sonnet-5-5"], name="m-friend")
            from baton import crypto
            with mock.patch("baton.crypto.default_keyring", lambda: crypto.Keyring(other.dir / "keys")):
                out = app.connect(other, url, "evenings")
            self.assertEqual((out["status"], out["name"]), ("applied", "forge"))
            self.assertEqual([a.actor for a in store.state().applications.values()], ["m-friend"])
            status, _, _ = self.call("POST", f"/api/p/{pid}/decide", {"decision": "admit", "applicant": "m-friend"})
            self.assertEqual(status, 200)
            self.call("POST", f"/api/p/{pid}/grant", {"to": "m-friend"})
            self.assertEqual(store.state().actors["m-friend"].right, "steward")
        finally:
            node.stop()


if __name__ == "__main__":
    unittest.main()
