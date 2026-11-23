"""A machine's workspace: the projects it holds a log for, the catalogs it follows, and what the machine offers.

Default location: $BATON_WORKSPACE, else ~/.local/share/baton. workspace.json:

    {"projects": {"<project id>": {"home": "/path/to/log", "trusted": true}},
     "catalogs": ["https://example.org/catalog.json", "/path/to/catalog.json"],
     "machine": {"name": "m-1a2b3c4d", "slots": 3,
                 "allowed": ["claude:claude-sonnet-5-5", "codex:gpt-6.1-sol", "ollama:qwen3-coder:30b"],
                 "tiers": {"ollama:qwen3-coder:30b": "standard"},
                 "commands": {"claude": ["claude", "-p", "--model", "{model}", ...]}, "share_hardware": true}}

`trusted` is true for projects created on this machine. What runs here (`machine.commands`, or baton's defaults per
provider) is only ever this file's choice: no project's policy can choose a command (spec section 10).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

from . import manifest as mf
from .store import Store

MAX_DOC = 2 * 1024 * 1024
MAX_CATALOGS = 50
MAX_DEPTH = 2


def default_dir() -> Path:
    return Path(os.environ.get("BATON_WORKSPACE") or Path.home() / ".local" / "share" / "baton")


def manifest_of(store: Store, now: float | None = None) -> dict | None:
    """The project's manifest as every peer computes it from the log, or None for a log with no project event."""
    import time
    s = store.state()
    if not s.project_id:
        return None
    now = time.time() if now is None else now
    m = {"baton": mf.PROTOCOL, "id": s.project_id, **s.project, "humans": sorted(store.policy().humans),
         "updated": mf.iso(s.project_updated)}
    m.setdefault("join", "apply")
    m["activity"] = {
        "agents": sum(1 for a in s.actors.values() if a.kind == "member"),
        "present": sum(1 for a in s.actors.values() if a.kind == "member" and a.present(now)),
        "open_tasks": sum(1 for t in s.tasks.values() if s.task_status(t, now) == "open"),
        "running": sum(1 for t in s.tasks.values() if s.task_status(t, now) == "taken"),
        "done": sum(1 for t in s.tasks.values() if t.outcome == "done"),
        "pending_applications": sum(1 for a in s.applications.values() if a.status == "pending"),
        "as_of": mf.iso(now),
    }
    return m


def fetch(source: str, base: str | None = None) -> dict:
    """Read a JSON document from a local path or an http(s) URL, at most MAX_DOC bytes."""
    if base and not source.startswith(("http://", "https://", "/")) and base.startswith(("http://", "https://")):
        source = base.rsplit("/", 1)[0] + "/" + source
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"Accept": "application/json", "User-Agent": "baton"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read(MAX_DOC + 1)
    else:
        path = Path(source.removeprefix("file://")).expanduser()
        if base and not path.is_absolute() and not base.startswith(("http://", "https://")):
            path = Path(base).parent / path
        with open(path, "rb") as f:
            data = f.read(MAX_DOC + 1)
    if len(data) > MAX_DOC:
        raise ValueError(f"{source} is larger than {MAX_DOC} bytes")
    return json.loads(data.decode("utf-8"))


