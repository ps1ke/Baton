"""Events: the only thing peers exchange.

An event is content-addressed: its id is the SHA-256 of its canonical JSON, so the same event has the same id and the
same file name on every peer, and two peers merge by taking the union of their files. It names its author's public
key and carries the author's signature of its id, so no peer can forge or alter another's events in transit. An event names the events its
author had seen (`parents`) and carries a Lamport clock larger than any of theirs, so sorting by (lamport, id) gives
every peer the same total order, consistent with causality.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass

KINDS = frozenset({"project", "join", "apply", "admit", "decline", "task", "claim", "renew", "release", "done",
                   "propose", "approve", "reject", "executed", "message", "offer", "grant", "revoke", "policy"})
SIG_CONTEXT = b"baton-event-v3:"   # what a signature covers: this prefix and the event id


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
    key: str = ""        # the author's Ed25519 public key (hex); part of the content, so of the id
    sig: str = ""        # Ed25519 signature of SIG_CONTEXT + id, by `key`

    def content(self) -> dict:
        return {"kind": self.kind, "actor": self.actor, "lamport": self.lamport, "ts": self.ts,
                "parents": list(self.parents), "body": self.body, "key": self.key}

    def to_dict(self) -> dict:
        return {**self.content(), "id": self.id, "sig": self.sig}


def digest(content: dict) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


def make(kind: str, actor: str, body: dict, parents, lamport: int, ts: float, keyring=None) -> Event:
    """A new event; signed with the actor's key from `keyring` when one is given."""
    if kind not in KINDS:
        raise BadEvent(f"unknown event kind {kind!r}")
    if not actor:
        raise BadEvent("an event needs an actor")
    key = keyring.public(actor) if keyring is not None else ""
    e = Event(kind, actor, int(lamport), float(ts), tuple(sorted(set(parents))), dict(body), key=key)
    e = dataclasses.replace(e, id=digest(e.content()))
    if keyring is not None:
        _, sig = keyring.sign(actor, SIG_CONTEXT + e.id.encode())
        e = dataclasses.replace(e, sig=sig)
    return e


def signed(e: Event) -> bool:
    from . import crypto
    return bool(e.key and e.sig) and crypto.verify(e.key, SIG_CONTEXT + e.id.encode(), e.sig)


def from_dict(d: dict) -> Event:
    """Rebuild an event read from a peer; refuse one whose id does not match its content (edited or corrupt)."""
    try:
        e = Event(d["kind"], d["actor"], d["lamport"], d["ts"], tuple(d["parents"]), d["body"], d["id"],
                  d.get("key", ""), d.get("sig", ""))
    except (KeyError, TypeError) as err:
        raise BadEvent(f"malformed event: {err}") from None
    if e.kind not in KINDS:
        raise BadEvent(f"unknown event kind {e.kind!r}")
    if not isinstance(e.lamport, int) or not isinstance(e.ts, (int, float)) or not isinstance(e.body, dict) \
            or not isinstance(e.actor, str) or not all(isinstance(p, str) for p in e.parents):
        raise BadEvent("event fields have the wrong types")
    if digest(e.content()) != e.id:
        raise BadEvent(f"event {str(e.id)[:12]} does not match its content")
    if not signed(e):
        raise BadEvent(f"event {str(e.id)[:12]} is not signed by its key")
    return e


def order(events) -> list[Event]:
    return sorted(events, key=lambda e: (e.lamport, e.id))
