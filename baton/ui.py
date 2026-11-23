"""`baton ui`: a local dashboard over the log. It reads the same events the CLI does and writes only what a human may
write (approve, reject, execute), as that human.

It listens on 127.0.0.1 only. Every write needs the session token printed with the URL (so another web page open in
the same browser cannot post to it), and every request must name this host, which stops DNS-rebinding pages.
"""

from __future__ import annotations

import http.server
import json
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import forge
from .store import Store

PAGE = Path(__file__).with_name("ui.html")
FEED = 80


def _title(s, task_id):
    t = s.tasks.get(task_id)
    return t.title if t else task_id[:12]


def describe(e, s) -> tuple[str, str]:
    """A line for the activity feed and how it ended (ok, denied, ignored, or a proposal status)."""
    b = e.body
    if e.id in s.ignored:
        result = "ignored"
    elif e.id in s.denied:
        result = "denied"
    elif e.id in s.proposals:
        result = s.proposals[e.id].status
    else:
        result = "ok"
    k = e.kind
    if k == "join":
        text = f"joined as {b.get('role')} under {b.get('parent')}"
    elif k == "task":
        text = f"posted “{b.get('title', '')}”" + (" (subtask)" if b.get("parent") else "")
    elif k == "claim":
        if b.get("task_id"):
            text = f"took “{_title(s, b['task_id'])}”"
        else:
            text = f"claimed {', '.join(b.get('paths', []) + b.get('topics', [])) or '?'} for {b.get('task', '')}"
    elif k == "release":
        lease = s.leases.get(b.get("lease", ""))
        text = f"released {lease.task if lease else b.get('lease', '')[:12]}"
    elif k == "done":
        text = f"{b.get('outcome', 'done')} “{_title(s, b.get('task', ''))}”" + (f": {b['summary']}" if b.get("summary") else "")
    elif k == "propose":
        text = f"proposed {b.get('action')} on {b.get('target') or '-'}"
    elif k in ("approve", "reject", "executed"):
        p = s.proposals.get(b.get("proposal", ""))
        what = f"{p.action} on {p.target or '-'} by {p.actor}" if p else b.get("proposal", "")[:12]
        text = {"approve": "approved", "reject": "rejected", "executed": "executed"}[k] + " " + what
    else:
        text = k
    why = s.ignored.get(e.id) or s.denied.get(e.id) or (s.proposals[e.id].reason if e.id in s.proposals else "")
    return text + (f" — {why}" if why else ""), result


def snapshot(store: Store, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ordered, held, bad = store.load()
    s = store.state()
    pol = store.policy()

    def actor(a):
        return {"name": a.name, "role": a.role, "parent": a.parent, "model": a.model, "effort": a.effort,
                "children": a.children}

    tasks = []
    for t in sorted(s.tasks.values(), key=lambda x: x.created):
        status = s.task_status(t, now)
        lease = s.leases.get(t.lease) if t.lease else None
        tasks.append({"id": t.id, "title": t.title, "detail": t.detail, "status": status, "by": t.by,
                      "role": t.role, "parent": t.parent, "subtasks": t.subtasks, "scope": list(t.paths + t.topics),
                      "holder": lease.actor if lease and status == "taken" else None,
                      "left": round(lease.expiry - now) if lease and status == "taken" else None,
                      "summary": t.summary, "evidence": t.evidence, "created": t.created})
    leases = [{"id": l.id, "actor": l.actor, "task": l.task, "scope": list(l.paths), "topics": list(l.topics),
               "left": round(l.expiry - now), "ttl": l.ttl} for l in s.active_leases(now)]
    proposals = [{"id": p.id, "actor": p.actor, "action": p.action, "target": p.target, "payload": p.payload,
                  "status": p.status, "reason": p.reason, "decided_by": p.decided_by, "result": p.result, "ts": p.ts}
                 for p in sorted(s.proposals.values(), key=lambda p: p.ts, reverse=True)]
    feed = []
    for e in reversed(ordered):
        if e.kind == "renew":
            continue
        text, result = describe(e, s)
        feed.append({"id": e.id, "kind": e.kind, "actor": e.actor, "ts": e.ts, "text": text, "result": result})
        if len(feed) >= FEED:
            break
    refused = sum(1 for p in s.proposals.values() if p.status in ("duplicate", "over_budget", "unleased", "unknown_action"))
    return {
        "now": now, "home": str(store.home.resolve()), "events": len(ordered), "held": len(held), "bad": len(bad),
        "humans": sorted(pol.humans), "actors": [actor(a) for a in s.actors.values()], "tasks": tasks,
        "leases": leases, "proposals": proposals, "feed": feed,
        "counts": {"refused": refused, "denied": len(s.denied), "ignored": len(s.ignored)},
        "executors": sorted(forge.ACTIONS),
    }


def decide(store: Store, actor: str, proposal: str, decision: str, reason: str = "") -> dict:
    s = store.state()
    pid = store.resolve(proposal, s.proposals)
    if decision in ("approve", "reject"):
        body = {"proposal": pid}
        if decision == "reject" and reason:
            body["reason"] = reason
        e = store.append(decision, actor, body)
    elif decision == "execute":
        p = s.proposals[pid]
        if p.status != "approved":
            raise ValueError(f"proposal is {p.status}; only an approved proposal runs")
        e = store.append("executed", actor, {"proposal": pid, "result": forge.execute(p.action, p.target, p.payload)})
    else:
        raise ValueError(f"unknown decision {decision!r}")
    after = store.state()
    if e.id in after.ignored:
        raise ValueError(after.ignored[e.id])
    return {"ok": True, "status": after.proposals[pid].status}


def make_server(store: Store, actor: str | None, host: str = "127.0.0.1", port: int = 8765):
    token = secrets.token_urlsafe(24)
    humans = store.policy().humans
    if actor and actor not in humans:
        raise ValueError(f"{actor} is not in [gate] humans {sorted(humans)}; the dashboard writes as a human")

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "baton"

        def log_message(self, *args):
            pass

        def _host_ok(self) -> bool:
            name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return name in ("127.0.0.1", "localhost", "::1")

        def _send(self, code: int, body: bytes, kind: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "wrong host"})
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                snap = snapshot(store)
                snap["me"] = actor
                return self._json(200, snap)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "wrong host"})
            if not secrets.compare_digest(self.headers.get("X-Baton-Token", ""), token):
                return self._json(403, {"error": "missing or wrong session token: open the URL baton printed"})
            if urlparse(self.path).path != "/api/decide":
                return self._json(404, {"error": "not found"})
            if not actor:
                return self._json(403, {"error": "read-only: start `baton ui` with --actor <a human from the policy>"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 65536:
                    raise ValueError("request too large")
                req = json.loads(self.rfile.read(n) or b"{}")
                out = decide(store, actor, str(req["proposal"]), str(req["decision"]), str(req.get("reason", "")))
            except (KeyError, ValueError, TypeError, RuntimeError, NotImplementedError) as err:
                return self._json(400, {"error": str(err)})
            self._json(200, out)

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv, token


def serve(store: Store, actor: str | None, port: int = 8765, open_browser: bool = True):
    srv, token = make_server(store, actor, port=port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/#token={token}"
    print(f"baton ui on {url}" + ("" if actor else "  (read-only: no --actor)"), flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
