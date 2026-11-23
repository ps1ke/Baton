import base64
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from baton import machine as mc
from baton.workspace import Workspace

from helpers import fake_probe


class Hardware(unittest.TestCase):
    def test_the_real_machine_answers_something_sane(self):
        p = mc.probe([tempfile.gettempdir()])
        self.assertGreaterEqual(p["cpu"]["logical_cores"], 1)
        self.assertTrue(p["disk"] and p["disk"][0]["total_gb"] > 0)
        self.assertEqual({a["id"] for a in p["agents"]} >= {"claude", "codex"}, True)
        self.assertGreaterEqual(mc.suggested_slots(p), 1)

    def test_nvidia_smi_is_parsed(self):
        out = "NVIDIA GeForce RTX 4090, 24564, 1200, 37\n"
        with mock.patch.object(mc.shutil, "which", lambda name: "/x/" + name if name == "nvidia-smi" else None), \
                mock.patch.object(mc, "_run", lambda cmd, **kw: (0, out)):
            g = mc.gpus()
        self.assertEqual(g, [{"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090", "vram_gb": 24.0,
                              "vram_used_gb": 1.2, "utilization_percent": 37.0}])

    def test_local_model_tiers_follow_the_parameter_count(self):
        self.assertEqual([mc.guess_local_tier(n) for n in ("qwen3-coder:30b", "llama3.3:70b", "phi4:14b", "mystery")],
                         ["standard", "strong", "fast", "fast"])


class Agents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claude_plan_is_read_and_no_token_is_returned(self):
        (self.home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "SECRET-A", "refreshToken": "SECRET-R", "subscriptionType": "max",
            "refreshTokenExpiresAt": int((time.time() + 3600) * 1000)}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home), "BATON_CLAUDE_BIN": "sh"}):
            c = mc.claude()
        self.assertEqual((c["installed"], c["logged_in"], c["plan"]), (True, True, "max"))
        self.assertNotIn("SECRET", json.dumps(c))

    def test_an_expired_claude_login_is_not_logged_in(self):
        (self.home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "a", "subscriptionType": "pro", "refreshTokenExpiresAt": 1000}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home), "BATON_CLAUDE_BIN": "sh"}):
            self.assertFalse(mc.claude()["logged_in"])

    def test_codex_plan_comes_from_the_id_token_claims(self):
        claims = base64.urlsafe_b64encode(json.dumps(
            {"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}).encode()).decode().rstrip("=")
        (self.home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"h.{claims}.s", "access_token": "SECRET"}}))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home), "BATON_CODEX_BIN": "sh"}), \
                mock.patch.object(mc, "_codex_app", lambda path, reqs, timeout=10: [{"data": [{"model": "gpt-6.1-sol"},
                                                                                           {"model": "gpt-6.1-mini"}]}]):
            c = mc.codex()
        self.assertEqual((c["logged_in"], c["plan"]), (True, "pro"))
        self.assertEqual(c["models"], [{"model": "gpt-6.1-sol", "tier": "strong"}, {"model": "gpt-6.1-mini", "tier": "fast"}])
        self.assertNotIn("SECRET", json.dumps(c))

    def test_an_ollama_server_on_localhost_is_found(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"models": [{"name": "qwen3-coder:30b"}]}).encode()
                self.send_response(200 if self.path == "/api/tags" else 404)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(mc, "LOCAL_SERVERS", [("ollama", f"http://127.0.0.1:{srv.server_address[1]}", "ollama")]), \
                    mock.patch.dict(os.environ, {"OLLAMA_HOST": ""}):
                found = mc.local_servers()
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(found[0]["models"], [{"model": "qwen3-coder:30b", "tier": "standard"}])


class Offer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_allowed_usable_models_are_offered_with_the_users_tiers_and_slots(self):
        p = fake_probe("claude")
        p["agents"].append({"id": "codex", "kind": "subscription", "installed": False, "logged_in": False,
                            "models": [{"model": "gpt-6.1-sol", "tier": "strong"}]})
        cfg = self.ws.set_machine(allowed=["claude:claude-sonnet-5-5", "codex:gpt-6.1-sol"], slots=3,
                                  tiers={"claude:claude-sonnet-5-5": "strong"})
        o = mc.offer(p, cfg, 180)
        self.assertEqual(o["models"], [{"id": "claude:claude-sonnet-5-5", "tier": "strong", "family": "claude"}])
        self.assertEqual((o["slots"], o["ttl"]), (3, 180.0))
        why = {m["ref"]: m["why"] for m in mc.models(p, cfg)}
        self.assertIn("not installed", why["codex:gpt-6.1-sol"])
        cfg = self.ws.set_machine(share_hardware=False)
        self.assertNotIn("hardware", mc.offer(p, cfg, 180))
        with self.assertRaises(ValueError):
            self.ws.set_machine(tiers={"x:y": "huge"})

    def test_commands_and_effort_come_from_the_machine(self):
        p = fake_probe("claude", "codex")
        cfg = self.ws.set_machine(allowed=["codex:gpt-6.1-sol"])
        cmd, effort = mc.command_for(p, cfg, "codex:gpt-6.1-sol")
        self.assertEqual((cmd[:2], effort), (["codex", "exec"], "high"))
        with self.assertRaises(PermissionError):
            mc.command_for(p, cfg, "claude:claude-opus-5-5")
        self.assertTrue(mc.default_name().startswith("m-"))


if __name__ == "__main__":
    unittest.main()
