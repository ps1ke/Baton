"""Events: the only thing peers exchange.

An event is content-addressed: its id is the SHA-256 of its canonical JSON, so the same event has the same id and the
same file name on every peer, and two peers merge by taking the union of their files. An event names the events its
author had seen (`parents`) and carries a Lamport clock larger than any of theirs, so sorting by (lamport, id) gives
every peer the same total order, consistent with causality.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass

KINDS = frozenset({"join", "task", "claim", "renew", "release", "done", "propose", "approve", "reject", "executed"})


class BadEvent(ValueError):
    pass


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Event:
    kind: str
    actor: str
    lamport: int
    ts: float
    parents: tuple[str, ...]
    body: dict
    id: str = ""

    def content(self) -> dict:
        return {"kind": self.kind, "actor": self.actor, "lamport": self.lamport, "ts": self.ts,
                "parents": list(self.parents), "body": self.body}

    def to_dict(self) -> dict:
        return {**self.content(), "id": self.id}


def digest(content: dict) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


def make(kind: str, actor: str, body: dict, parents, lamport: int, ts: float) -> Event:
    if kind not in KINDS:
        raise BadEvent(f"unknown event kind {kind!r}")
    if not actor:
        raise BadEvent("an event needs an actor")
    e = Event(kind, actor, int(lamport), float(ts), tuple(sorted(set(parents))), dict(body))
    return dataclasses.replace(e, id=digest(e.content()))


def from_dict(d: dict) -> Event:
    """Rebuild an event read from a peer; refuse one whose id does not match its content (edited or corrupt)."""
    try:
        e = Event(d["kind"], d["actor"], d["lamport"], d["ts"], tuple(d["parents"]), d["body"], d["id"])
    except (KeyError, TypeError) as err:
        raise BadEvent(f"malformed event: {err}") from None
    if e.kind not in KINDS:
        raise BadEvent(f"unknown event kind {e.kind!r}")
    if not isinstance(e.lamport, int) or not isinstance(e.ts, (int, float)) or not isinstance(e.body, dict) \
            or not isinstance(e.actor, str) or not all(isinstance(p, str) for p in e.parents):
        raise BadEvent("event fields have the wrong types")
    if digest(e.content()) != e.id:
        raise BadEvent(f"event {str(e.id)[:12]} does not match its content")
    return e


def order(events) -> list[Event]:
    return sorted(events, key=lambda e: (e.lamport, e.id))
