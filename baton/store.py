"""A peer's copy of the log: one JSON file per event under <home>/events, named by the event id.

Files are never edited or deleted, so two copies merge by union, and git can carry them between peers without ever
producing a conflict (`sync`).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: no cross-process lock
    fcntl = None

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

    def import_file(self, path) -> ev.Event:
        """Take in one event file sent by other means (an inbox, an email, an issue): it is checked like any other."""
        e = ev.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        self.write(e)
        return e

    def resolve(self, prefix: str, table) -> str:
        hits = [k for k in table if k.startswith(prefix)]
        if len(hits) != 1:
            raise KeyError(f"{prefix!r} matches {len(hits)} ids")
        return hits[0]

    # -- git transport ------------------------------------------------------------------------------------------
    def _git(self, *args, check=True):
        env = os.environ.copy()
        if not self._git_identity():
            # merge commits need an author; event files carry their own actor, so a neutral one is fine
            env.update(GIT_AUTHOR_NAME="baton", GIT_AUTHOR_EMAIL="baton@localhost",
                       GIT_COMMITTER_NAME="baton", GIT_COMMITTER_EMAIL="baton@localhost")
        return subprocess.run(["git", "-C", str(self.home), *args], capture_output=True, text=True, check=check,
                              env=env, stdin=subprocess.DEVNULL)

    def _git_identity(self) -> bool:
        if not hasattr(self, "_has_identity"):
            r = subprocess.run(["git", "-C", str(self.home), "config", "user.email"], capture_output=True, text=True)
            self._has_identity = bool(r.stdout.strip())
        return self._has_identity

    def is_repo(self) -> bool:
        return (self.home / ".git").exists()

    def has_remote(self, remote: str = "origin") -> bool:
        return self.is_repo() and self._git("remote", "get-url", remote, check=False).returncode == 0

    def branch(self) -> str:
        r = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return r.stdout.strip() or "main"

    @contextlib.contextmanager
    def _lock(self):
        """One sync at a time per log, across processes (the dashboard, a swarm and a manual `baton sync`)."""
        path = self.home / ".git" / "baton-sync.lock"
        with open(path, "a+") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def sync(self, remote: str = "origin", branch: str | None = None, push: bool = True) -> dict:
        """Commit new events, pull the remote's (a union: event files never conflict, so even two peers that started
        separate histories merge cleanly) and push the result.

        Returns {"new": events that arrived, "sent": events committed here, "pushed": bool, "error": why a push
        failed}. A pull failure raises; a push failure does not (a log you may only read still syncs one way)."""
        if not self.is_repo():
            raise RuntimeError(f"{self.home} is not a git repository: run `git init` there and add a remote")
        branch = branch or self.branch()
        with self._lock():
            before = set(self.read()[0])
            ignore = self.dir / ".gitignore"
            if not ignore.exists():
                self.dir.mkdir(parents=True, exist_ok=True)
                ignore.write_text(".tmp-*\n", encoding="utf-8")
            self._git("add", "--", "events", *(["policy.toml"] if (self.home / "policy.toml").exists() else []))
            staged = self._git("diff", "--cached", "--name-only", check=False).stdout.split()
            sent = sum(1 for f in staged if f.endswith(".json"))
            if staged:
                self._git("commit", "-q", "-m", f"baton: {sent} new event(s)")
            self._pull(remote, branch)
            pushed, error = False, None
            if push:
                for _ in range(3):
                    r = self._git("push", "-q", remote, f"HEAD:{branch}", check=False)
                    if r.returncode == 0:
                        pushed = True
                        break
                    error = r.stderr.strip() or "git push failed"
                    if "fetch first" not in error and "non-fast-forward" not in error and "rejected" not in error:
                        break
                    self._pull(remote, branch)   # someone pushed in between: take theirs and try again
                if pushed:
                    error = None
            new = len(set(self.read()[0]) - before)
            return {"new": new, "sent": sent, "pushed": pushed, "error": error}

    def _pull(self, remote, branch):
        r = self._git("pull", "-q", "--no-rebase", "--no-edit", "--allow-unrelated-histories", remote, branch, check=False)
        if r.returncode and "couldn't find remote ref" not in r.stderr:
            raise RuntimeError(r.stderr.strip() or "git pull failed")
