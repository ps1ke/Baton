"""Fold the event log into state. Every decision is made here, from the events alone, so peers that hold the same events
agree on every lease, budget and approval without asking anyone.

Decisions use an event's *effective* time: its own timestamp, but never earlier than its parents' effective times, so an
event cannot be backdated before what its author had already seen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import events as ev
from . import paths
from .policy import Policy


@dataclass
class Lease:
    id: str
    actor: str
    task: str
    paths: tuple[str, ...]
    topics: tuple[str, ...]
    start: float
    ttl: float
    expiry: float
    released: bool = False

    def active(self, t: float) -> bool:
        return not self.released and self.start <= t < self.expiry

    def collides(self, scope_paths, scope_topics) -> bool:
        return paths.any_overlap(self.paths, scope_paths) or bool(set(self.topics) & set(scope_topics))


@dataclass
class Proposal:
    id: str
    actor: str
    action: str
    target: str
    payload: dict
    ts: float
    key: str
    status: str = "pending"      # pending approved rejected executed duplicate over_budget unleased unknown_action
    reason: str = ""
    decided_by: str = ""
    result: dict | None = None

    @property
    def counts(self) -> bool:
        """Counts against a budget: everything that was let through or is waiting."""
        return self.status in ("pending", "approved", "executed")


@dataclass
class Actor:
    name: str
    role: str
    parent: str | None
    model: str = ""
    effort: str = ""
    children: list[str] = field(default_factory=list)


@dataclass
class State:
    actors: dict[str, Actor] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)
    denied: dict[str, str] = field(default_factory=dict)        # claim event id -> why
    proposals: dict[str, Proposal] = field(default_factory=dict)
    ignored: dict[str, str] = field(default_factory=dict)       # event id -> why it had no effect

    def active_leases(self, t: float) -> list[Lease]:
        return [l for l in self.leases.values() if l.active(t)]

    def conflicts(self, scope_paths, scope_topics, t: float, actor: str | None = None) -> list[Lease]:
        return [l for l in self.active_leases(t) if l.actor != actor and l.collides(scope_paths, scope_topics)]

    def covered(self, actor: str, scope_paths, t: float) -> bool:
        """Every path is inside a lease the actor holds at time t."""
        held = [l for l in self.active_leases(t) if l.actor == actor]
        return all(any(paths.inside(p, lp) for l in held for lp in l.paths) for p in scope_paths)

    def ancestors(self, name: str) -> list[str]:
        out, seen = [], set()
        a = self.actors.get(name)
        while a and a.parent and a.parent not in seen:
            seen.add(a.parent)
            out.append(a.parent)
            a = self.actors.get(a.parent)
        return out


def _str_list(value) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of strings")
    return tuple(value)


class _Folder:
    def __init__(self, policy: Policy):
        self.p = policy
        self.s = State()
        self.eff: dict[str, float] = {}
        for h in sorted(policy.humans):
            self.s.actors[h] = Actor(h, "human", None)

    def ignore(self, e: ev.Event, why: str):
        self.s.ignored[e.id] = why

    def allowed(self, e: ev.Event) -> str | None:
        """None when the actor may emit this kind of event, else the reason it may not."""
        a = self.s.actors.get(e.actor)
        if a is None:
            return "actor has not joined"
        if a.role == "human":
            return None
        need = self.p.role_of_kind(e.kind)
        if need == "approve":
            return None if a.role in self.p.approver_roles else f"role {a.role} cannot approve"
        if need == "execute":
            return None if a.role in self.p.approver_roles else f"role {a.role} cannot execute"
        return None if need in self.p.roles[a.role].can else f"role {a.role} cannot {need}"

    def run(self, ordered: list[ev.Event]) -> State:
        for e in ordered:
            t = max([e.ts] + [self.eff.get(pid, 0.0) for pid in e.parents])
            self.eff[e.id] = t
            if e.kind != "join":
                why = self.allowed(e)
                if why:
                    self.ignore(e, why)
                    continue
            try:
                getattr(self, "on_" + e.kind)(e, t)
            except (ValueError, TypeError, KeyError) as err:
                self.ignore(e, f"bad body: {err}")
        return self.s

    # -- tree ---------------------------------------------------------------------------------------------------
    def on_join(self, e, t):
        if e.actor in self.s.actors:
            return self.ignore(e, "already joined")
        role = self.p.roles.get(e.body["role"])
        if role is None:
            return self.ignore(e, f"unknown role {e.body['role']!r}")
        parent = e.body.get("parent")
        if role.under == "human":
            if parent not in self.p.humans:
                return self.ignore(e, f"a {role.name} must join under a human ({sorted(self.p.humans)})")
        else:
            pa = self.s.actors.get(parent)
            if pa is None or pa.role != role.under:
                return self.ignore(e, f"a {role.name} must join under a {role.under}")
            if len(pa.children) >= self.p.roles[pa.role].max_children:
                return self.ignore(e, f"{parent} already has {len(pa.children)} children (max_children)")
        self.s.actors[e.actor] = Actor(e.actor, role.name, parent, role.model, role.effort)
        if parent in self.s.actors:
            self.s.actors[parent].children.append(e.actor)

    # -- leases -------------------------------------------------------------------------------------------------
    def on_claim(self, e, t):
        b = e.body
        scope = tuple(paths.norm(x) for x in _str_list(b.get("paths", [])))
        topics = _str_list(b.get("topics", []))
        if not scope and not topics:
            return self.ignore(e, "a claim needs at least one path or topic")
        ttl = min(float(b.get("ttl", self.p.default_ttl)), self.p.max_ttl)
        if ttl <= 0:
            return self.ignore(e, "ttl must be positive")
        clash = self.s.conflicts(scope, topics, t, actor=e.actor)
        if clash:
            first = min(clash, key=lambda l: l.start)
            self.s.denied[e.id] = f"overlaps lease {first.id[:12]} held by {first.actor} ({first.task})"
            return
        self.s.leases[e.id] = Lease(e.id, e.actor, str(b.get("task", "")), scope, topics, t, ttl, t + ttl)

    def _lease(self, e) -> Lease | None:
        lease = self.s.leases.get(e.body["lease"])
        if lease is None:
            self.ignore(e, "no such lease")
        return lease

    def on_renew(self, e, t):
        lease = self._lease(e)
        if lease is None:
            return
        if lease.actor != e.actor:
            return self.ignore(e, "only the holder renews a lease")
        if not lease.active(t):
            return self.ignore(e, "lease had already ended; claim again")
        lease.expiry = t + lease.ttl

    def on_release(self, e, t):
        lease = self._lease(e)
        if lease is None:
            return
        a = self.s.actors[e.actor]
        if e.actor != lease.actor and a.role != "human" and e.actor not in self.s.ancestors(lease.actor):
            return self.ignore(e, "only the holder, its orchestrators or a human release a lease")
        if not lease.active(t):
            return self.ignore(e, "lease had already ended")
        lease.released = True
        lease.expiry = t

    # -- outward actions ----------------------------------------------------------------------------------------
    def on_propose(self, e, t):
        b = e.body
        action, target, payload = str(b["action"]), str(b.get("target", "")), b.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        key = ev.digest({"action": action, "target": target, "payload": payload})
        pr = Proposal(e.id, e.actor, action, target, payload, t, key)
        self.s.proposals[e.id] = pr
        budget = self.p.budgets.get(action)
        if budget is None:
            pr.status, pr.reason = "unknown_action", f"no [budgets.{action}] in the policy"
            return
        same = [q for q in self.s.proposals.values() if q is not pr and q.key == key and q.status != "rejected"
                and q.counts]
        if same:
            pr.status, pr.reason = "duplicate", f"same as {same[0].id[:12]} by {same[0].actor}"
            return
        for limit, who in ((budget.per_actor, e.actor), (budget.total, None)):
            if limit is None:
                continue
            n, window = limit
            used = sum(1 for q in self.s.proposals.values() if q is not pr and q.action == action and q.counts
                       and t - window < q.ts <= t and (who is None or q.actor == who))
            if used >= n:
                pr.status = "over_budget"
                pr.reason = f"{used} {action} in the last {window:g}s ({'per actor' if who else 'total'} limit {n})"
                return
        scope = tuple(paths.norm(x) for x in _str_list(payload.get("paths", [])))
        if budget.needs_lease and not scope:
            pr.status, pr.reason = "unleased", f"a {action} must name the paths it touches"
            return
        if scope and not self.s.covered(e.actor, scope, t):
            pr.status, pr.reason = "unleased", "the actor holds no active lease covering these paths"
            return
        role = self.s.actors[e.actor].role
        if role in self.p.auto.get(action, ()):
            pr.status, pr.decided_by = "approved", "policy"

    def _pending(self, e) -> Proposal | None:
        pr = self.s.proposals.get(e.body["proposal"])
        if pr is None:
            self.ignore(e, "no such proposal")
        elif pr.actor == e.actor:
            self.ignore(e, "an actor cannot decide its own proposal")
            pr = None
        return pr

    def on_approve(self, e, t):
        pr = self._pending(e)
        if pr is None:
            return
        if pr.status != "pending":
            return self.ignore(e, f"proposal is {pr.status}")
        pr.status, pr.decided_by = "approved", e.actor

    def on_reject(self, e, t):
        pr = self._pending(e)
        if pr is None:
            return
        if pr.status not in ("pending", "approved"):
            return self.ignore(e, f"proposal is {pr.status}")
        pr.status, pr.decided_by, pr.reason = "rejected", e.actor, str(e.body.get("reason", ""))

    def on_executed(self, e, t):
        pr = self.s.proposals.get(e.body["proposal"])
        if pr is None:
            return self.ignore(e, "no such proposal")
        if pr.status != "approved":
            return self.ignore(e, f"proposal is {pr.status}, not approved")
        pr.status, pr.result = "executed", dict(e.body.get("result", {}))


def fold(ordered: list[ev.Event], policy: Policy) -> State:
    return _Folder(policy).run(ordered)


def causal(events_by_id: dict[str, ev.Event]) -> tuple[list[ev.Event], dict[str, str]]:
    """The events whose whole history is present, in fold order, and why the others are held back.

    An event is admitted only after all its parents, and only with a Lamport clock above theirs.
    """
    children: dict[str, list[str]] = {}
    missing: dict[str, int] = {}
    held: dict[str, str] = {}
    for e in events_by_id.values():
        absent = [p for p in e.parents if p not in events_by_id]
        if absent:
            held[e.id] = f"waiting for parent {absent[0][:12]}"
            continue
        missing[e.id] = len(e.parents)
        for p in e.parents:
            children.setdefault(p, []).append(e.id)
    ready = deque(i for i, n in missing.items() if n == 0)
    admitted: dict[str, ev.Event] = {}
    while ready:
        i = ready.popleft()
        e = events_by_id[i]
        if any(events_by_id[p].lamport >= e.lamport for p in e.parents):
            held[i] = "lamport clock not above its parents'"
            continue
        admitted[i] = e
        for c in children.get(i, ()):
            missing[c] -= 1
            if missing[c] == 0:
                ready.append(c)
    for i in missing:
        if i not in admitted and i not in held:
            held[i] = "an ancestor was held back"
    return ev.order(admitted.values()), held