class Workspace:
    def __init__(self, path: Path | None = None):
        self.dir = Path(path) if path else default_dir()
        self.file = self.dir / "workspace.json"

    def load(self) -> dict:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            d = {}
        d.setdefault("projects", {})
        d.setdefault("catalogs", [])
        return d

    def save(self, d: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, self.file)

    # -- projects -----------------------------------------------------------------------------------------------
    def add_project(self, project_id: str, home: Path, trusted: bool):
        d = self.load()
        d["projects"][project_id] = {"home": str(Path(home).resolve()), "trusted": bool(trusted)}
        self.save(d)

    def projects(self) -> dict[str, dict]:
        return self.load()["projects"]

    def entry_for(self, home: Path) -> tuple[str, dict] | None:
        target = str(Path(home).resolve())
        for pid, entry in self.projects().items():
            if entry.get("home") == target:
                return pid, entry
        return None

    def store(self, project_id: str) -> Store:
        entry = self.projects().get(project_id)
        if entry is None:
            raise KeyError(f"no local copy of project {project_id[:12]}")
        return Store(entry["home"])

    # -- this machine -------------------------------------------------------------------------------------------
    def machine(self) -> dict:
        from . import machine as mc
        cfg = {**mc.DEFAULTS, **(self.load().get("machine") or {})}
        cfg["name"] = cfg.get("name") or mc.default_name()
        return cfg

    def set_machine(self, **changes) -> dict:
        from . import machine as mc
        d = self.load()
        cfg = {**mc.DEFAULTS, **(d.get("machine") or {})}
        for k, v in changes.items():
            if k not in mc.DEFAULTS:
                raise KeyError(f"unknown machine setting {k!r}")
            cfg[k] = v
        if cfg.get("slots") is not None and not (isinstance(cfg["slots"], int) and 0 <= cfg["slots"] <= 64):
            raise ValueError("slots must be 0..64")
        for ref, tier in cfg.get("tiers", {}).items():
            if tier not in mc.TIERS:
                raise ValueError(f"{ref}: tier must be one of {', '.join(mc.TIERS)}")
        d["machine"] = cfg
        self.save(d)
        return self.machine()

    def clone(self, m: dict) -> Store:
        """Fetch a project's log from its manifest's log.git, and check it really is that project."""
        log = m.get("log") or {}
        url = log.get("git")
        if not mf.git_url_ok(url):
            raise ValueError("this project publishes no usable log.git URL")
        dest = self.dir / "projects" / m["id"][:16]
        if dest.exists():
            raise FileExistsError(f"{dest} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "-c", "protocol.ext.allow=never", "clone", "-q"]
        if log.get("branch"):
            cmd += ["--branch", log["branch"]]
        r = subprocess.run(cmd + ["--", url, str(dest)], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or "git clone failed")
        store = Store(dest)
        got = store.state().project_id
        if got != m["id"]:
            raise ValueError(f"the log at {url} is project {str(got)[:12]}, not {m['id'][:12]}: not adding it")
        self.add_project(m["id"], dest, trusted=False)
        return store

    # -- catalogs -----------------------------------------------------------------------------------------------
    def add_catalog(self, source: str):
        d = self.load()
        if source not in d["catalogs"]:
            d["catalogs"].append(source)
        self.save(d)

    def remove_catalog(self, source: str):
        d = self.load()
        d["catalogs"] = [c for c in d["catalogs"] if c != source]
        self.save(d)

    def discover(self, now: float | None = None) -> tuple[list[dict], list[str]]:
        """Every project this machine knows: its own logs first, then the catalogs it follows and the catalogs they
        link to (MAX_DEPTH hops, MAX_CATALOGS documents). Returns (entries, problems). An entry is
        {"manifest", "local": bool, "trusted": bool, "sources": [...]}."""
        found: dict[str, dict] = {}
        problems: list[str] = []
        for pid, entry in self.projects().items():
            try:
                m = manifest_of(Store(entry["home"]), now)
            except (OSError, ValueError) as err:
                problems.append(f"local project {pid[:12]}: {err}")
                continue
            if m:
                found[m["id"]] = {"manifest": m, "local": True, "trusted": bool(entry.get("trusted")),
                                  "home": entry["home"], "sources": ["local"]}
        queue = [(c, None, 0) for c in self.load()["catalogs"]]
        seen = set()
        while queue and len(seen) < MAX_CATALOGS:
            src, base, depth = queue.pop(0)
            key = src if not base else f"{base} -> {src}"
            if src in seen:
                continue
            seen.add(src)
            try:
                cat = fetch(src, base)
            except (OSError, ValueError) as err:
                problems.append(f"catalog {key}: {err}")
                continue
            errors = mf.check_catalog(cat)
            if errors:
                problems.append(f"catalog {key}: {'; '.join(errors)}")
                continue
            for m in cat.get("projects", []):
                bad = mf.check_manifest(m)
                if bad:
                    problems.append(f"catalog {cat['name']}: a manifest was skipped ({bad[0]})")
                    continue
                hit = found.get(m["id"])
                if hit is None:
                    found[m["id"]] = {"manifest": m, "local": False, "trusted": False, "sources": [cat["name"]]}
                    continue
                if cat["name"] not in hit["sources"]:
                    hit["sources"].append(cat["name"])
                # Our own log is authoritative; between catalogs the newest manifest wins (spec section 7).
                if not hit["local"] and str(m.get("updated", "")) > str(hit["manifest"].get("updated", "")):
                    hit["manifest"] = m
            if depth < MAX_DEPTH:
                for link in cat.get("catalogs", []):
                    queue.append((link["url"], src, depth + 1))
        return sorted(found.values(), key=lambda e: (not e["local"], e["manifest"]["name"].lower())), problems


def publish(catalog_path: Path, m: dict) -> dict:
    """Add or replace a project's manifest in a catalog file (creating it if needed)."""
    path = Path(catalog_path)
    cat = json.loads(path.read_text(encoding="utf-8")) if path.exists() else mf.new_catalog(path.stem)
    errors = mf.check_catalog(cat) or mf.check_manifest(m)
    if errors:
        raise ValueError("; ".join(errors))
    cat["projects"] = [x for x in cat.get("projects", []) if x.get("id") != m["id"]] + [m]
    cat["updated"] = m.get("updated", "")
    path.write_text(json.dumps(cat, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return cat


def volunteer(ws: "Workspace", entry: dict, actor: str, offer: dict, note: str) -> dict:
    """Apply to help a project with this machine: fetch its log if needed, append an `apply` with the machine's offer
    (no role: the network decides what the machine does), and say how to deliver it."""
    m = entry["manifest"]
    store = Store(entry["home"]) if entry.get("home") else ws.clone(m)
    body = {"offer": offer, "note": note}
    e = store.append("apply", actor, body)
    s = store.state()
    if e.id in s.ignored:
        raise ValueError(s.ignored[e.id])
    file = store.dir / f"{e.id}.json"
    inbox = m.get("inbox")
    if inbox:
        how = f"send {file} to the project's inbox: {inbox['url']} (they import it with `baton import`)"
    elif m.get("log"):
        how = "run `baton sync` in the project's log if you can push to it; otherwise send the event file to a maintainer"
    else:
        how = "send the event file to a maintainer (they import it with `baton import`)"
    return {"event": e.id, "file": str(file), "home": str(store.home), "deliver": how}
