"""`baton ui`: a local dashboard over every project this machine knows.

The home page is the directory: the projects in your workspace and in the catalogs you follow, with what they need
and how they link to each other, and a Help button that applies to a project as one of your agents. A project page
shows its board (with each task's message thread), leases, agents, applications, proposals and activity, and lets a
human of that project admit, decline, approve, reject, execute, add links and answer agents' messages.

It listens on 127.0.0.1 only. Every write needs the session token printed with the URL (so another web page open in
the same browser cannot post to it), and every request must name this host, which stops DNS-rebinding pages. All
text from logs and catalogs is untrusted and the page only ever inserts it as text.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import re
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from . import forge
from . import manifest as mf
from .store import Store
from .workspace import Workspace, manifest_of, volunteer

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
    elif e.id in s.applications:
        result = s.applications[e.id].status
    else:
        result = "ok"
    k = e.kind
    if k == "project":
        text = "created the project" if e.id == s.project_id else "changed " + ", ".join(sorted(b))
    elif k == "join":
        text = f"joined as {b.get('role')} under {b.get('parent')}"
    elif k == "apply":
        text = f"applied as {b.get('role')}" + (f" under {b['parent']}" if b.get("parent") else "")
    elif k in ("admit", "decline"):
        text = ("admitted " if k == "admit" else "declined ") + str(b.get("applicant"))
    elif k == "task":
        text = f"posted “{b.get('title', '')}”" + (" (subtask)" if b.get("parent") else "")
    elif k == "claim":
        if b.get("task_id"):
            text = f"took “{_title(s, b['task_id'])}”"
        else:
            text = f"claimed {', '.join(b.get('paths', []) + b.get('topics', [])) or '?'} for {b.get('task', '')}"
    elif k == "release":
        lease = s.leases.get(b.get("lease", ""))
        text = f"released {lease.task if lease else str(b.get('lease', ''))[:12]}"
    elif k == "done":
        text = f"{b.get('outcome', 'done')} “{_title(s, b.get('task', ''))}”" + (f": {b['summary']}" if b.get("summary") else "")
    elif k == "propose":
        text = f"proposed {b.get('action')} on {b.get('target') or '-'}"
    elif k in ("approve", "reject", "executed"):
        p = s.proposals.get(b.get("proposal", ""))
        what = f"{p.action} on {p.target or '-'} by {p.actor}" if p else str(b.get("proposal", ""))[:12]
        text = {"approve": "approved", "reject": "rejected", "executed": "executed"}[k] + " " + what
    elif k == "message":
        m = s.messages.get(e.id)
        where = f" in “{_title(s, m.task)}”" if m and m.task else ""
        to = f" to {m.to}" if m and m.to else ""
        text = ("replied" if b.get("reply_to") else "said") + to + where + ": " + str(b.get("text", ""))[:200]
    else:
        text = k
    why = s.ignored.get(e.id) or s.denied.get(e.id) or (s.proposals[e.id].reason if e.id in s.proposals else "")
    return text + (f" — {why}" if why else ""), result


def _message(m) -> dict:
    return {"id": m.id, "actor": m.actor, "text": m.text, "task": m.task, "to": m.to, "reply_to": m.reply_to,
            "root": m.root, "ts": m.ts}


MESSAGES = 400


def snapshot(store: Store, now: float | None = None, me: str | None = None) -> dict:
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
    applications = [{"id": a.id, "actor": a.actor, "role": a.role, "parent": a.parent, "offer": a.offer,
                     "note": a.note, "ts": a.ts, "status": a.status, "decided_by": a.decided_by, "reason": a.reason}
                    for a in sorted(s.applications.values(), key=lambda a: a.ts, reverse=True)]
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
        "leases": leases, "proposals": proposals, "applications": applications, "feed": feed,
        "manifest": manifest_of(store, now), "join": s.join_mode,
        "roles": [{"name": r.name, "under": r.under, "model": r.model, "effort": r.effort} for r in pol.roles.values()],
        "messages": [_message(m) for m in list(s.messages.values())[-MESSAGES:]],
        "inbox": [m.id for m in s.inbox(me)] if me else [],
        "counts": {"refused": refused, "denied": len(s.denied), "ignored": len(s.ignored)},
        "executors": sorted(forge.ACTIONS),
    }


def decide(store: Store, actor: str, req: dict) -> dict:
    """approve / reject / execute a proposal, or admit / decline an application, as `actor`."""
    decision = str(req.get("decision", ""))
    if decision in ("admit", "decline"):
        body = {"applicant": str(req["applicant"])}
        if decision == "decline" and req.get("reason"):
            body["reason"] = str(req["reason"])
        e = store.append(decision, actor, body)
        after = store.state()
        if e.id in after.ignored:
            raise ValueError(after.ignored[e.id])
        return {"ok": True}
    s = store.state()
    pid = store.resolve(str(req["proposal"]), s.proposals)
    if decision in ("approve", "reject"):
        body = {"proposal": pid}
        if decision == "reject" and req.get("reason"):
            body["reason"] = str(req["reason"])
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


def say(store: Store, actor: str, req: dict) -> dict:
    body = {"text": str(req.get("text", ""))}
    for k in ("task", "to", "reply_to"):
        v = str(req.get(k) or "").strip()
        if v:
            body[k] = v
    if "reply_to" in body:
        body.pop("task", None)
    e = store.append("message", actor, body)
    after = store.state()
    if e.id in after.ignored:
        raise ValueError(after.ignored[e.id])
    return {"ok": True, "id": e.id}


def add_link(store: Store, actor: str, req: dict) -> dict:
    link = {"rel": str(req.get("rel", "related"))}
    for k in ("project", "manifest", "name"):
        v = str(req.get(k) or "").strip()
        if v:
            link[k] = v
    s = store.state()
    links = [l for l in s.project.get("links", []) if l != link] + [link]
    e = store.append("project", actor, {"links": links})
    after = store.state()
    if e.id in after.ignored:
        raise ValueError(after.ignored[e.id])
    return {"ok": True}


def key_for(home: str, project_id: str | None) -> str:
    return project_id or "log-" + hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:16]


class Directory:
    """What the page can see: the workspace's projects and catalogs, plus the log `baton ui` was started in."""

    def __init__(self, ws: Workspace, extra_home: Path | None = None):
        self.ws, self.extra = ws, extra_home
        self._cache: tuple[float, list, list] | None = None

    def entries(self, fresh: bool = False) -> tuple[list[dict], list[str]]:
        if not fresh and self._cache and time.time() - self._cache[0] < 30:
            return self._cache[1], self._cache[2]
        entries, problems = self.ws.discover()
        if self.extra and (self.extra / "events").is_dir():
            home = str(self.extra.resolve())
            if not any(e.get("home") == home for e in entries):
                store = Store(self.extra)
                m = manifest_of(store)
                hit = next((e for e in entries if m and e["manifest"]["id"] == m["id"]), None)
                if hit:
                    hit.update(local=True, home=home)
                else:
                    entries.insert(0, {"manifest": m, "local": True, "trusted": True, "home": home,
                                       "sources": ["this folder"]})
        for e in entries:
            e["key"] = key_for(e.get("home", ""), e["manifest"]["id"] if e.get("manifest") else None)
        self._cache = (time.time(), entries, problems)
        return entries, problems

    def store(self, key: str) -> Store:
        for e in self.entries()[0]:
            if e["key"] == key and e.get("home"):
                return Store(e["home"])
        for e in self.entries(fresh=True)[0]:
            if e["key"] == key and e.get("home"):
                return Store(e["home"])
        raise KeyError("no local log for this project")

    def entry(self, project_id: str) -> dict:
        for e in self.entries(fresh=True)[0]:
            if e.get("manifest") and e["manifest"]["id"] == project_id:
                return e
        raise KeyError("unknown project")


