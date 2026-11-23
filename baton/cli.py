"""baton: a coordination layer between many agents and one shared repository.

Agents never talk to the forge directly: they join a tree of roles, lease the paths they work on, and propose outward
actions, which pass rate budgets, duplicate checks and an approval gate before `execute` performs them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import forge
from . import policy as pol
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


def cmd_claim(a):
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
    p = sub.add_parser("claim"); p.add_argument("task"); p.add_argument("--path", action="append")
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
    p = sub.add_parser("sync"); p.add_argument("--remote", default="origin"); p.add_argument("--branch", default="main")
    p.set_defaults(fn=cmd_sync)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (KeyError, ValueError, RuntimeError, NotImplementedError) as err:
        print(f"baton: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
