"""A peer's copy of the log: one JSON file per event under <home>/events, named by the event id.

Files are never edited or deleted, so two copies merge by union, and git can carry them between peers without ever
producing a conflict (`sync`).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import events as ev
from . import policy as pol
from . import state as st


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.dir = self.home / "events"

    def init(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        pol_path = self.home / "policy.toml"
        if not pol_path.exists():
            pol_path.write_text(pol.DEFAULT.lstrip(), encoding="utf-8", newline="\n")

    def policy(self) -> pol.Policy:
        return pol.load(self.home)

    def read(self) -> tuple[dict[str, ev.Event], dict[str, str]]:
        """Every event on disk, and the files that are not valid events with why."""
        found, bad = {}, {}
        if not self.dir.is_dir():
            return found, bad
        for path in sorted(self.dir.glob("*.json")):
            try:
                e = ev.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if path.stem != e.id:
                    raise ev.BadEvent("file name is not the event id")
                found[e.id] = e
            except (OSError, ValueError) as err:
                bad[path.name] = str(err)
        return found, bad

    def load(self):
        """(ordered admitted events, held-back event ids with why, bad files with why)."""
        found, bad = self.read()
        ordered, held = st.causal(found)
        return ordered, held, bad

    def state(self) -> st.State:
        ordered, _, _ = self.load()
        return st.fold(ordered, self.policy())

    def append(self, kind: str, actor: str, body: dict, now: float | None = None) -> ev.Event:
        found, _ = self.read()
        ordered, _ = st.causal(found)
        referenced = {p for e in ordered for p in e.parents}
        heads = [e.id for e in ordered if e.id not in referenced]
        lamport = max((e.lamport for e in found.values()), default=0) + 1
        e = ev.make(kind, actor, body, heads, lamport, time.time() if now is None else now)
        self.write(e)
        return e

    def write(self, e: ev.Event):
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{e.id}.json"
        if target.exists():
            return
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(e.to_dict(), f, sort_keys=True, indent=1, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, target)

    def merge_from(self, other: "Store") -> int:
        """Copy in the other peer's valid events (a local stand-in for sync). Returns how many were new."""
        theirs, _ = other.read()
        mine, _ = self.read()
        new = [e for i, e in theirs.items() if i not in mine]
        for e in new:
            self.write(e)
        return len(new)

    def resolve(self, prefix: str, table) -> str:
        hits = [k for k in table if k.startswith(prefix)]
        if len(hits) != 1:
            raise KeyError(f"{prefix!r} matches {len(hits)} ids")
        return hits[0]

    # -- git transport ------------------------------------------------------------------------------------------
    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.home), *args], capture_output=True, text=True, check=check)

    def sync(self, remote: str = "origin", branch: str = "main") -> str:
        """Commit new events, pull the remote's (a union: event files never conflict) and push the result."""
        if not (self.home / ".git").exists():
            raise RuntimeError(f"{self.home} is not a git repository: run `git init` there and add a remote")
        self._git("add", "events", "policy.toml")
        if self._git("diff", "--cached", "--quiet", check=False).returncode:
            self._git("commit", "-q", "-m", "baton: new events")
        pull = self._git("pull", "-q", "--no-rebase", "--no-edit", remote, branch, check=False)
        if pull.returncode and "couldn't find remote ref" not in pull.stderr:
            raise RuntimeError(pull.stderr.strip() or "git pull failed")
        push = self._git("push", "-q", remote, f"HEAD:{branch}", check=False)
        if push.returncode:
            raise RuntimeError(push.stderr.strip() or "git push failed")
        return "synced"