def make_server(ws: Workspace, actor: str | None, host: str = "127.0.0.1", port: int = 8765,
                extra_home: Path | None = None):
    token = secrets.token_urlsafe(24)
    directory = Directory(ws, extra_home)
    route = re.compile(r"^/api/p/([\w-]{1,80})/(state|decide|link|message)$")

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
                             "connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
                             "form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 65536:
                raise ValueError("request too large")
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                raise ValueError("expected a JSON object")
            return d

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "wrong host"})
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/projects":
                entries, problems = directory.entries(fresh="fresh" in (urlparse(self.path).query or ""))
                return self._json(200, {"me": actor, "projects": entries, "problems": problems,
                                        "catalogs": ws.load()["catalogs"], "now": time.time()})
            m = route.match(path)
            if m and m.group(2) == "state":
                try:
                    store = directory.store(m.group(1))
                except KeyError as err:
                    return self._json(404, {"error": str(err)})
                snap = snapshot(store, me=actor)
                snap["me"] = actor
                snap["me_here"] = bool(actor) and actor in snap["humans"]
                return self._json(200, snap)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "wrong host"})
            if not secrets.compare_digest(self.headers.get("X-Baton-Token", ""), token):
                return self._json(403, {"error": "missing or wrong session token: open the URL baton printed"})
            if not actor:
                return self._json(403, {"error": "read-only: start `baton ui --actor human:<you>` to act"})
            path = urlparse(self.path).path
            try:
                req = self._body()
                if path == "/api/catalogs":
                    source = str(req.get("source", "")).strip()
                    if not source:
                        raise ValueError("give a catalog path or URL")
                    (ws.remove_catalog if req.get("action") == "remove" else ws.add_catalog)(source)
                    directory.entries(fresh=True)
                    return self._json(200, {"ok": True})
                if path == "/api/volunteer":
                    agent = str(req.get("actor", "")).strip()
                    if not re.fullmatch(r"[\w.:@-]{1,80}", agent) or agent.startswith("human:"):
                        raise ValueError("name your agent with letters, digits and . : @ - _ (not human:...)")
                    offer = {k: [x.strip() for x in str(req.get(k, "")).split(",") if x.strip()][:20]
                             for k in ("skills", "models", "hardware")}
                    offer = {k: v for k, v in offer.items() if v}
                    out = volunteer(ws, directory.entry(str(req["project"])), agent, str(req.get("role") or "worker"),
                                    str(req.get("parent") or "") or None, offer, str(req.get("note", ""))[:2000])
                    directory.entries(fresh=True)
                    return self._json(200, out)
                m = route.match(path)
                if not m or m.group(2) == "state":
                    return self._json(404, {"error": "not found"})
                store = directory.store(m.group(1))
                if actor not in store.policy().humans:
                    return self._json(403, {"error": f"{actor} is not a human of this project, so it cannot decide here"})
                out = {"decide": decide, "link": add_link, "message": say}[m.group(2)](store, actor, req)
                directory.entries(fresh=True)
                return self._json(200, out)
            except (KeyError, ValueError, TypeError, RuntimeError, NotImplementedError, OSError) as err:
                return self._json(400, {"error": str(err)})

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv, token


def serve(ws: Workspace, actor: str | None, port: int = 8765, open_browser: bool = True, extra_home: Path | None = None):
    srv, token = make_server(ws, actor, port=port, extra_home=extra_home)
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
