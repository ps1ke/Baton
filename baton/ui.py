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
import hmac
import http.server
import os
import json
import re
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from . import forge
from . import machine as mc
from . import manifest as mf
from . import sync
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
        n = len((b.get("offer") or {}).get("models", []))
        text = "joined" + (f", offering {n} model(s)" if n else "")
    elif k == "offer":
        text = f"is here: {len(b.get('models', []))} model(s), {b.get('slots', 0)} slot(s)"
    elif k == "apply":
        n = len((b.get("offer") or {}).get("models", []))
        text = f"applied with a machine offering {n} model(s)"
    elif k in ("admit", "decline"):
        text = ("admitted " if k == "admit" else "declined ") + str(b.get("applicant"))
    elif k == "task":
        tk = s.tasks.get(e.id)
        text = f"posted {tk.kind if tk else b.get('kind', 'a')} package “{b.get('title', '')}”" + \
            (f" at level {tk.level}" if tk and tk.level else "")
    elif k == "claim":
        if b.get("task_id"):
            tk = s.tasks.get(b["task_id"])
            text = f"took {tk.kind + ' ' if tk else ''}“{_title(s, b['task_id'])}”" + (f" with {b['model']}" if b.get("model") else "")
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

    from . import network
    used = network.load(s, now)
    from .crypto import fingerprint
    members = [{"name": a.name, "present": a.present(now), "offer": a.offer, "busy": used.get(a.name, 0),
                "last_seen": a.offer_at, "joined": a.joined, "right": a.right, "revoked": a.revoked,
                "key": fingerprint(a.key) if a.key else None} for a in s.members()]
    picks = {k: {"member": v.member, "model": v.model, "tier": v.tier} for k, v in network.assign(s, pol, now).items()}
    tasks = []
    for t in sorted(s.tasks.values(), key=lambda x: (x.created, x.id)):
        status = s.task_status(t, now)
        lease = s.leases.get(t.lease) if t.lease else None
        tasks.append({"id": t.id, "title": t.title, "detail": t.detail, "goal": t.goal, "status": status, "by": t.by,
                      "kind": t.kind, "level": t.level, "tier": t.tier, "of": t.of, "checks": t.checks,
                      "phase": t.phase, "rework": t.rework, "size": t.size, "acceptance": t.acceptance,
                      "parent": t.parent, "subtasks": t.subtasks, "scope": list(t.paths + t.topics),
                      "depends_on": list(t.depends_on), "runs": t.runs, "report": t.report, "feedback": t.feedback,
                      "holder": lease.actor if lease and status == "taken" else None,
                      "model": lease.model if lease and status == "taken" else None,
                      "left": round(lease.expiry - now) if lease and status == "taken" else None,
                      "summary": t.summary, "evidence": t.evidence, "created": t.created})
    leases = [{"id": l.id, "actor": l.actor, "task": l.task, "scope": list(l.paths), "topics": list(l.topics),
               "model": l.model, "left": round(l.expiry - now), "ttl": l.ttl} for l in s.active_leases(now)]
    proposals = [{"id": p.id, "actor": p.actor, "action": p.action, "target": p.target, "payload": p.payload,
                  "status": p.status, "reason": p.reason, "decided_by": p.decided_by, "result": p.result, "ts": p.ts}
                 for p in sorted(s.proposals.values(), key=lambda p: p.ts, reverse=True)]
    applications = [{"id": a.id, "actor": a.actor, "offer": a.offer,
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
        "humans": sorted(a.name for a in s.actors.values() if a.kind == "human" and a.level0) or sorted(pol.humans),
        "author": s.author, "mode": s.mode, "members": members, "assignment": picks, "tasks": tasks,
        "leases": leases, "proposals": proposals, "applications": applications, "feed": feed,
        "manifest": manifest_of(store, now), "join": s.join_mode,
        "network": {"max_depth": s.policy.max_depth, "max_children": pol.max_children, "max_rework": pol.max_rework,
                    "orchestrate": list(pol.orchestrate), "tiers": pol.tiers, "pipeline": list(pol.pipeline),
                    "presence_ttl": pol.presence_ttl},
        "sync": sync.status_of(store.home),
        "messages": [_message(m) for m in list(s.messages.values())[-MESSAGES:]],
        "inbox": [m.id for m in s.inbox(me)] if me else [],
        "counts": {"refused": refused, "denied": len(s.denied), "ignored": len(s.ignored)},
        "executors": sorted(forge.ACTIONS) + ["push"],
    }


def decide(store: Store, actor: str, req: dict) -> dict:
    """approve / reject / execute a proposal, or admit / decline an application, as `actor`."""
    decision = str(req.get("decision", ""))
    if decision in ("admit", "decline"):
        body = {"applicant": str(req["applicant"])}   # no role: the network decides what a member does
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
        from .cli import execute_action
        e = store.append("executed", actor, {"proposal": pid, "result": execute_action(store, p)})
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


_PROBE: dict = {}


def _probe(fresh: bool = False, limits: bool = False) -> dict:
    """The machine probe, cached for a minute (CPU sampling and Codex's model list take a moment)."""
    if fresh or limits or not _PROBE or time.time() - _PROBE["at"] > 60:
        _PROBE.clear()
        _PROBE.update(mc.probe([], limits=limits))
    return _PROBE


def machine_view(ws: Workspace, fresh: bool = False, limits: bool = False) -> dict:
    cfg = ws.machine()
    p = _probe(fresh, limits)
    return {"config": cfg, "probe": p, "models": mc.models(p, cfg), "suggested_slots": mc.suggested_slots(p),
            "offer": mc.offer(p, cfg, 180.0), "tiers": list(mc.TIERS)}


def set_machine(ws: Workspace, req: dict) -> dict:
    changes = {}
    if "allowed" in req:
        if not isinstance(req["allowed"], list) or not all(isinstance(x, str) for x in req["allowed"]):
            raise ValueError("allowed must be a list of provider:model")
        changes["allowed"] = list(dict.fromkeys(req["allowed"]))
    if "tiers" in req:
        if not isinstance(req["tiers"], dict):
            raise ValueError("tiers must map provider:model to a tier")
        changes["tiers"] = {str(k): str(v) for k, v in req["tiers"].items()}
    if "slots" in req:
        changes["slots"] = None if req["slots"] in (None, "") else int(req["slots"])
    if "name" in req:
        name = str(req["name"]).strip()
        if not re.fullmatch(r"[\w.@-]{1,80}", name):
            raise ValueError("a machine name is letters, digits and . @ - _")
        changes["name"] = name
    if "share_hardware" in req:
        changes["share_hardware"] = bool(req["share_hardware"])
    ws.set_machine(**changes)
    return machine_view(ws)


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


def ui_secret(ws: Workspace) -> str:
    """The app's session secret, kept in the workspace so a bookmarked baton keeps working across restarts."""
    f = ws.dir / "ui-secret"
    if not f.exists():
        ws.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(24))
    return f.read_text().strip()


