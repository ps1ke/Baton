"""baton: coordination for many agents working on one project, peer-to-peer.

Agents join a tree of roles (each with its own model, effort and fan-out), take tasks from a shared board, lease the
paths and topics they work on, and propose anything that leaves the machine, which passes rate budgets, duplicate
checks and an approval gate before `execute` performs it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import forge
from . import manifest as mf
from . import policy as pol
from . import runner
from .store import Store


def _store(a) -> Store:
    return Store(a.home)


def _actor(a) -> str:
    actor = a.actor or os.environ.get("BATON_ACTOR")
    if not actor:
        sys.exit("baton: name the actor with --actor or BATON_ACTOR")
    return actor


def _outcome(store: Store, e) -> tuple[int, str]:
    s = store.state()
    if e.id in s.ignored:
        return 1, f"ignored: {s.ignored[e.id]}"
    if e.id in s.denied:
        return 1, f"denied: {s.denied[e.id]}"
    if e.id in s.proposals:
        p = s.proposals[e.id]
        ok = p.status in ("pending", "approved")
        return (0 if ok else 1), f"proposal {e.id[:12]}: {p.status}" + (f" ({p.reason})" if p.reason else "")
    return 0, f"ok {e.id[:12]}"


def _emit(a, kind, body) -> int:
    store = _store(a)
    e = store.append(kind, _actor(a), body)
    code, text = _outcome(store, e)
    if kind == "claim" and code == 0:
        text = f"lease {e.id[:12]} granted locally; it holds once peers have synced and agree"
    print(text)
    return code


def cmd_init(a):
    store = _store(a)
    store.init()
    print(f"initialised {store.home} (edit {store.home / 'policy.toml'}; `git init` it and add a remote to share)")
    return 0


def cmd_join(a):
    return _emit(a, "join", {"role": a.role, "parent": a.parent})


def cmd_task(a):
    s = _store(a).state()
    body = {"title": a.title, "detail": a.detail or "", "paths": a.path or [], "topics": a.topic or []}
    if a.role:
        body["role"] = a.role
    if a.parent:
        body["parent"] = _store(a).resolve(a.parent, s.tasks)
    return _emit(a, "task", body)


def cmd_tasks(a):
    s = _store(a).state()
    now = time.time()
    for t in sorted(s.tasks.values(), key=lambda x: x.created):
        status = s.task_status(t, now)
        if not a.all and status in ("done", "failed"):
            continue
        holder = s.leases[t.lease].actor if t.lease and status == "taken" else ""
        indent = "  " if t.parent else ""
        print(f"{indent}{t.id[:12]} {status:9} {('[' + t.role + '] ') if t.role else ''}{t.title}"
              + (f" <- {holder}" if holder else "") + (f" : {t.summary}" if t.summary and a.all else ""))
    return 0


def cmd_done(a):
    s = _store(a).state()
    body = {"task": _store(a).resolve(a.task_id, s.tasks), "outcome": a.outcome, "summary": a.summary or ""}
    return _emit(a, "done", body)


def cmd_run(a):
    run = runner.Run(_store(a), _actor(a), a.task_id)
    if a.dry_run:
        print(" ".join(repr(x) if " " in x or "\n" in x else x for x in run.command()))
        return 0
    return 0 if run.go(timeout=pol.duration(a.timeout) if a.timeout else None) == "done" else 1


def cmd_claim(a):
    if a.task_id:
        s = _store(a).state()
        return _emit(a, "claim", {"task_id": _store(a).resolve(a.task_id, s.tasks)})
    if not a.task:
        sys.exit("baton: claim needs a description or --task-id")
    body = {"task": a.task, "paths": a.path or [], "topics": a.topic or []}
    if a.ttl:
        body["ttl"] = pol.duration(a.ttl)
    store = _store(a)
    clash = store.state().conflicts(tuple(body["paths"]), tuple(body["topics"]), time.time(), actor=_actor(a))
    if clash:
        for l in clash:
            print(f"conflict: lease {l.id[:12]} by {l.actor} ({l.task}) on {', '.join(l.paths + l.topics)}")
        return 1
    return _emit(a, "claim", body)


def cmd_renew(a):
    s = _store(a).state()
    return _emit(a, "renew", {"lease": _store(a).resolve(a.lease, s.leases)})


def cmd_release(a):
    s = _store(a).state()
    return _emit(a, "release", {"lease": _store(a).resolve(a.lease, s.leases)})


def cmd_check(a):
    """Before starting: is anyone on these paths, here or in an open pull request?"""
    scope = a.path or []
    s = _store(a).state()
    found = 0
    for l in s.conflicts(tuple(scope), tuple(a.topic or []), time.time(), actor=a.actor):
        print(f"lease  {l.id[:12]} {l.actor}: {l.task} [{', '.join(l.paths + l.topics)}]")
        found += 1
    if a.repo and scope:
        for pr in forge.touching(forge.open_pulls(a.repo), scope):
            print(f"pr     #{pr['number']} {pr['author']}: {pr['title']} [{', '.join(pr['files'][:4])}]")
            found += 1
    print("clear" if not found else f"{found} overlapping item(s): coordinate before starting")
    return 1 if found else 0


def cmd_propose(a):
    payload = {}
    if a.repo:
        payload["repo"] = a.repo
    if a.body_file:
        payload["body"] = Path(a.body_file).read_text(encoding="utf-8")
    if a.path:
        payload["paths"] = a.path
    return _emit(a, "propose", {"action": a.action, "target": a.target, "payload": payload})


def cmd_decide(a):
    s = _store(a).state()
    pid = _store(a).resolve(a.proposal, s.proposals)
    body = {"proposal": pid}
    if a.cmd == "reject" and a.reason:
        body["reason"] = a.reason
    return _emit(a, a.cmd, body)


def cmd_execute(a):
    store = _store(a)
    s = store.state()
    pid = store.resolve(a.proposal, s.proposals)
    p = s.proposals[pid]
    if p.status != "approved":
        print(f"proposal {pid[:12]} is {p.status}; only an approved proposal runs")
        return 1
    if not a.yes:
        print(f"would run {p.action} on {p.target} for {p.actor}:\n{json.dumps(p.payload, indent=1)}\n"
              "(pass --yes to run it)")
        return 0
    result = forge.execute(p.action, p.target, p.payload)
    return _emit(a, "executed", {"proposal": pid, "result": result})


def cmd_status(a):
    store = _store(a)
    ordered, held, bad = store.load()
    s = store.state()
    now = time.time()

    def tree(name, depth):
        x = s.actors[name]
        tier = f" {x.model} effort={x.effort}" if x.model else ""
        print(f"{'  ' * depth}{name} [{x.role}]{tier}")
        for c in x.children:
            tree(c, depth + 1)

    print(f"{len(ordered)} events" + (f", {len(held)} held back" if held else "") + (f", {len(bad)} bad files" if bad else ""))
    print("tree:")
    for name, x in s.actors.items():
        if x.parent is None:
            tree(name, 1)
    print("leases:")
    for l in s.active_leases(now):
        print(f"  {l.id[:12]} {l.actor}: {l.task} [{', '.join(l.paths + l.topics)}] {int(l.expiry - now)}s left")
    print("proposals:")
    for p in s.proposals.values():
        if a.all or p.status in ("pending", "approved"):
            print(f"  {p.id[:12]} {p.status:12} {p.action} {p.target} by {p.actor}" + (f" ({p.reason})" if p.reason else ""))
    return 0


def cmd_whoami(a):
    s = _store(a).state()
    x = s.actors.get(_actor(a))
    if x is None:
        print("not joined")
        return 1
    print(json.dumps({"actor": x.name, "role": x.role, "parent": x.parent, "model": x.model, "effort": x.effort,
                      "children": x.children}))
    return 0


def cmd_ui(a):
    from . import ui
    from .workspace import Workspace
    ui.serve(Workspace(), a.actor or os.environ.get("BATON_ACTOR"), port=a.port, open_browser=not a.no_open,
             extra_home=Path(a.home))
    return 0


# -- projects, catalogs and joining (spec/PROTOCOL.md sections 6-8) --------------------------------------------------
def _project_fields(a) -> dict:
    f = {}
    for k in ("name", "summary", "description", "homepage", "repo", "join"):
        v = getattr(a, k, None)
        if v is not None:
            f[k] = v
    if getattr(a, "log_git", None):
        f["log"] = {"git": a.log_git, **({"branch": a.log_branch} if a.log_branch else {})}
    if getattr(a, "inbox", None):
        f["inbox"] = {"type": "git" if mf.git_url_ok(a.inbox) and not a.inbox.startswith("http") else "url", "url": a.inbox}
    if getattr(a, "tag", None):
        f["tags"] = a.tag
    if getattr(a, "need", None):
        needs = []
        for spec in a.need:   # role[:skill,skill][@hardware,hardware][*count]
            count = None
            if "*" in spec:
                spec, count = spec.rsplit("*", 1)
            hw = []
            if "@" in spec:
                spec, h = spec.split("@", 1)
                hw = [x for x in h.split(",") if x]
            role, _, sk = spec.partition(":")
            n = {"role": role, "skills": [x for x in sk.split(",") if x]}
            if hw:
                n["hardware"] = hw
            if count:
                n["count"] = int(count)
            needs.append(n)
        f["needs"] = needs
    return f


def cmd_project(a):
    from .workspace import Workspace, manifest_of, publish
    store = _store(a)
    if a.action in ("create", "set"):
        s = store.state()
        if a.action == "create" and s.project_id:
            sys.exit(f"baton: this log is already project {s.project_id[:12]}; use `project set`")
        if a.action == "set" and not s.project_id:
            sys.exit("baton: no project here yet; use `project create`")
        code = _emit(a, "project", _project_fields(a))
        s = store.state()
        if code == 0 and a.action == "create":
            Workspace().add_project(s.project_id, store.home, trusted=True)
            print(f"project {s.project_id} created and added to your workspace")
        return code
    if a.action == "link":
        s = store.state()
        link = {"rel": a.rel}
        for k in ("project", "manifest", "name"):
            if getattr(a, "link_" + k):
                link[k] = getattr(a, "link_" + k)
        links = [l for l in s.project.get("links", []) if l != link] + [link]
        return _emit(a, "project", {"links": links})
    m = manifest_of(store)
    if m is None:
        sys.exit("baton: this log has no project; use `project create`")
    if a.action == "publish":
        publish(Path(a.catalog), m)
        print(f"published {m['name']} into {a.catalog}")
        return 0
    print(json.dumps(m, indent=1, ensure_ascii=False))
    return 0


def cmd_catalog(a):
    from .workspace import Workspace
    ws = Workspace()
    if a.action == "add":
        ws.add_catalog(a.source)
    elif a.action == "remove":
        ws.remove_catalog(a.source)
    elif a.action == "new":
        Path(a.source).write_text(json.dumps(mf.new_catalog(a.name or Path(a.source).stem, a.description or ""),
                                             indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {a.source}; add projects with `baton project publish {a.source}`")
        return 0
    for c in ws.load()["catalogs"]:
        print(c)
    return 0


def cmd_discover(a):
    from .workspace import Workspace
    entries, problems = Workspace().discover()
    for e in entries:
        m = e["manifest"]
        if a.tag and not set(a.tag) <= set(m.get("tags", [])):
            continue
        needs = ", ".join(n["role"] + (":" + "/".join(n.get("skills", [])) if n.get("skills") else "") for n in m.get("needs", []))
        act = m.get("activity", {})
        print(f"{m['id'][:12]} {m['name']}  [{'local' if e['local'] else ', '.join(e['sources'])}] join={m.get('join', 'apply')}")
        if m.get("summary"):
            print(f"    {m['summary']}")
        if needs or act:
            print(f"    needs: {needs or '-'}" + (f"   open tasks: {act.get('open_tasks')}, agents: {act.get('agents')}" if act else ""))
    for p in problems:
        print(f"! {p}", file=sys.stderr)
    return 0


def _find_manifest(project: str):
    from .workspace import Workspace
    entries, _ = Workspace().discover()
    hits = [e for e in entries if e["manifest"]["id"].startswith(project)]
    if len(hits) != 1:
        raise KeyError(f"{project!r} matches {len(hits)} known projects")
    return hits[0]


def cmd_volunteer(a):
    offer = {}
    for k in ("skill", "model", "hardware"):
        if getattr(a, k):
            offer[k + ("s" if k != "hardware" else "")] = getattr(a, k)
    from .workspace import Workspace, volunteer
    out = volunteer(Workspace(), _find_manifest(a.project), _actor(a), a.role, a.parent, offer, a.note or "")
    print(f"applied ({out['event'][:12]}); log at {out['home']}\n{out['deliver']}")
    return 0


def cmd_applications(a):
    s = _store(a).state()
    for x in s.applications.values():
        if a.all or x.status == "pending":
            print(f"{x.actor} {x.status} as {x.role}" + (f" under {x.parent}" if x.parent else "")
                  + (f" offer={json.dumps(x.offer)}" if x.offer else "") + (f" : {x.note}" if x.note else ""))
    return 0


def cmd_admit(a):
    body = {"applicant": a.applicant}
    if a.cmd == "admit":
        if a.role:
            body["role"] = a.role
        if a.parent:
            body["parent"] = a.parent
    elif a.reason:
        body["reason"] = a.reason
    return _emit(a, a.cmd, body)


def cmd_import(a):
    store = _store(a)
    for f in a.files:
        e = store.import_file(f)
        print(f"imported {e.kind} {e.id[:12]} by {e.actor}")
    return 0


def cmd_export_event(a):
    found, _ = _store(a).read()
    print(json.dumps(found[_store(a).resolve(a.event, found)].to_dict(), indent=1, ensure_ascii=False))
    return 0


def cmd_workspace(a):
    from .workspace import Workspace
    ws = Workspace()
    if a.action == "runner":
        cmd = [x for x in a.command if x != "--"]
        if cmd:
            ws.set_runner(cmd)
        print(json.dumps(ws.runner()))
        return 0
    print(json.dumps({"dir": str(ws.dir), **ws.load()}, indent=1))
    return 0


def cmd_sync(a):
    print(_store(a).sync(a.remote, a.branch))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="baton", description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", default=os.environ.get("BATON_HOME", ".baton"), help="the log folder (BATON_HOME)")
    ap.add_argument("--actor", help="who acts (BATON_ACTOR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("join"); p.add_argument("role"); p.add_argument("--parent"); p.set_defaults(fn=cmd_join)
    p = sub.add_parser("task", help="post a task to the board"); p.add_argument("title"); p.add_argument("--detail")
    p.add_argument("--path", action="append"); p.add_argument("--topic", action="append")
    p.add_argument("--role", help="only this role may take it"); p.add_argument("--parent", help="split from this task")
    p.set_defaults(fn=cmd_task)
    p = sub.add_parser("tasks", help="the board"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_tasks)
    p = sub.add_parser("done", help="report how a task you hold ended"); p.add_argument("task_id")
    p.add_argument("--outcome", choices=("done", "failed", "escalated"), default="done"); p.add_argument("--summary")
    p.set_defaults(fn=cmd_done)
    p = sub.add_parser("run", help="take a task and run the policy's agent on it")
    p.add_argument("task_id", nargs="?", help="default: the oldest open task for your role")
    p.add_argument("--dry-run", action="store_true", help="print the agent command and take nothing")
    p.add_argument("--timeout", help="e.g. 30m"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("claim"); p.add_argument("task", nargs="?"); p.add_argument("--task-id")
    p.add_argument("--path", action="append")
    p.add_argument("--topic", action="append"); p.add_argument("--ttl"); p.set_defaults(fn=cmd_claim)
    for name, fn in (("renew", cmd_renew), ("release", cmd_release)):
        p = sub.add_parser(name); p.add_argument("lease"); p.set_defaults(fn=fn)
    p = sub.add_parser("check"); p.add_argument("--path", action="append"); p.add_argument("--topic", action="append")
    p.add_argument("--repo", help="also look at this GitHub repository's open pull requests"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("propose"); p.add_argument("action"); p.add_argument("--target", default="")
    p.add_argument("--repo"); p.add_argument("--body-file"); p.add_argument("--path", action="append")
    p.set_defaults(fn=cmd_propose)
    for name in ("approve", "reject"):
        p = sub.add_parser(name); p.add_argument("proposal"); p.set_defaults(fn=cmd_decide)
        if name == "reject":
            p.add_argument("--reason")
    p = sub.add_parser("execute"); p.add_argument("proposal"); p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_execute)
    p = sub.add_parser("status"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_status)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    p = sub.add_parser("project", help="create, change, link and publish this log's project")
    p.add_argument("action", choices=("create", "set", "show", "link", "publish"))
    p.add_argument("catalog", nargs="?", help="publish: the catalog file to add the manifest to")
    for k in ("name", "summary", "description", "homepage", "repo"):
        p.add_argument("--" + k)
    p.add_argument("--join", choices=mf.JOIN_MODES); p.add_argument("--log-git"); p.add_argument("--log-branch")
    p.add_argument("--inbox", help="where applications are sent: a git URL or an http(s) URL")
    p.add_argument("--tag", action="append")
    p.add_argument("--need", action="append", help="role[:skill,skill][@hardware][*count], e.g. worker:python,c@gpu*3")
    p.add_argument("--rel", choices=mf.RELS, default="related")
    p.add_argument("--link-project", help="link: the other project's id"); p.add_argument("--link-manifest")
    p.add_argument("--link-name")
    p.set_defaults(fn=cmd_project)
    p = sub.add_parser("catalog", help="the catalogs you follow, or make a new catalog file")
    p.add_argument("action", choices=("list", "add", "remove", "new")); p.add_argument("source", nargs="?")
    p.add_argument("--name"); p.add_argument("--description"); p.set_defaults(fn=cmd_catalog)
    p = sub.add_parser("discover", help="every project your workspace and catalogs know")
    p.add_argument("--tag", action="append"); p.set_defaults(fn=cmd_discover)
    p = sub.add_parser("volunteer", help="apply to help a project, as one of your agents")
    p.add_argument("project", help="project id prefix"); p.add_argument("--role", default="worker")
    p.add_argument("--parent"); p.add_argument("--skill", action="append"); p.add_argument("--model", action="append")
    p.add_argument("--hardware", action="append"); p.add_argument("--note"); p.set_defaults(fn=cmd_volunteer)
    p = sub.add_parser("applications"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_applications)
    for name in ("admit", "decline"):
        p = sub.add_parser(name); p.add_argument("applicant")
        if name == "admit":
            p.add_argument("--role"); p.add_argument("--parent")
        else:
            p.add_argument("--reason")
        p.set_defaults(fn=cmd_admit)
    p = sub.add_parser("import", help="take in event files sent by other means"); p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("export-event"); p.add_argument("event"); p.set_defaults(fn=cmd_export_event)
    p = sub.add_parser("workspace", help="show the workspace, or set your own agent command")
    p.add_argument("action", nargs="?", choices=("show", "runner"), default="show")
    p.add_argument("command", nargs=argparse.REMAINDER); p.set_defaults(fn=cmd_workspace)
    p = sub.add_parser("ui", help="a local dashboard; approve and reject from the browser")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--no-open", action="store_true")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("sync"); p.add_argument("--remote", default="origin"); p.add_argument("--branch", default="main")
    p.set_defaults(fn=cmd_sync)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (KeyError, ValueError, LookupError, RuntimeError, NotImplementedError, OSError) as err:
        print(f"baton: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
