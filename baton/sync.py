"""Background sync: keep local logs exchanging events with their git remotes every few seconds.

Without it, two machines only see each other's work when someone runs `baton sync`. A Syncer runs in a thread (in
`baton sync --every`, `baton ui --sync` and `baton swarm`), syncs every log it is given that has a remote, backs off
on a log whose remote fails, and records the outcome per log in STATUS so the dashboard can show how fresh it is.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .store import Store

STATUS: dict[str, dict] = {}   # resolved home -> {"at", "ok", "pulled", "new", "sent", "pushed", "error"}
MAX_BACKOFF = 300.0


def status_of(home) -> dict | None:
    return STATUS.get(str(Path(home).resolve()))


def sync_once(store: Store, remote: str = "origin", branch: str | None = None) -> dict:
    """One sync, recorded in STATUS. Never raises: a failure is part of the returned status."""
    key = str(store.home.resolve())
    try:
        r = store.sync(remote, branch)
        st = {"at": time.time(), "ok": r["error"] is None, "pulled": True, **r}
    except (RuntimeError, OSError) as err:
        st = {"at": time.time(), "ok": False, "pulled": False, "new": 0, "sent": 0, "pushed": False, "error": str(err)}
    STATUS[key] = st
    return st


class Syncer:
    """Sync the logs `homes()` returns every `every` seconds until stopped."""

    def __init__(self, homes, every: float = 15.0, remote: str = "origin", branch: str | None = None, log=None):
        self.homes = homes if callable(homes) else (lambda: homes)
        self.every, self.remote, self.branch, self.log = max(1.0, every), remote, branch, log
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._next: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._said: dict[str, str] = {}
        self._thread: threading.Thread | None = None

    def tick(self, now: float | None = None) -> dict[str, dict]:
        """Sync every log that is due; the statuses of those synced."""
        now = time.time() if now is None else now
        done = {}
        for home in self.homes():
            store = Store(home)
            key = str(store.home.resolve())
            if now < self._next.get(key, 0) or not store.has_remote(self.remote):
                continue
            st = sync_once(store, self.remote, self.branch)
            done[key] = st
            if st["pulled"]:   # a push refused (a log you may only read) is not a reason to pull less often
                self._backoff.pop(key, None)
                self._next[key] = now + self.every
            else:
                wait = min(MAX_BACKOFF, self._backoff.get(key, self.every) * 2)
                self._backoff[key] = wait
                self._next[key] = now + wait
            error = st["error"] or ""
            if self.log and (st["new"] or st["sent"] or error != self._said.get(key, "")):
                self.log(describe(key, st))
            self._said[key] = error
        return done

    def poke(self):
        """Sync every log on the next pass (something worth sending just happened)."""
        self._next = {k: v for k, v in self._next.items() if k in self._backoff}
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as err:   # a background thread must not die silently
                if self.log:
                    self.log(f"sync: {err}")
            self._wake.wait(1.0)
            self._wake.clear()

    def start(self) -> "Syncer":
        self._thread = threading.Thread(target=self.run, name="baton-sync", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)


def describe(home: str, st: dict) -> str:
    name = Path(home).name
    parts = []
    if st["new"]:
        parts.append(f"{st['new']} new event(s) in")
    if st["sent"]:
        parts.append(f"{st['sent']} sent" + ("" if st["pushed"] else " (committed, not pushed)"))
    if st["error"]:
        parts.append(("push failed: " if st["sent"] or st["new"] else "") + (st["error"].splitlines() or ["?"])[-1][:200])
    return f"sync {name}: " + (", ".join(parts) or "up to date")