def make_server(ws: Workspace, actor: str | None, host: str = "127.0.0.1", port: int = 8765,
                extra_home: Path | None = None, token: str | None = None):
    from . import app
    token = token or secrets.token_urlsafe(24)
    cookie = hmac.new(token.encode(), b"baton-session", hashlib.sha256).hexdigest()
    directory = Directory(ws, extra_home)
    runs = app.Runs(ws)
    route = re.compile(r"^/api/p/([\w-]{1,80})/(state|decide|link|message|task|invite|grant|revoke|run|discoverable)$")
    fixed_actor = actor

    def who():
        return fixed_actor or app.me(ws)

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

        def _json(self, code: int, obj, headers=()):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _session(self) -> bool:
            if secrets.compare_digest(self.headers.get("X-Baton-Token", ""), token):
                return True
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "baton_session" and secrets.compare_digest(v, cookie):
                    return True
            return False

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
            if path == "/api/machine":
                return self._json(200, machine_view(ws, fresh="fresh" in (urlparse(self.path).query or ""),
                                                    limits="limits" in (urlparse(self.path).query or "")))
            if path == "/api/app":
                cfg = ws.machine()
                from . import update
                return self._json(200, {"me": who(), "session": self._session(), "machine": cfg["name"],
                                        "allowed": len(cfg.get("allowed", [])), "address": ws.load().get("address", ""),
                                        "projects": len(ws.projects()), "version": __import__("baton").__version__,
                                        "update": update.status(ws)})
            if path == "/api/projects":
                entries, problems = directory.entries(fresh="fresh" in (urlparse(self.path).query or ""))
                for e in entries:
                    if e.get("manifest"):
                        e["running"] = runs.running(e["manifest"]["id"])
                return self._json(200, {"me": who(), "projects": entries, "problems": problems,
                                        "catalogs": ws.load()["catalogs"], "now": time.time()})
            m = route.match(path)
            if m and m.group(2) == "state":
                try:
                    store = directory.store(m.group(1))
                except KeyError as err:
                    return self._json(404, {"error": str(err)})
                actor = who()
                snap = snapshot(store, me=actor)
                st = store.state()
                a = st.actors.get(actor) if actor else None
                snap["me"] = actor
                snap["me_here"] = bool(a and a.level0)
                snap["me_author"] = bool(a and a.right == "author")
                snap["machine"] = ws.machine()["name"]
                if st.project_id:
                    snap["run"] = runs.status(st.project_id)
                return self._json(200, snap)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "wrong host"})
            path = urlparse(self.path).path
            if path == "/api/session":   # the printed link's token, exchanged for a cookie
                if not secrets.compare_digest(self.headers.get("X-Baton-Token", ""), token):
                    return self._json(403, {"error": "wrong token"})
                return self._json(200, {"ok": True}, [("Set-Cookie", f"baton_session={cookie}; HttpOnly; SameSite=Strict; "
                                                                     "Path=/; Max-Age=31536000")])
            if not self._session():
                return self._json(403, {"error": "open baton from this machine: run `baton` in a terminal"})
            try:
                req = self._body()
                if path == "/api/machine":   # this machine's own settings: not an act in any project
                    if "address" in req:
                        app.set_address(ws, str(req.pop("address") or ""))
                    if "auto_update" in req:
                        d = ws.load()
                        d["auto_update"] = bool(req.pop("auto_update"))
                        ws.save(d)
                    return self._json(200, set_machine(ws, req) if req else machine_view(ws))
                if path == "/api/update":
                    from . import update
                    if req.get("action") == "install":
                        if not update.installed():
                            raise ValueError("this baton runs from a development checkout: update it with git pull")
                        c = update.check()
                        if not c["available"]:
                            return self._json(200, {**c, "installed": False})
                        version = update.apply(c["latest"])
                        update.remember(ws, updated_to=version, commit=c["latest"], error=None, restart=True)
                        if getattr(srv, "on_updated", None):
                            threading.Thread(target=srv.on_updated, daemon=True).start()
                        return self._json(200, {**c, "installed": True, "version": version,
                                                "restarting": not any(runs.running(p) for p in list(runs.procs))})
                    c = update.check()
                    update.remember(ws, checked=True, latest=c["latest"], error=None)
                    return self._json(200, {**c, "can_install": update.installed()})
                if path == "/api/me":
                    return self._json(200, {"me": app.set_me(ws, str(req.get("name", "")))})
                if path == "/api/connect":
                    out = app.connect(ws, str(req.get("invite", "")), str(req.get("note", "")))
                    directory.entries(fresh=True)
                    return self._json(200, {**out, "key": out["project"]})
                actor = who()
                if not actor:
                    return self._json(403, {"error": "say who you are first"})
                if path == "/api/project/create":
                    pid = app.create_project(ws, actor, str(req.get("repo", "")), str(req.get("name", "")),
                                             str(req.get("summary", "")), str(req.get("join") or "apply"),
                                             bool(req.get("steward_here", True)))
                    directory.entries(fresh=True)
                    return self._json(200, {"project": pid, "key": pid})
                if path == "/api/catalogs":
                    source = str(req.get("source", "")).strip()
                    if not source:
                        raise ValueError("give a catalog path or URL")
                    (ws.remove_catalog if req.get("action") == "remove" else ws.add_catalog)(source)
                    directory.entries(fresh=True)
                    return self._json(200, {"ok": True})
                if path == "/api/volunteer":
                    agent = str(req.get("actor") or ws.machine()["name"]).strip()
                    if not re.fullmatch(r"[\w.:@-]{1,80}", agent) or agent.startswith("human:"):
                        raise ValueError("name the machine with letters, digits and . : @ - _ (not human:...)")
                    offer = mc.offer(_probe(), ws.machine(), 180.0)
                    if not offer["models"]:
                        raise ValueError("this machine offers no model yet: choose some under This machine")
                    out = volunteer(ws, directory.entry(str(req["project"])), agent, offer, str(req.get("note", ""))[:2000])
                    directory.entries(fresh=True)
                    return self._json(200, out)
                m = route.match(path)
                if not m or m.group(2) == "state":
                    return self._json(404, {"error": "not found"})
                store = directory.store(m.group(1))
                st = store.state()
                op = m.group(2)
                if op == "run":   # this machine's own work: no project right needed beyond membership
                    if not st.project_id:
                        raise ValueError("only a project can run here")
                    out = runs.start(st.project_id, store.home, int(req.get("max_runs") or 20)) \
                        if req.get("action") == "start" else runs.stop(st.project_id)
                    return self._json(200, out)
                a = st.actors.get(actor)
                private_human = st.mode == "private" and actor in store.policy().humans
                if not (a and a.level0) and not private_human:
                    return self._json(403, {"error": f"{actor} is not level 0 (author or steward) of this project"})
                if op == "task":
                    out = {"id": app.post_package(ws, store, actor, req)}
                elif op == "invite":
                    out = {"url": app.make_invite(ws, store, actor)}
                elif op == "grant":
                    app.grant(store, actor, str(req["to"]), str(req.get("right") or "steward"))
                    out = {"ok": True}
                elif op == "revoke":
                    app.revoke(store, actor, str(req["actor"]))
                    out = {"ok": True}
                elif op == "discoverable":
                    app.set_discoverable(store, actor, bool(req.get("on")))
                    out = {"ok": True}
                else:
                    out = {"decide": decide, "link": add_link, "message": say}[op](store, actor, req)
                directory.entries(fresh=True)
                return self._json(200, out)
            except (KeyError, ValueError, TypeError, RuntimeError, NotImplementedError, OSError, PermissionError) as err:
                return self._json(400, {"error": str(err)})

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.directory = directory
    srv.runs = runs
    srv.on_updated = None   # set by serve(): restart into the new version
    return srv, token


