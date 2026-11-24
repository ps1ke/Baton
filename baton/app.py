"""What the web app does for a person, so nobody needs the command line: who you are, start a project from a
repository, join one from an invite, post work, invite machines, make stewards, and run this machine.

Every function acts through the same signed events as the CLI; the app only chooses sensible defaults:
- a person is `human:<name>`, with a key this machine keeps;
- whoever starts a project is its author, and this machine becomes a steward (level 0) of it;
- every project gets its own peer port on this machine, and running a project means a `baton swarm --p2p` in the
  background, whose output is kept in the workspace.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import codeflow
from . import machine as mc
from . import p2p
from . import policy as pol
from .store import Store
from .workspace import Workspace

BASE_PORT = 7420
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


# -- who you are -----------------------------------------------------------------------------------------------------
def me(ws: Workspace) -> str | None:
    return ws.load().get("me")


def set_me(ws: Workspace, name: str) -> str:
    name = name.strip().removeprefix("human:")
    if not NAME.fullmatch(name):
        raise ValueError("a name is 1 to 40 letters, digits, dots, dashes or underscores (it is shown to the projects you join)")
    d = ws.load()
    d["me"] = "human:" + name
    ws.save(d)
    return d["me"]


def set_address(ws: Workspace, address: str):
    """The host name or IP other machines use to reach this one (a port per project is added)."""
    address = address.strip()
    if address and not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", address):
        raise ValueError("an address is a host name or an IP address, without a port")
    d = ws.load()
    d["address"] = address
    ws.save(d)


def port_of(ws: Workspace, project_id: str) -> int:
    d = ws.load()
    entry = d["projects"].get(project_id, {})
    if not entry.get("port"):
        used = {e.get("port") for e in d["projects"].values()}
        entry["port"] = next(p for p in range(BASE_PORT, BASE_PORT + 1000) if p not in used and _free(p))
        d["projects"][project_id] = entry
        ws.save(d)
    return entry["port"]


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


# -- starting and joining --------------------------------------------------------------------------------------------
def create_project(ws: Workspace, author: str, repo: str, name: str = "", summary: str = "", join: str = "apply",
                   steward_here: bool = True) -> str:
    """A new project from a repository: the genesis (signed by you), this machine as a steward, the policy."""
    from . import manifest as mf
    if not author:
        raise ValueError("say who you are first")
    repo, name = codeflow.as_repo_url(repo), name.strip()
    if not (mf.git_url_ok(repo) or re.fullmatch(r"https://\S+", repo)):
        raise ValueError("give the repository's URL (https://github.com/you/project.git, git@github.com:you/project.git) "
                         "or a folder on this machine that is a git repository")
    if not name:
        name = re.sub(r"\.git$", "", repo.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "project"
    home = ws.dir / "projects" / f"new-{secrets.token_hex(4)}"
    store = Store(home)
    store.init()
    fields = {"name": name[:120], "repo": repo, "join": join}
    if summary.strip():
        fields["summary"] = summary.strip()[:280]
    e = store.append("project", author, fields)
    s = store.state()
    if s.project_id != e.id:
        raise ValueError(s.ignored.get(e.id, "the project could not be created"))
    final = ws.dir / "projects" / e.id[:16]
    home.rename(final)
    store = Store(final)
    ws.add_project(e.id, final, trusted=True)
    store.append("policy", author, {"toml": pol.DEFAULT.lstrip()})
    if steward_here:
        m = ws.machine()["name"]
        store.append("grant", author, {"to": m, "key": store.keyring.public(m), "right": "steward"})
    port_of(ws, e.id)
    try:   # clone now, so the first package has a base commit to start from
        codeflow.checkout(ws, store)
    except RuntimeError:
        pass
    return e.id


def find_by_id(project_id: str) -> list[str]:
    """Addresses announced on the DHT for a discoverable project."""
    from . import dht
    return [f"{ip}:{port}" for ip, port in dht.find(project_id)]


def connect(ws: Workspace, invite: str, note: str = "") -> dict:
    """Use an invite link, or the id of a discoverable open project: fetch its public card and apply (or join)."""
    invite = invite.strip()
    if re.fullmatch(r"[0-9a-f]{64}", invite):   # no invite: look on the local networks, then on the DHT
        from . import lan
        addrs = [a for a, _ in lan.find(invite)]
        if not addrs:
            addrs = find_by_id(invite)
        if not addrs:
            raise ValueError("no machine of that project answers on your networks or on the DHT right now. Its "
                             "stewards must be running baton, and reachable: on your network or VPN, or with a "
                             "forwarded port. An invite link from them works too.")
        return _connect_unpinned(ws, invite, addrs, note)
    inv = p2p.parse_invite(invite)
    home = ws.dir / "projects" / inv["project"][:16]
    store = Store(home)
    store.init()
    member = ws.machine()["name"]
    node = p2p.Node(store, member, ws.dir / "tls")
    r = node.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
    s = store.state()
    if s.project_id != inv["project"]:
        raise ValueError("the inviter sent another project's card; not applying")
    d = ws.load()
    d["projects"][s.project_id] = {**d["projects"].get(s.project_id, {}), "home": str(home.resolve()),
                                   "trusted": False, "peers": {inv["addr"]: inv["cert"]}}
    ws.save(d)
    port_of(ws, s.project_id)
    if r["access"] == "full":
        return {"project": s.project_id, "status": "member", "name": s.project.get("name")}
    offer = mc.offer(mc.probe([home]), ws.machine(), s.policy.presence_ttl)
    if not offer["models"]:
        raise ValueError("this machine offers no model yet: choose some under This machine first")
    kind = "join" if s.join_mode == "open" else "apply"
    store.append(kind, member, {"offer": offer, **({"note": note[:2000]} if note and kind == "apply" else {})})
    node.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
    return {"project": s.project_id, "status": "joined" if kind == "join" else "applied", "name": s.project.get("name")}


def _connect_unpinned(ws: Workspace, project_id: str, addrs: list[str], note: str) -> dict:
    home = ws.dir / "projects" / project_id[:16]
    store = Store(home)
    store.init()
    member = ws.machine()["name"]
    node = p2p.Node(store, member, ws.dir / "tls")
    last = None
    for addr in addrs[:8]:
        try:
            first = node.sync_with(addr, None, project=project_id)   # it must show it is the author or a steward
            s = store.state()
            if s.join_mode == "closed":
                raise ValueError("that project takes no new members")
            offer = mc.offer(mc.probe([home]), ws.machine(), s.policy.presence_ttl)
            if not offer["models"]:
                raise ValueError("this machine offers no model yet: choose some under This machine first")
            kind = "join" if s.join_mode == "open" else "apply"
            store.append(kind, member, {"offer": offer, **({"note": note[:2000]} if note and kind == "apply" else {})})
            node.sync_with(addr, first["cert"], project=project_id)   # now pinned: the same node as before
            d = ws.load()
            d["projects"][project_id] = {**d["projects"].get(project_id, {}), "home": str(home.resolve()),
                                         "trusted": False, "peers": {addr: first["cert"]}}
            ws.save(d)
            port_of(ws, project_id)
            return {"project": project_id, "status": "joined" if kind == "join" else "applied", "name": s.project.get("name")}
        except (OSError, ConnectionError, PermissionError) as err:
            last = err
    raise ValueError(f"could not reach that project's machines: {last}")


def set_discoverable(store: Store, actor: str, on: bool):
    e = store.append("project", actor, {"discoverable": bool(on)})
    if e.id in store.state().ignored:
        raise PermissionError(store.state().ignored[e.id])


# -- acting in a project -----------------------------------------------------------------------------------------
def post_package(ws: Workspace, store: Store, actor: str, req: dict) -> str:
    s = store.state()
    body = {"title": str(req.get("title", "")).strip()}
    for k in ("goal", "context"):
        if str(req.get(k, "")).strip():
            body[k] = str(req[k]).strip()
    if req.get("size") in ("S", "M", "L", "XL"):
        body["size"] = req["size"]
    if req.get("kind") in pol.KINDS:
        body["kind"] = req["kind"]
    paths = [p.strip().strip("/") for p in re.split(r"[,\s]+", str(req.get("paths", ""))) if p.strip()]
    if paths:
        body["paths"] = paths
    checks = [c.strip() for c in str(req.get("checks", "")).splitlines() if c.strip()]
    if checks:
        body["acceptance"] = [{"check": f"check{i + 1}", "run": c} for i, c in enumerate(checks)]
    if s.project_id and codeflow.local_path(s.project.get("repo")):
        codeflow.snapshot(ws, store, body)   # a folder on this machine: its history travels as a bundle
    elif s.project_id:
        try:
            repo = codeflow.checkout(ws, store)
            if repo is not None:
                head = codeflow.git(repo, "rev-parse", "origin/HEAD", check=False).stdout.decode().strip() or \
                    codeflow.git(repo, "rev-parse", "HEAD").stdout.decode().strip()
                body["base"] = head   # the work starts from the repository as it is now
        except (RuntimeError, OSError):
            pass
    e = store.append("task", actor, body)
    s = store.state()
    if e.id in s.ignored:
        raise ValueError(s.ignored[e.id])
    return e.id


def make_invite(ws: Workspace, store: Store, actor: str, ttl: float = 86400) -> str:
    s = store.state()
    who = s.actors.get(actor)
    if not s.project_id or who is None or not who.level0:
        raise PermissionError("invites come from the author or a steward")
    address = ws.load().get("address")
    if not address:
        raise ValueError("set this machine's address first (the host name or IP other machines can reach)")
    node = p2p.Node(store, ws.machine()["name"], ws.dir / "tls")
    token = node.invites.new(s.project_id, ttl)
    return p2p.invite_url(f"{address}:{port_of(ws, s.project_id)}", s.project_id, node.fp, token)


def grant(store: Store, actor: str, to: str, right: str = "steward"):
    s = store.state()
    target = s.actors.get(to)
    if target is None or not target.key:
        raise ValueError(f"{to} has not joined, so there is no key to grant")
    e = store.append("grant", actor, {"to": to, "key": target.key, "right": right})
    if e.id in store.state().ignored:
        raise PermissionError(store.state().ignored[e.id])


def revoke(store: Store, actor: str, target: str):
    e = store.append("revoke", actor, {"actor": target})
    if e.id in store.state().ignored:
        raise PermissionError(store.state().ignored[e.id])


# -- running this machine -------------------------------------------------------------------------------------------
class Runs:
    """This machine's swarms, one per project, as background processes started from the app."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.procs: dict[str, subprocess.Popen] = {}

    def log_file(self, project_id: str) -> Path:
        return self.ws.dir / "logs" / f"{project_id[:16]}.log"

    def start(self, project_id: str, home: Path, max_runs: int = 20) -> dict:
        if self.running(project_id):
            return self.status(project_id)
        d = self.ws.load()
        port = port_of(self.ws, project_id)
        address = d.get("address")
        entry = d["projects"].get(project_id, {})
        cmd = [sys.executable, "-m", "baton", "--home", str(home), "swarm", "--p2p", f"0.0.0.0:{port}",
               "--max-runs", str(int(max_runs)), "--interval", "3s"]
        if address:
            cmd += ["--advertise", f"{address}:{port}"]
        for addr, fp in (entry.get("peers") or {}).items():
            cmd += ["--peer", f"{addr}={fp}"]
        self.log_file(project_id).parent.mkdir(parents=True, exist_ok=True)
        out = open(self.log_file(project_id), "a", encoding="utf-8")
        out.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} started from the app\n")
        out.flush()
        env = {**os.environ, "BATON_WORKSPACE": str(self.ws.dir), "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent),
                                                           os.environ.get("PYTHONPATH")]))}
        self.procs[project_id] = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env,
                                                  cwd=str(home), start_new_session=True)
        return self.status(project_id)

    def stop(self, project_id: str) -> dict:
        p = self.procs.get(project_id)
        if p and p.poll() is None:
            import signal
            p.send_signal(signal.SIGINT)   # running agents finish their package; nothing new starts
        return self.status(project_id)

    def running(self, project_id: str) -> bool:
        p = self.procs.get(project_id)
        return bool(p and p.poll() is None)

    def status(self, project_id: str) -> dict:
        lines = []
        f = self.log_file(project_id)
        if f.exists():
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 8000))
                lines = fh.read().decode("utf-8", "replace").splitlines()[-25:]
        p = self.procs.get(project_id)
        return {"running": self.running(project_id), "exit": p.poll() if p else None, "log": lines,
                "port": self.ws.load()["projects"].get(project_id, {}).get("port")}

    def stop_all(self):
        for pid in list(self.procs):
            self.stop(pid)
