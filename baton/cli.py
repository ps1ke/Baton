"""baton: a peer-to-peer network of agents working on one project.

Machines offer what they have (the models the user allows, how many packages may run at once, the hardware); the
network gives each work package to a machine and a model: orchestration per level, implementation, tests, reviews,
investigations. Every change is tested and reviewed before it counts, leases keep agents off each other's files, and
anything that leaves the machine waits for a human.
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
from .store import Store


def _store(a) -> Store:
    return Store(a.home)


def _ws():
    from .workspace import Workspace
    return Workspace()


def _actor(a) -> str:
    """--actor, BATON_ACTOR, else this machine's member name."""
    return a.actor or os.environ.get("BATON_ACTOR") or _ws().machine()["name"]


def _probe(a=None) -> dict:
    from . import machine as mc
    return mc.probe([a.home] if a is not None else [], limits=bool(getattr(a, "limits", False)))


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


def _emit(a, kind, body, actor=None) -> int:
    store = _store(a)
    e = store.append(kind, actor or _actor(a), body)
    code, text = _outcome(store, e)
    if kind == "claim" and code == 0:
        text = f"lease {e.id[:12]} granted locally; it holds once peers have synced and agree"
    print(text)
    return code


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h" if sec < 86400 else f"{sec // 86400}d"


def cmd_init(a):
    store = _store(a)
    store.init()
    print(f"initialised {store.home} (put yourself in [gate] humans in {store.home / 'policy.toml'}; "
          "`git init` it and add a remote to share)")
    return 0


# -- this machine ----------------------------------------------------------------------------------------------------
def _machine_report(p: dict, cfg: dict) -> str:
    from . import machine as mc
    c, m = p["cpu"], p["memory"]
    lines = [f"machine {cfg['name']}  ({p['os']})",
             f"  cpu     {c['model'] or c['arch']}: {c['physical_cores'] or '?'} cores / {c['logical_cores']} threads, "
             f"{c['utilization_percent'] if c['utilization_percent'] is not None else '?'}% busy"
             + (f", load {' '.join(str(x) for x in c['load'])}" if c["load"] else ""),
             f"  memory  {m['total_gb']} GB, {m['available_gb']} GB available"]
    for g in p["gpus"]:
        vram = f"{g['vram_gb']} GB" + (f" ({g['vram_used_gb']} used)" if g.get("vram_used_gb") is not None else "") \
            if g.get("vram_gb") else "memory unknown"
        lines.append(f"  gpu     {g['name']}: {vram}"
                     + (f", {g['utilization_percent']:.0f}% busy" if g.get("utilization_percent") is not None else ""))
    if not p["gpus"]:
        lines.append("  gpu     none found")
    for d in p["disk"]:
        lines.append(f"  disk    {d['path']}: {d['free_gb']} GB free of {d['total_gb']} GB")
    lines.append("agents:")
    for ag in p["agents"]:
        if ag.get("kind") == "local":
            state = f"local server at {ag['url']}"
        elif not ag["installed"]:
            state = "not installed"
        else:
            state = ("logged in" if ag["logged_in"] else "not logged in" if ag["logged_in"] is False else "login unknown") \
                + (f", plan {ag['plan']}" if ag.get("plan") else "") + (f" ({ag['auth']})" if ag.get("auth") else "") \
                + (f", {ag['version']}" if ag.get("version") else "")
        lim = ag.get("limits") or {}
        if lim.get("week_percent") is not None or lim.get("primary_percent") is not None:
            state += "  usage: " + ", ".join(f"{k.replace('_percent', '')} {v}%" for k, v in lim.items()
                                             if k.endswith("_percent") and v is not None)
        lines.append(f"  {ag['id']:9} {state}")
    lines.append(f"models (allowed ones are offered; slots: {cfg.get('slots') or str(mc.suggested_slots(p)) + ' (suggested)'}):")
    for x in mc.models(p, cfg):
        mark = "[x]" if x["allowed"] else "[ ]"
        note = "" if x["usable"] else f"  — {x['why']}"
        lines.append(f"  {mark} {x['ref']:40} {x['tier']:9}{' (from the catalog, not confirmed)' if x['unverified'] else ''}{note}")
    lines.append("change: baton machine allow|deny <provider:model> ..., tier <ref> <tier>, slots <n>, cmd <provider> -- <cmd>")
    return "\n".join(lines)


def cmd_machine(a):
    from . import machine as mc
    ws = _ws()
    cfg = ws.machine()
    act, args = a.action, a.args
    if act in ("allow", "deny"):
        p = _probe(a)
        known = {m["ref"] for m in mc.models(p, cfg)}
        refs = [r for r in args if r != "--"]
        if refs == ["all"]:
            refs = [m["ref"] for m in mc.models(p, cfg) if m["usable"]]
        unknown = [r for r in refs if r not in known]
        if unknown and act == "allow":
            sys.exit(f"baton: not found on this machine: {', '.join(unknown)} (see `baton machine`)")
        allowed = [r for r in cfg["allowed"] if r not in refs] + (refs if act == "allow" else [])
        cfg = ws.set_machine(allowed=allowed)
    elif act == "tier":
        if len(args) != 2:
            sys.exit("baton: machine tier <provider:model> <fast|standard|strong|frontier>")
        cfg = ws.set_machine(tiers={**cfg["tiers"], args[0]: args[1]})
    elif act == "slots":
        if len(args) != 1 or not args[0].isdigit():
            sys.exit("baton: machine slots <n>   (how many work packages may run here at once)")
        cfg = ws.set_machine(slots=int(args[0]))
    elif act == "name":
        cfg = ws.set_machine(name=args[0])
    elif act == "cmd":
        if not args:
            sys.exit("baton: machine cmd <provider> -- <command ...>   ({model} {effort} {prompt} are filled in)")
        cmd = [x for x in args[1:] if x != "--"]
        cfg = ws.set_machine(commands={**cfg["commands"], args[0]: cmd} if cmd else
                             {k: v for k, v in cfg["commands"].items() if k != args[0]})
    elif act == "hardware":
        cfg = ws.set_machine(share_hardware=args[:1] != ["hide"])
    p = _probe(a)
    if a.json:
        print(json.dumps({"config": cfg, "probe": p, "models": mc.models(p, cfg),
                          "offer": mc.offer(p, cfg, _store(a).policy().presence_ttl)}, indent=1))
    else:
        print(_machine_report(p, cfg))
    return 0


def _offer(a) -> dict:
    from . import machine as mc
    return mc.offer(_probe(a), _ws().machine(), _store(a).policy().presence_ttl)


def cmd_join(a):
    return _emit(a, "join", {"offer": _offer(a)})


def cmd_offer(a):
    o = _offer(a)
    if not o["models"]:
        print("this machine offers no model yet: `baton machine allow <provider:model>`")
    return _emit(a, "offer", o)


# -- work packages ---------------------------------------------------------------------------------------------------
def _package_from_args(a, s, store) -> dict:
    if a.file:
        body = json.loads(Path(a.file).read_text(encoding="utf-8"))
    else:
        body = {}
    if a.title:
        body["title"] = a.title
    for k in ("kind", "goal", "size"):
        if getattr(a, k):
            body[k] = getattr(a, k)
    if a.context or a.detail:
        body["context"] = a.context or a.detail
    for k, v in (("paths", a.path), ("topics", a.topic), ("deliverables", a.deliverable),
                 ("constraints", a.constraint), ("inputs", a.input)):
        if v:
            body[k] = v
    acc = list(body.get("acceptance", []))
    acc += [{"criterion": c} for c in a.accept or []]
    for c in a.check or []:
        name, _, cmd = c.partition("=")
        acc.append({"check": name, "run": cmd} if cmd else {"check": f"check{len(acc) + 1}", "run": name})
    if acc:
        body["acceptance"] = acc
    needs = dict(body.get("needs", {}))
    if a.tier:
        needs["tier"] = a.tier
    if a.hardware:
        needs["hardware"] = a.hardware
    if needs:
        body["needs"] = needs
    parent = a.parent or (os.environ.get("BATON_TASK") if getattr(a, "under_current", True) else None)
    if parent:
        body["parent"] = store.resolve(parent, s.tasks)
    if a.depends_on:
        body["depends_on"] = [store.resolve(d, s.tasks) for d in a.depends_on]
    if a.base:
        body["base"] = a.base
    elif not parent:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        if r.returncode == 0 and len(r.stdout.strip()) in (40, 64):
            body["base"] = r.stdout.strip()   # a root package starts from the commit you are on
    return body


def cmd_task(a):
    store = _store(a)
    s = store.state()
    body = _package_from_args(a, s, store)
    if not body.get("title"):
        sys.exit("baton: a work package needs a title (or --file package.json)")
    return _emit(a, "task", body)


def _status_line(s, t, now) -> str:
    st = s.task_status(t, now)
    lease = s.leases.get(t.lease) if t.lease else None
    who = f" <- {lease.actor} ({lease.model})" if lease and st == "taken" else ""
    return f"{'  ' * t.level}{t.id[:12]} {st:9} [{t.kind} L{t.level} {t.tier}] {t.title}{who}"


def cmd_tasks(a):
    s = _store(a).state()
    now = time.time()

    def walk(t):
        st = s.task_status(t, now)
        if a.all or st not in ("done", "failed"):
            print(_status_line(s, t, now) + (f" : {t.summary}" if t.summary and a.all else ""))
        for c in t.subtasks + t.checks:
            walk(s.tasks[c])

    for t in sorted(s.tasks.values(), key=lambda x: x.created):
        if t.parent is None and t.of is None:
            walk(t)
    return 0


def cmd_show(a):
    store = _store(a)
    s = store.state()
    t = s.tasks[store.resolve(a.task_id, s.tasks)]
    d = {"id": t.id, "status": s.task_status(t, time.time()), "kind": t.kind, "level": t.level, "tier": t.tier,
         "title": t.title, "goal": t.goal, "context": t.detail, "paths": list(t.paths), "topics": list(t.topics),
         "acceptance": t.acceptance, "deliverables": t.deliverables, "constraints": t.constraints,
         "size": t.size, "parent": t.parent, "of": t.of, "depends_on": list(t.depends_on), "by": t.by,
         "subtasks": t.subtasks, "checks": t.checks, "runs": t.runs, "rework": t.rework, "feedback": t.feedback,
         "outcome": t.outcome, "summary": t.summary, "report": t.report}
    print(json.dumps({k: v for k, v in d.items() if v not in (None, "", [], {})}, indent=1, ensure_ascii=False))
    return 0


def _report_from_args(a) -> dict:
    r = json.loads(Path(a.report).read_text(encoding="utf-8")) if a.report else {}
    if a.change:
        r["changes"] = r.get("changes", []) + [{"path": p} for p in a.change]
    checks = list((r.get("verification") or {}).get("checks", []))
    for c in a.check or []:   # name=status[:command]
        name, _, rest = c.partition("=")
        status, _, cmd = rest.partition(":")
        if status not in ("passed", "failed", "not_run"):
            sys.exit(f"baton: --check {c!r}: use name=passed|failed|not_run[:command]")
        checks.append({"name": name, "status": status, **({"command": cmd} if cmd else {})})
    if checks or a.verification:
        status = a.verification or ("passed" if checks and all(c["status"] == "passed" for c in checks)
                                    else "failed" if any(c["status"] == "failed" for c in checks) else "not_run")
        r["verification"] = {"status": status, "checks": checks}
    for f in a.finding or []:   # severity:text[@path[:line]]
        sev, _, rest = f.partition(":")
        text, _, where = rest.rpartition("@") if "@" in rest else (rest, "", "")
        path, _, line = where.partition(":")
        item = {"severity": sev, "text": text}
        if path:
            item["path"] = path
        if line.isdigit():
            item["line"] = int(line)
        r.setdefault("findings", []).append(item)
    if a.verdict:
        r["verdict"] = a.verdict
    if a.notes:
        r["notes"] = a.notes
    return r


def cmd_done(a):
    store = _store(a)
    s = store.state()
    body = {"task": store.resolve(a.task_id, s.tasks), "outcome": a.outcome, "summary": a.summary or ""}
    report = _report_from_args(a)
    wt, base = os.environ.get("BATON_WORKTREE"), os.environ.get("BATON_BASE")
    task = s.tasks[body["task"]]
    if wt and base and a.outcome == "done" and task.kind in ("implement", "orchestrate"):
        from . import codeflow   # the change itself travels with the report, as a patch
        report = {**codeflow.capture(store, task, Path(wt), base), **report}
    if report:
        body["report"] = report
    return _emit(a, "done", body)


def cmd_assign(a):
    from . import network
    s = _store(a).state()
    now = time.time()
    picks = network.assign(s, s.policy, now)
    present = [m for m in s.members() if m.present(now)]
    print(f"{len(present)} member(s) present: " + ", ".join(
        f"{m.name} ({len(m.offer.get('models', []))} models, {m.offer.get('slots', 0)} slots)" for m in present))
    for t in s.open_tasks(now):
        p = picks.get(t.id)
        print(f"{t.id[:12]} [{t.kind} L{t.level} {t.tier}] {t.title}  ->  "
              + (f"{p.member} with {p.model} ({p.tier})" if p else "nobody fits right now"))
    return 0


def cmd_run(a):
    """Run one package the network assigned to this machine, now, in the foreground."""
    from . import network, runner
    store = _store(a)
    me = _actor(a)
    cfg = _ws().machine()
    p = _probe(a)
    s = store.state()
    now = time.time()
    picks = network.assign(s, s.policy, now)
    mine = [(pid, pk) for pid, pk in picks.items() if pk.member == me]
    if a.task_id:
        pid = store.resolve(a.task_id, s.tasks)
        mine = [(x, pk) for x, pk in mine if x == pid]
        if not mine:
            print(network.why_not(s, s.policy, now, pid, me))
            return 1
    if not mine:
        print(f"the network has nothing for {me} right now (is it present? `baton offer`)")
        return 1
    pid, pick = sorted(mine, key=lambda kv: s.tasks[kv[0]].created)[0]
    run = runner.Run(store, me, pid, pick.model, p, cfg)
    if a.dry_run:
        print(" ".join(repr(x) if " " in x or "\n" in x else x for x in run.command()))
        return 0
    out = run.go(timeout=pol.duration(a.timeout) if a.timeout else None)
    return 0 if out in ("done", "checking", "waiting") else 1


def cmd_swarm(a):
    from . import swarm
    store = _store(a)
    ws = _ws()
    cfg = ws.machine()
    if a.slots is not None:
        cfg = {**cfg, "slots": a.slots}
    me = _actor(a)
    s = store.state()
    p = _probe(a)
    if me not in s.actors:
        from . import machine as mc
        o = mc.offer(p, cfg, store.policy().presence_ttl)
        if s.join_mode != "open":
            sys.exit(f"baton: this project takes members by application: `baton volunteer <project>` as {me} first")
        e = store.append("join", me, {"offer": o})
        if e.id in store.state().ignored:
            sys.exit(f"baton: could not join: {store.state().ignored[e.id]}")
        print(f"{me} joined")
    logs = Path(a.transcripts) if a.transcripts else store.home / "runs"
    p2p_cfg = None
    if a.p2p:
        p2p_cfg = {"listen": a.p2p, "advertise": a.advertise or a.p2p, "every": pol.duration(a.p2p_every),
                   "peers": dict(x.split("=", 1) for x in (a.peer or []))}
    sw = swarm.Swarm(store, me, cfg, p, max_runs=a.max_runs, timeout=pol.duration(a.timeout) if a.timeout else None,
                     once=a.once, interval=pol.duration(a.interval), sync_every=pol.duration(a.sync) if a.sync else None,
                     transcripts=logs, log=lambda line: print(time.strftime("%H:%M:%S ") + line, flush=True),
                     ws=ws, p2p=p2p_cfg)
    results = swarm.serve(sw)
    print(f"swarm: {len(results)} run(s); transcripts in {logs}")
    return 0


# -- leases, checks, the gate -----------------------------------------------------------------------------------------
def cmd_claim(a):
    store = _store(a)
    if a.task_id:
        s = store.state()
        body = {"task_id": store.resolve(a.task_id, s.tasks)}
        if a.model:
            body["model"] = a.model
        return _emit(a, "claim", body)
    if not a.task:
        sys.exit("baton: claim needs a description or --task-id")
    body = {"task": a.task, "paths": a.path or [], "topics": a.topic or []}
    if a.ttl:
        body["ttl"] = pol.duration(a.ttl)
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
    result = execute_action(store, p)
    return _emit(a, "executed", {"proposal": pid, "result": result})


def execute_action(store, p) -> dict:
    if p.action == "push":
        from . import codeflow
        repo = codeflow.checkout(_ws(), store)
        if repo is None:
            raise ValueError("this log has no repository to push to")
        return codeflow.push(store, repo, p.payload)
    return forge.execute(p.action, p.target, p.payload)


def cmd_status(a):
    from . import network
    store = _store(a)
    ordered, held, bad = store.load()
    s = store.state()
    now = time.time()
    print(f"{len(ordered)} events" + (f", {len(held)} held back" if held else "") + (f", {len(bad)} bad files" if bad else ""))
    print("members:")
    used = network.load(s, now)
    for m in s.members():
        o = m.offer
        here = f"present, {used.get(m.name, 0)}/{o.get('slots', 0)} slots busy" if m.present(now) else \
            (f"last seen {_ago(now - m.offer_at)} ago" if m.offer_at else "never offered")
        models = ", ".join(f"{x['id']} ({x['tier']})" for x in o.get("models", []))
        print(f"  {m.name}: {here}" + (f" — {models}" if models else ""))
    print("leases:")
    for l in s.active_leases(now):
        print(f"  {l.id[:12]} {l.actor} {l.model}: {l.task} [{', '.join(l.paths + l.topics)}] {int(l.expiry - now)}s left")
    print("proposals:")
    for p in s.proposals.values():
        if a.all or p.status in ("pending", "approved"):
            print(f"  {p.id[:12]} {p.status:12} {p.action} {p.target} by {p.actor}" + (f" ({p.reason})" if p.reason else ""))
    return 0


def _node(a, store=None):
    from . import p2p
    ws = _ws()
    return p2p.Node(store or _store(a), _actor(a), ws.dir / "tls")


def cmd_peer(a):
    """Run this machine's peer node for the log (`serve`), or exchange once with one node (`sync`)."""
    from . import p2p
    import threading
    store = _store(a)
    node = _node(a, store)
    node.log = lambda line: print(time.strftime("%H:%M:%S ") + line, flush=True)
    if a.action == "sync":
        addr, _, fp = a.target.partition("=")
        print(json.dumps(node.sync_with(addr, fp)))
        return 0
    if a.action == "id":
        print(json.dumps({"actor": node.actor, "key": store.keyring.public(node.actor),
                          "key_fingerprint": __import__("baton.crypto", fromlist=["x"]).fingerprint(store.keyring.public(node.actor)),
                          "cert": node.fp}, indent=1))
        return 0
    host, _, port = a.listen.rpartition(":")
    real = node.serve(host, int(port))
    node.addr = a.advertise
    for x in a.peer or []:
        addr, _, fp = x.partition("=")
        node.extra[addr] = fp
    print(f"peer node for {store.home} as {node.actor} on {host}:{real}; certificate {node.fp}", flush=True)
    stop = threading.Event()
    try:
        node.run(pol.duration(a.every), stop)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
    return 0


def cmd_invite(a):
    from . import p2p
    store = _store(a)
    s = store.state()
    if not s.project_id:
        sys.exit("baton: only a project (with a genesis) can invite")
    me = s.actors.get(_actor(a))
    if not me or not me.level0:
        sys.exit("baton: invites come from the author or a steward (level 0)")
    node = _node(a, store)
    token = node.invites.new(s.project_id, pol.duration(a.ttl))
    print(p2p.invite_url(a.addr, s.project_id, node.fp, token))
    return 0


def cmd_connect(a):
    """Use an invite: fetch the project's public card from the inviter, apply with this machine, deliver it."""
    from . import p2p
    from . import machine as mc
    inv = p2p.parse_invite(a.invite)
    ws = _ws()
    home = ws.dir / "projects" / inv["project"][:16]
    store = Store(home)
    store.init()
    node = _node(a, store)
    r = node.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
    s = store.state()
    if s.project_id != inv["project"]:
        sys.exit("baton: the inviter sent another project's card; not applying")
    ws.add_project(s.project_id, home, trusted=False)
    me = _actor(a)
    if r["access"] == "full":
        print(f"{me} is already a member; log at {home}")
        return 0
    o = mc.offer(_probe(a), ws.machine(), s.policy.presence_ttl)
    if not o["models"]:
        sys.exit("baton: this machine offers no model yet: `baton machine allow <provider:model>`")
    kind = "join" if s.join_mode == "open" else "apply"
    store.append(kind, me, {"offer": o, **({"note": a.note} if a.note and kind == "apply" else {})})
    node.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
    print(f"{'joined' if kind == 'join' else 'applied to'} {s.project.get('name')} as {me}; log at {home}\n"
          f"next: baton --home {home} swarm --p2p 0.0.0.0:7420 --advertise <your address>:7420 "
          f"--peer {inv['addr']}={inv['cert']}")
    return 0


def cmd_grant(a):
    s = _store(a).state()
    key = a.key
    if not key:
        target = s.actors.get(a.to)
        key = target.key if target else ""
    if not key:
        sys.exit("baton: name the key to bind (`baton peer id` on that machine prints it), or grant a member who joined")
    return _emit(a, "grant", {"to": a.to, "key": key, "right": a.right})


def cmd_revoke(a):
    return _emit(a, "revoke", {"actor": a.target, **({"reason": a.reason} if a.reason else {})})


def cmd_policy_publish(a):
    text = (_store(a).home / "policy.toml").read_text(encoding="utf-8")
    pol.parse(text)
    return _emit(a, "policy", {"toml": text})


def cmd_whoami(a):
    s = _store(a).state()
    me = _actor(a)
    x = s.actors.get(me)
    if x is None:
        print(f"{me}: not a member")
        return 1
    print(json.dumps({"actor": x.name, "kind": x.kind, "present": x.present(time.time()), "offer": x.offer}))
    return 0


# -- talk ------------------------------------------------------------------------------------------------------------
def _show(m, s, now) -> str:
    where = f"[{_task_title(s, m.task)}] " if m.task else ""
    to = f" -> {m.to}" if m.to else ""
    return f"{m.id[:12]} {_ago(now - m.ts):>4} {m.actor}{to}{' asks' if m.ask else ''}: {where}{m.text}"


def _task_title(s, task_id):
    t = s.tasks.get(task_id)
    return (t.title[:40] if t else "?") + f" {task_id[:8]}"


def cmd_message(a):
    """Say something in the log: in a package's thread, to one member, or as a reply. --ask waits for a reply."""
    store = _store(a)
    s = store.state()
    body = {"text": a.text if a.text != "-" else sys.stdin.read()}
    task = a.task or (os.environ.get("BATON_TASK") if not a.reply and not a.to else None)
    if task:
        body["task"] = store.resolve(task, s.tasks)
    if a.to:
        body["to"] = a.to
    if a.reply:
        body["reply_to"] = store.resolve(a.reply, s.messages)
        body.pop("task", None)
    if a.ask or a.wait:
        body["ask"] = True
    me = _actor(a)
    e = store.append("message", me, body)
    code, text = _outcome(store, e)
    print(text)
    if code or not a.wait:
        return code
    deadline = time.time() + pol.duration(a.wait)
    while time.time() < deadline:
        s = store.state()
        answers = [m for m in s.messages.values() if m.reply_to == e.id]
        if answers:
            for m in answers:
                print(_show(m, s, time.time()))
            return 0
        time.sleep(2)
    print(f"no answer within {a.wait}")
    return 3


def cmd_inbox(a):
    s = _store(a).state()
    now = time.time()
    got = s.inbox(_actor(a), unanswered=not a.all)
    if a.json:
        print(json.dumps([{"id": m.id, "actor": m.actor, "text": m.text, "task": m.task, "to": m.to, "ask": m.ask,
                           "reply_to": m.reply_to, "ts": m.ts} for m in got], ensure_ascii=False))
        return 0
    for m in got:
        print(_show(m, s, now))
    if not got:
        print("nothing waiting" if not a.all else "no messages")
    return 0


def cmd_thread(a):
    store = _store(a)
    s = store.state()
    tid = store.resolve(a.task_id, s.tasks)
    t = s.tasks[tid]
    print(f"{tid[:12]} {s.task_status(t, time.time())}: {t.title}")
    now = time.time()
    for m in s.thread(tid):
        print("  " + _show(m, s, now))
    return 0


def cmd_update(a):
    import subprocess
    import tempfile
    import urllib.request
    url = "https://ps1ke.github.io/Baton/install.sh"
    with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as f:
        f.write(urllib.request.urlopen(url, timeout=30).read())
    return subprocess.call(["sh", f.name], env={**os.environ, "BATON_REF": a.ref})


def cmd_ui(a):
    from . import ui
    ui.serve(_ws(), a.actor or os.environ.get("BATON_ACTOR"), port=a.port, open_browser=not a.no_open,
             extra_home=Path(a.home), sync_every=pol.duration(a.sync) if a.sync else None)
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
        for spec in a.need:   # [tier][:skill,skill][@hardware,hardware][*count]
            count = None
            if "*" in spec:
                spec, count = spec.rsplit("*", 1)
            hw = []
            if "@" in spec:
                spec, h = spec.split("@", 1)
                hw = [x for x in h.split(",") if x]
            tier, _, sk = spec.partition(":")
            n = {"skills": [x for x in sk.split(",") if x]}
            if tier:
                n["tier"] = tier
            if hw:
                n["hardware"] = hw
            if count:
                n["count"] = int(count)
            needs.append(n)
        f["needs"] = needs
    return f


def cmd_project(a):
    from .workspace import manifest_of, publish
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
            _ws().add_project(s.project_id, store.home, trusted=True)
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
    ws = _ws()
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
    entries, problems = _ws().discover()
    for e in entries:
        m = e["manifest"]
        if a.tag and not set(a.tag) <= set(m.get("tags", [])):
            continue
        needs = ", ".join((n.get("tier") or "any") + (":" + "/".join(n.get("skills", [])) if n.get("skills") else "")
                          for n in m.get("needs", []))
        act = m.get("activity", {})
        print(f"{m['id'][:12]} {m['name']}  [{'local' if e['local'] else ', '.join(e['sources'])}] join={m.get('join', 'apply')}")
        if m.get("summary"):
            print(f"    {m['summary']}")
        if needs or act:
            print(f"    needs: {needs or '-'}" + (f"   open: {act.get('open_tasks')}, machines present: {act.get('present')}" if act else ""))
    for p in problems:
        print(f"! {p}", file=sys.stderr)
    return 0


def _find_manifest(project: str):
    entries, _ = _ws().discover()
    hits = [e for e in entries if e["manifest"]["id"].startswith(project)]
    if len(hits) != 1:
        raise KeyError(f"{project!r} matches {len(hits)} known projects")
    return hits[0]


def cmd_volunteer(a):
    from .workspace import volunteer
    from . import machine as mc
    entry = _find_manifest(a.project)
    o = mc.offer(_probe(a), _ws().machine(), 180.0)
    if not o["models"]:
        sys.exit("baton: this machine offers no model yet: `baton machine allow <provider:model>`")
    out = volunteer(_ws(), entry, _actor(a), o, a.note or "")
    print(f"applied ({out['event'][:12]}) as {_actor(a)} offering {len(o['models'])} model(s) and {o['slots']} slot(s); "
          f"log at {out['home']}\n{out['deliver']}")
    return 0


def cmd_applications(a):
    s = _store(a).state()
    for x in s.applications.values():
        if a.all or x.status == "pending":
            models = ", ".join(m["id"] for m in x.offer.get("models", []))
            print(f"{x.actor} {x.status}" + (f" offering {models} ({x.offer.get('slots', 0)} slots)" if models else "")
                  + (f" : {x.note}" if x.note else ""))
    return 0


def cmd_admit(a):
    body = {"applicant": a.applicant}
    if a.cmd == "decline" and a.reason:
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
    ws = _ws()
    print(json.dumps({"dir": str(ws.dir), **ws.load()}, indent=1))
    return 0


def cmd_sync(a):
    from . import sync
    store = _store(a)
    if not a.every:
        st = sync.sync_once(store, a.remote, a.branch)
        print(sync.describe(str(store.home.resolve()), st))
        return 0 if st["ok"] else 1
    s = sync.Syncer([store.home], pol.duration(a.every), a.remote, a.branch,
                    log=lambda line: print(time.strftime("%H:%M:%S ") + line, flush=True))
    print(f"syncing {store.home} with {a.remote} every {a.every} (Ctrl-C to stop)", flush=True)
    try:
        s.run()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="baton", description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", default=os.environ.get("BATON_HOME", ".baton"), help="the log folder (BATON_HOME)")
    ap.add_argument("--actor", help="who acts (BATON_ACTOR; default: this machine's member name)")
    from . import __version__
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init").set_defaults(fn=cmd_init)

    p = sub.add_parser("machine", help="this machine: hardware, agents, models; choose what it offers")
    p.add_argument("action", nargs="?", default="show", choices=("show", "allow", "deny", "tier", "slots", "name", "cmd", "hardware"))
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.add_argument("--json", action="store_true"); p.add_argument("--limits", action="store_true", help="also read usage limits")
    p.set_defaults(fn=cmd_machine)
    sub.add_parser("join", help="join an open project with this machine").set_defaults(fn=cmd_join)
    sub.add_parser("offer", help="announce this machine's offer now (a swarm does it by itself)").set_defaults(fn=cmd_offer)

    p = sub.add_parser("task", aliases=["package"], help="post a work package")
    p.add_argument("title", nargs="?"); p.add_argument("--file", help="a work package JSON file (spec/schemas/package.schema.json)")
    p.add_argument("--kind", choices=pol.KINDS); p.add_argument("--goal"); p.add_argument("--context"); p.add_argument("--detail")
    p.add_argument("--path", action="append"); p.add_argument("--topic", action="append")
    p.add_argument("--accept", action="append", help="an acceptance criterion"); p.add_argument("--check", action="append", help="name=command")
    p.add_argument("--deliverable", action="append"); p.add_argument("--constraint", action="append"); p.add_argument("--input", action="append")
    p.add_argument("--size", choices=("S", "M", "L", "XL")); p.add_argument("--tier", choices=pol.TIERS)
    p.add_argument("--hardware", action="append"); p.add_argument("--parent"); p.add_argument("--depends-on", action="append")
    p.add_argument("--base", help="the commit it starts from (default for a root package: HEAD here)")
    p.set_defaults(fn=cmd_task)
    p = sub.add_parser("tasks", help="the package tree"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_tasks)
    p = sub.add_parser("show", help="one package, with its runs and report"); p.add_argument("task_id"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("done", help="report how a package you hold ended"); p.add_argument("task_id")
    p.add_argument("--outcome", choices=("done", "split", "failed", "escalated"), default="done"); p.add_argument("--summary")
    p.add_argument("--change", action="append", help="a path you changed"); p.add_argument("--check", action="append", help="name=passed|failed|not_run[:command]")
    p.add_argument("--verification", choices=("passed", "failed", "not_run")); p.add_argument("--finding", action="append", help="severity:text[@path[:line]]")
    p.add_argument("--verdict", choices=("passed", "changes_requested")); p.add_argument("--notes"); p.add_argument("--report", help="a report JSON file")
    p.set_defaults(fn=cmd_done)
    sub.add_parser("assign", help="what the network gives to whom right now").set_defaults(fn=cmd_assign)
    p = sub.add_parser("run", help="run one package the network gave this machine")
    p.add_argument("task_id", nargs="?"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--timeout")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("swarm", help="take part in the project with this machine until stopped")
    p.add_argument("--slots", type=int, help="packages at once (default: `baton machine slots`)")
    p.add_argument("--max-runs", type=int, default=20, help="stop after this many agent runs (0: no limit)")
    p.add_argument("--timeout", default="30m", help="per agent run"); p.add_argument("--interval", default="3s")
    p.add_argument("--sync", help="sync with the git remote this often, e.g. 15s")
    p.add_argument("--once", action="store_true", help="stop when nothing is left for this machine")
    p.add_argument("--transcripts", help="where agent output is kept (default: <home>/runs)")
    p.add_argument("--p2p", metavar="HOST:PORT", help="run a peer node listening here, e.g. 0.0.0.0:7420")
    p.add_argument("--advertise", metavar="HOST:PORT", help="the address other machines use to reach it")
    p.add_argument("--peer", action="append", metavar="HOST:PORT=CERT", help="a node to reach before offers name any")
    p.add_argument("--p2p-every", default="5s", help="how often to exchange with every peer")
    p.set_defaults(fn=cmd_swarm)

    p = sub.add_parser("claim"); p.add_argument("task", nargs="?"); p.add_argument("--task-id"); p.add_argument("--model")
    p.add_argument("--path", action="append"); p.add_argument("--topic", action="append"); p.add_argument("--ttl")
    p.set_defaults(fn=cmd_claim)
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
    p = sub.add_parser("peer", help="this machine's peer node: serve, sync once, or show its identity")
    p.add_argument("action", choices=("serve", "sync", "id")); p.add_argument("target", nargs="?", help="sync: HOST:PORT=CERT")
    p.add_argument("--listen", default="0.0.0.0:7420"); p.add_argument("--advertise")
    p.add_argument("--peer", action="append", metavar="HOST:PORT=CERT"); p.add_argument("--every", default="5s")
    p.set_defaults(fn=cmd_peer)
    p = sub.add_parser("invite", help="an invite for one machine to apply (author and stewards)")
    p.add_argument("addr", help="the address your node is reachable at, HOST:PORT"); p.add_argument("--ttl", default="1d")
    p.set_defaults(fn=cmd_invite)
    p = sub.add_parser("connect", help="use an invite: fetch the project's card and apply with this machine")
    p.add_argument("invite"); p.add_argument("--note"); p.set_defaults(fn=cmd_connect)
    p = sub.add_parser("grant", help="the author makes a machine or a human a steward (level 0)")
    p.add_argument("to"); p.add_argument("--key", help="its public key (default: the key it joined with)")
    p.add_argument("--right", choices=("steward", "member"), default="steward"); p.set_defaults(fn=cmd_grant)
    p = sub.add_parser("revoke", help="remove an actor (stewards remove members; the author removes anyone)")
    p.add_argument("target"); p.add_argument("--reason"); p.set_defaults(fn=cmd_revoke)
    sub.add_parser("policy", help="publish policy.toml into the log (author only)").set_defaults(fn=cmd_policy_publish)

    p = sub.add_parser("message", help="talk in the log: a package thread, one member, or a reply")
    p.add_argument("text", help='the message, or "-" to read it from stdin')
    p.add_argument("--task", help="the package thread (default inside a run: BATON_TASK)")
    p.add_argument("--to", help="one member"); p.add_argument("--reply", help="the message id you answer")
    p.add_argument("--ask", action="store_true", help="a question: it waits in the inbox until someone replies")
    p.add_argument("--wait", help="ask, then wait this long for the reply, e.g. 10m"); p.set_defaults(fn=cmd_message)
    p = sub.add_parser("inbox", help="messages waiting for an answer"); p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_inbox)
    p = sub.add_parser("thread", help="a package and its messages"); p.add_argument("task_id"); p.set_defaults(fn=cmd_thread)

    p = sub.add_parser("project", help="create, change, link and publish this log's project")
    p.add_argument("action", choices=("create", "set", "show", "link", "publish"))
    p.add_argument("catalog", nargs="?", help="publish: the catalog file to add the manifest to")
    for k in ("name", "summary", "description", "homepage", "repo"):
        p.add_argument("--" + k)
    p.add_argument("--join", choices=mf.JOIN_MODES); p.add_argument("--log-git"); p.add_argument("--log-branch")
    p.add_argument("--inbox", help="where applications are sent: a git URL or an http(s) URL")
    p.add_argument("--tag", action="append")
    p.add_argument("--need", action="append", help="[tier][:skill,skill][@hardware][*count], e.g. strong:python@gpu*3")
    p.add_argument("--rel", choices=mf.RELS, default="related")
    p.add_argument("--link-project", help="link: the other project's id"); p.add_argument("--link-manifest")
    p.add_argument("--link-name")
    p.set_defaults(fn=cmd_project)
    p = sub.add_parser("catalog", help="the catalogs you follow, or make a new catalog file")
    p.add_argument("action", choices=("list", "add", "remove", "new")); p.add_argument("source", nargs="?")
    p.add_argument("--name"); p.add_argument("--description"); p.set_defaults(fn=cmd_catalog)
    p = sub.add_parser("discover", help="every project your workspace and catalogs know")
    p.add_argument("--tag", action="append"); p.set_defaults(fn=cmd_discover)
    p = sub.add_parser("volunteer", help="apply to help a project with this machine")
    p.add_argument("project", help="project id prefix"); p.add_argument("--note"); p.set_defaults(fn=cmd_volunteer)
    p = sub.add_parser("applications"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_applications)
    for name in ("admit", "decline"):
        p = sub.add_parser(name); p.add_argument("applicant")
        if name == "decline":
            p.add_argument("--reason")
        p.set_defaults(fn=cmd_admit)
    p = sub.add_parser("import", help="take in event files sent by other means"); p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("export-event"); p.add_argument("event"); p.set_defaults(fn=cmd_export_event)
    sub.add_parser("workspace", help="show the workspace file").set_defaults(fn=cmd_workspace)
    p = sub.add_parser("ui", help="a local dashboard: projects, this machine, approvals")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--no-open", action="store_true")
    p.add_argument("--sync", help="also sync every local log that has a git remote this often, e.g. 15s")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("sync", help="exchange events with the log's git remote (once, or --every 15s)")
    p.add_argument("--remote", default="origin"); p.add_argument("--branch", help="default: the current branch")
    p.add_argument("--every", help="keep syncing at this interval, e.g. 15s"); p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("update", help="install the latest baton (runs the installer again)")
    p.add_argument("--ref", default="main"); p.set_defaults(fn=cmd_update)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(x in sub.choices for x in argv) and not any(x in ("-h", "--help", "--version") for x in argv):
        argv.append("ui")   # plain `baton`: open the app in the browser
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (KeyError, ValueError, LookupError, RuntimeError, NotImplementedError, OSError) as err:
        print(f"baton: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