def serve(ws: Workspace, actor: str | None, port: int = 8765, open_browser: bool = True, extra_home: Path | None = None,
          sync_every: float | None = None):
    try:
        srv, token = make_server(ws, actor, port=port, extra_home=extra_home, token=ui_secret(ws))
    except OSError:   # already running: just open it
        url = f"http://127.0.0.1:{port}/#token={ui_secret(ws)}"
        print(f"baton is already running: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    url = f"http://127.0.0.1:{srv.server_address[1]}/#token={token}"
    print(f"baton is running at {url}\n(keep this terminal open; Ctrl-C stops it)", flush=True)
    syncer = None
    if sync_every:
        directory = srv.directory

        def homes():
            return [e["home"] for e in directory.entries()[0] if e.get("home")]

        syncer = sync.Syncer(homes, sync_every, log=lambda line: print(time.strftime("%H:%M:%S ") + line, flush=True)).start()
        print(f"syncing local logs with their git remotes every {sync_every:g}s", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    restart = threading.Event()

    def restart_when_idle():
        while any(srv.runs.running(pid) for pid in list(srv.runs.procs)):
            time.sleep(30)   # machines are working: restart once they stop
        restart.set()
        srv.shutdown()

    srv.on_updated = restart_when_idle

    def updater():
        from . import update
        while not restart.is_set():
            if update.auto(ws, log=lambda line: print(time.strftime("%H:%M:%S ") + line, flush=True)):
                restart_when_idle()
                return
            time.sleep(update.EVERY)

    threading.Thread(target=updater, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if syncer:
            syncer.stop()
        if not restart.is_set():
            srv.runs.stop_all()
        srv.server_close()
    if restart.is_set():   # the new version takes over, on the same port; the page reconnects by itself
        import os
        import sys
        args = [a for a in sys.argv[1:] if a != "--no-open"]
        print("restarting into the new version", flush=True)
        os.execv(sys.executable, [sys.executable, "-m", "baton", *args, *([] if "ui" in args else ["ui"]), "--no-open"])
