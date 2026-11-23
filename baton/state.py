"""Fold the event log into state. Every decision is made here, from the events alone, so peers that hold the same events
agree on every lease, budget and approval without asking anyone.

Decisions use an event's *effective* time: its own timestamp, but never earlier than its parents' effective times, so an
event cannot be backdated before what its author had already seen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import events as ev
from . import manifest as mf
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
class Task:
    id: str
    by: str
    title: str
    detail: str
    paths: tuple[str, ...]
    topics: tuple[str, ...]
    role: str | None             # only this role may take it
    parent: str | None           # the task this one was split from
    created: float
    lease: str | None = None     # the lease that holds it now (or last held it)
    outcome: str | None = None   # done failed escalated
    summary: str = ""
    evidence: dict = field(default_factory=dict)
    subtasks: list[str] = field(default_factory=list)


OUTCOMES = ("done", "failed", "escalated")


@dataclass
class Application:
    id: str
    actor: str
    role: str
    parent: str | None
    offer: dict
    note: str
    ts: float
    status: str = "pending"     # pending admitted declined
    decided_by: str = ""
    reason: str = ""


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
class Message:
    id: str
    seq: int                     # position in the fold: later messages have larger seq
    actor: str
    text: str
    task: str | None             # the task thread it belongs to
    to: str | None               # addressed to one actor (else everyone following the thread)
    reply_to: str | None
    root: str                    # the first message of its reply chain
    ts: float


MESSAGE_MAX = 4000


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
    tasks: dict[str, Task] = field(default_factory=dict)
    applications: dict[str, Application] = field(default_factory=dict)
    messages: dict[str, Message] = field(default_factory=dict)  # in fold order
    project_id: str | None = None                               # the genesis `project` event
    project: dict = field(default_factory=dict)                 # the project's current public fields
    project_updated: float = 0.0
    ignored: dict[str, str] = field(default_factory=dict)       # event id -> why it had no effect

    @property
    def join_mode(self) -> str:
        """A log with no `project` event is a private workspace: anyone may join, as before the protocol had projects."""
        return self.project.get("join", "apply") if self.project_id else "open"

    def task_status(self, task: Task, t: float) -> str:
        """open, taken, or the outcome. A task whose lease lapsed without a `done` is open again."""
        if task.outcome:
            return task.outcome
        lease = self.leases.get(task.lease) if task.lease else None
        return "taken" if lease and lease.active(t) else "open"

    def open_tasks(self, t: float, role: str | None = None) -> list[Task]:
        """Tasks nobody holds, oldest first, that a `role` may take and whose scope is free right now."""
        out = []
        for task in sorted(self.tasks.values(), key=lambda x: x.created):
            if self.task_status(task, t) != "open" or (role and task.role and task.role != role):
                continue
            if task.subtasks and any(not self.tasks[c].outcome for c in task.subtasks):
                continue   # split into subtasks that are still running
            if self.conflicts(task.paths, task.topics, t):
                continue
            out.append(task)
        return out

    def active_leases(self, t: float) -> list[Lease]:
        return [l for l in self.leases.values() if l.active(t)]

    def conflicts(self, scope_paths, scope_topics, t: float, actor: str | None = None) -> list[Lease]:
        return [l for l in self.active_leases(t) if l.actor != actor and l.collides(scope_paths, scope_topics)]

    def covered(self, actor: str, scope_paths, t: float) -> bool:
        """Every path is inside a lease the actor holds at time t."""
        held = [l for l in self.active_leases(t) if l.actor == actor]
        return all(any(paths.inside(p, lp) for l in held for lp in l.paths) for p in scope_paths)

    def thread(self, task_id: str) -> list[Message]:
        return [m for m in self.messages.values() if m.task == task_id]

    def _answered(self, m: Message, actor: str) -> bool:
        """`actor` wrote after `m` in reply to it, or later in the same task thread."""
        return any(x.actor == actor and x.seq > m.seq and (x.reply_to == m.id or (m.task and x.task == m.task))
                   for x in self.messages.values())

    def inbox(self, actor: str, unanswered: bool = True) -> list[Message]:
        """Messages for `actor`: addressed to it, replies to what it wrote, or posted to the thread of a task it
        created or has held, and (by default) only those it has not answered yet."""
        mine = {t.id for t in self.tasks.values()
                if t.by == actor or (t.lease and t.lease in self.leases and self.leases[t.lease].actor == actor)}
        wrote = {m.id for m in self.messages.values() if m.actor == actor}
        out = []
        for m in self.messages.values():
            if m.actor == actor:
                continue
            if m.to == actor or m.reply_to in wrote or (m.to is None and m.task in mine):
                if not unanswered or not self._answered(m, actor):
                    out.append(m)
        return out

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
        if need == "govern":
            return None if a.role in self.p.approver_roles else f"role {a.role} cannot change the project"
        if need == "talk":    # every member may talk; messages never act on anything
            return None
        if need == "admit":   # approvers, or a role that may admit (then only under itself: on_admit checks)
            ok = a.role in self.p.approver_roles or "admit" in self.p.roles[a.role].can
            return None if ok else f"role {a.role} cannot admit members"
        return None if need in self.p.roles[a.role].can else f"role {a.role} cannot {need}"

    def run(self, ordered: list[ev.Event]) -> State:
        for e in ordered:
            t = max([e.ts] + [self.eff.get(pid, 0.0) for pid in e.parents])
            self.eff[e.id] = t
            if e.kind not in ("join", "apply"):
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
    def _place(self, name: str, role_name, parent) -> str | None:
        """Add `name` to the tree as `role_name` under `parent`; the reason it cannot be, if it cannot."""
        if name in self.s.actors:
            return "already joined"
        role = self.p.roles.get(role_name)
        if role is None:
            return f"unknown role {role_name!r}"
        if role.under == "human":
            if parent not in self.p.humans:
                return f"a {role.name} must join under a human ({sorted(self.p.humans)})"
        else:
            pa = self.s.actors.get(parent)
            if pa is None or pa.role != role.under:
                return f"a {role.name} must join under a {role.under}"
            if len(pa.children) >= self.p.roles[pa.role].max_children:
                return f"{parent} already has {len(pa.children)} children (max_children)"
        self.s.actors[name] = Actor(name, role.name, parent, role.model, role.effort)
        if parent in self.s.actors:
            self.s.actors[parent].children.append(name)
        return None

    def on_join(self, e, t):
        if self.s.join_mode != "open":
            return self.ignore(e, f"this project takes members by application (join: {self.s.join_mode})")
        why = self._place(e.actor, e.body["role"], e.body.get("parent"))
        if why:
            self.ignore(e, why)

    # -- the project and its members ----------------------------------------------------------------------------
    def on_project(self, e, t):
        fields = {k: v for k, v in e.body.items()}
        errors = mf.check_fields(fields)
        if not errors and set(fields) - set(mf.FIELDS):
            errors = [f"a project event sets only {', '.join(mf.FIELDS)}"]
        if errors:
            return self.ignore(e, "; ".join(errors))
        if self.s.project_id is None:
            if not fields.get("name"):
                return self.ignore(e, "the first project event (the genesis) needs a name")
            self.s.project_id = e.id
        for k, v in fields.items():
            if v is None:
                self.s.project.pop(k, None)
            else:
                self.s.project[k] = v
        self.s.project_updated = t

    def on_apply(self, e, t):
        if e.actor in self.s.actors:
            return self.ignore(e, "already a member")
        if self.s.join_mode == "closed":
            return self.ignore(e, "this project is closed to new members")
        if any(a.actor == e.actor and a.status == "pending" for a in self.s.applications.values()):
            return self.ignore(e, "an application from this actor is already pending")
        b = e.body
        if b.get("role") not in self.p.roles:
            return self.ignore(e, f"unknown role {b.get('role')!r}")
        offer = b.get("offer", {})
        if not isinstance(offer, dict) or len(ev.canonical(offer)) > 4000:
            return self.ignore(e, "offer must be an object of at most 4000 bytes")
        note = b.get("note", "")
        if not isinstance(note, str) or len(note) > 2000:
            return self.ignore(e, "note must be a string of at most 2000 characters")
        self.s.applications[e.id] = Application(e.id, e.actor, b["role"], b.get("parent"), offer, note, t)

    def _application(self, e) -> Application | None:
        name = e.body["applicant"]
        pending = [a for a in self.s.applications.values() if a.actor == name and a.status == "pending"]
        if not pending:
            self.ignore(e, f"no pending application from {name}")
            return None
        return pending[-1]

    def _may_decide(self, e, parent) -> bool:
        a = self.s.actors[e.actor]
        if a.role == "human" or a.role in self.p.approver_roles or parent == e.actor:
            return True
        self.ignore(e, "a member admits or declines only applicants who would sit under it")
        return False

    def on_admit(self, e, t):
        app = self._application(e)
        if app is None:
            return
        role, parent = e.body.get("role", app.role), e.body.get("parent", app.parent)
        if not self._may_decide(e, parent):
            return
        why = self._place(app.actor, role, parent)
        if why:
            return self.ignore(e, why)
        app.status, app.decided_by = "admitted", e.actor

    def on_decline(self, e, t):
        app = self._application(e)
        if app is None or not self._may_decide(e, app.parent):
            return
        app.status, app.decided_by, app.reason = "declined", e.actor, str(e.body.get("reason", ""))[:500]

    # -- tasks ------------------------------------------------------------------------------------------------
    def on_task(self, e, t):
        b = e.body
        title = str(b.get("title", "")).strip()
        if not title:
            return self.ignore(e, "a task needs a title")
        role = b.get("role")
        if role is not None and role not in self.p.roles:
            return self.ignore(e, f"unknown role {role!r}")
        parent = b.get("parent")
        if parent is not None and parent not in self.s.tasks:
            return self.ignore(e, "no such parent task")
        task = Task(e.id, e.actor, title, str(b.get("detail", "")),
                    tuple(paths.norm(x) for x in _str_list(b.get("paths", []))), _str_list(b.get("topics", [])),
                    role, parent, t)
        self.s.tasks[e.id] = task
        if parent:
            self.s.tasks[parent].subtasks.append(e.id)

    def on_done(self, e, t):
        task = self.s.tasks.get(e.body["task"])
        if task is None:
            return self.ignore(e, "no such task")
        outcome = e.body.get("outcome", "done")
        if outcome not in OUTCOMES:
            return self.ignore(e, f"outcome must be one of {', '.join(OUTCOMES)}")
        lease = self.s.leases.get(task.lease) if task.lease else None
        if not lease or not lease.active(t) or lease.actor != e.actor:
            return self.ignore(e, "only the actor holding the task's lease reports it done")
        task.outcome, task.summary = outcome, str(e.body.get("summary", ""))
        evidence = e.body.get("evidence", {})
        task.evidence = dict(evidence) if isinstance(evidence, dict) else {}
        lease.released, lease.expiry = True, t

    # -- leases -------------------------------------------------------------------------------------------------
    def on_claim(self, e, t):
        b = e.body
        task = None
        if b.get("task_id") is not None:
            task = self.s.tasks.get(b["task_id"])
            if task is None:
                return self.ignore(e, "no such task")
            status = self.s.task_status(task, t)
            if status != "open":
                self.s.denied[e.id] = f"task is {status}"
                return
            role = self.s.actors[e.actor].role
            if task.role and role not in (task.role, "human"):
                self.s.denied[e.id] = f"task is for a {task.role}, not a {role}"
                return
        scope = tuple(paths.norm(x) for x in _str_list(b.get("paths", list(task.paths) if task else [])))
        topics = _str_list(b.get("topics", list(task.topics) if task else []))
        if task is not None:
            topics = tuple(dict.fromkeys(topics + ("task:" + task.id,)))
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
        self.s.leases[e.id] = Lease(e.id, e.actor, str(b.get("task", "") or (task.title if task else "")), scope,
                                    topics, t, ttl, t + ttl)
        if task is not None:
            task.lease = e.id

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

    # -- talk -------------------------------------------------------------------------------------------------
    def on_message(self, e, t):
        b = e.body
        text = b.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_MAX:
            return self.ignore(e, f"a message needs text of at most {MESSAGE_MAX} characters")
        task, to, reply_to = b.get("task"), b.get("to"), b.get("reply_to")
        root = e.id
        if reply_to is not None:
            first = self.s.messages.get(reply_to)
            if first is None:
                return self.ignore(e, "reply to an unknown message")
            if task is not None and task != first.task:
                return self.ignore(e, "a reply stays in its message's task thread")
            task, root = first.task, first.root
        if task is not None and task not in self.s.tasks:
            return self.ignore(e, "no such task")
        if to is not None and to not in self.s.actors:
            return self.ignore(e, f"{to} is not a member")
        self.s.messages[e.id] = Message(e.id, len(self.s.messages), e.actor, text, task, to, reply_to, root, t)

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
