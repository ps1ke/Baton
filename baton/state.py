"""Fold the event log into state. Every decision is made here, from the events alone, so peers that hold the same events
agree on every member, assignment, lease, check, budget and approval without asking anyone.

Decisions use an event's *effective* time: its own timestamp, but never earlier than its parents' effective times, so an
event cannot be backdated before what its author had already seen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import events as ev
from . import manifest as mf
from . import network
from . import paths
from . import wp
from .policy import KINDS, TIERS, Policy, tier_index


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
    model: str = ""
    package: str | None = None

    def active(self, t: float) -> bool:
        return not self.released and self.start <= t < self.expiry

    def collides(self, scope_paths, scope_topics) -> bool:
        return paths.any_overlap(self.paths, scope_paths) or bool(set(self.topics) & set(scope_topics))


@dataclass
class Task:
    """A work package (wp.py) and where it stands."""
    id: str
    by: str
    title: str
    kind: str
    level: int
    parent: str | None
    created: float
    goal: str = ""
    detail: str = ""
    paths: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    acceptance: list = field(default_factory=list)
    deliverables: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    size: str | None = None
    tier: str = "standard"
    hardware: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    of: str | None = None              # a test or review: the implement package it checks
    base: str | None = None            # the repository commit the package starts from (children inherit it)
    bundle: str | None = None          # a blob: the base commit as a git bundle, for repositories only the author has
    lease: str | None = None           # the lease that holds it now (or last held it)
    outcome: str | None = None         # done failed escalated
    summary: str = ""
    report: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    subtasks: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)   # implement: its test and review packages, in order
    ended: float | None = None
    phase: str = ""                    # orchestrate: "" | waiting | integrate; implement: "" | checking
    rework: int = 0
    feedback: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)    # {member, model, family, lease}
    round_start: int = 0               # runs before the current round (a rework or an integration starts one)

    @property
    def role(self):   # older readers
        return self.kind


FINAL = ("done", "failed", "escalated")


@dataclass
class Application:
    id: str
    actor: str
    offer: dict
    key: str
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
        return self.status in ("pending", "approved", "executed")


@dataclass
class Message:
    id: str
    seq: int
    actor: str
    text: str
    task: str | None
    to: str | None
    reply_to: str | None
    root: str
    ts: float
    ask: bool = False            # a question that waits for a reply


MESSAGE_MAX = 4000
LEVEL0_ACTIONS = ("push",)   # actions only level 0 may even propose: writing to the project's repository


@dataclass
class Actor:
    name: str
    kind: str                    # human | member
    joined: float = 0.0
    offer: dict = field(default_factory=dict)
    offer_at: float | None = None
    key: str = ""                # the one public key whose events count as this actor's
    right: str = "member"        # author | steward | member (humans in a private log: "human")
    revoked: bool = False

    @property
    def role(self):              # older readers
        return "human" if self.kind == "human" else "member"

    @property
    def level0(self) -> bool:
        """May make level-0 decisions: plan and integrate the top level, admit, approve, push."""
        return not self.revoked and self.right in ("author", "steward", "human")

    def present(self, t: float) -> bool:
        if self.offer_at is None or self.revoked:
            return False
        return self.offer_at <= t < self.offer_at + float(self.offer.get("ttl", 0))


@dataclass
class State:
    policy: Policy = field(default_factory=Policy)
    actors: dict[str, Actor] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)
    denied: dict[str, str] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    applications: dict[str, Application] = field(default_factory=dict)
    messages: dict[str, Message] = field(default_factory=dict)
    project_id: str | None = None
    project: dict = field(default_factory=dict)
    project_updated: float = 0.0
    ignored: dict[str, str] = field(default_factory=dict)
    author: str | None = None                                   # project mode: who signed the genesis
    policy_from: str | None = None                              # the `policy` event in force, if any

    @property
    def mode(self) -> str:
        """project: authority comes from the genesis signer's key. private: a log without a project, where the
        humans listed in the local policy.toml decide (each bound to the first key they sign with)."""
        return "project" if self.project_id else "private"

    def stewards(self) -> list[Actor]:
        return [a for a in self.actors.values() if a.level0 and a.right in ("author", "steward")]

    @property
    def join_mode(self) -> str:
        return self.project.get("join", "apply") if self.project_id else "open"

    def members(self) -> list[Actor]:
        return [a for a in self.actors.values() if a.kind == "member"]

    # -- packages -------------------------------------------------------------------------------------------------
    def task_status(self, task: Task, t: float) -> str:
        """open, taken, waiting (for children or dependencies), checking (in its pipeline), blocked, or the outcome."""
        if task.outcome:
            return task.outcome
        lease = self.leases.get(task.lease) if task.lease else None
        if lease and lease.active(t):
            return "taken"
        if task.phase == "checking":
            return "checking"
        if task.phase != "waiting" and len(task.runs) - task.round_start >= self.policy.max_attempts:
            return "stalled"   # taken this many times without a report: a human looks (release it to retry)
        if task.phase == "waiting":
            return "open" if all(self.tasks[c].outcome for c in task.subtasks) else "waiting"
        for d in task.depends_on:
            dep = self.tasks.get(d)
            if dep is None or dep.outcome in ("failed", "escalated"):
                return "blocked"
            if dep.outcome != "done":
                return "waiting"
        return "open"

    def open_tasks(self, t: float, role=None) -> list[Task]:
        """Packages nobody holds whose scope is free right now, oldest first."""
        out = []
        for task in sorted(self.tasks.values(), key=lambda x: (x.created, x.id)):
            if self.task_status(task, t) == "open" and not self.conflicts(task.paths, task.topics, t):
                out.append(task)
        return out

    def assignment(self, t: float) -> dict:
        return network.assign(self, self.policy, t)

    def active_leases(self, t: float) -> list[Lease]:
        return [l for l in self.leases.values() if l.active(t)]

    def conflicts(self, scope_paths, scope_topics, t: float, actor: str | None = None) -> list[Lease]:
        return [l for l in self.active_leases(t) if l.actor != actor and l.collides(scope_paths, scope_topics)]

    def covered(self, actor: str, scope_paths, t: float) -> bool:
        held = [l for l in self.active_leases(t) if l.actor == actor]
        return all(any(paths.inside(p, lp) for l in held for lp in l.paths) for p in scope_paths)

    def holds(self, actor: str, task_id: str, t: float) -> bool:
        task = self.tasks.get(task_id)
        lease = self.leases.get(task.lease) if task and task.lease else None
        return bool(lease and lease.active(t) and lease.actor == actor)

    def ancestors(self, task_id: str) -> list[str]:
        out, seen = [], set()
        task = self.tasks.get(task_id)
        while task and task.parent and task.parent not in seen:
            seen.add(task.parent)
            out.append(task.parent)
            task = self.tasks.get(task.parent)
        return out

    # -- messages -------------------------------------------------------------------------------------------------
    def thread(self, task_id: str) -> list[Message]:
        return [m for m in self.messages.values() if m.task == task_id]

    def replied(self, m: Message) -> bool:
        return any(x.reply_to == m.id for x in self.messages.values())

    def _answered(self, m: Message, actor: str) -> bool:
        return any(x.actor == actor and x.seq > m.seq and (x.reply_to == m.id or (m.task and x.task == m.task))
                   for x in self.messages.values())

    def inbox(self, actor: str, unanswered: bool = True) -> list[Message]:
        """Messages for `actor`: questions (`ask`) addressed to it or asked in the thread of a package it posted, until
        someone replies; other messages addressed to it, replying to it, or posted to a package it posted or worked
        on, until it answers."""
        mine = {t.id for t in self.tasks.values()
                if t.by == actor or any(r["member"] == actor for r in t.runs)}
        posted = {t.id for t in self.tasks.values() if t.by == actor}
        wrote = {m.id for m in self.messages.values() if m.actor == actor}
        out = []
        for m in self.messages.values():
            if m.ask:
                if m.to == actor or (m.to is None and m.task in posted):
                    if not unanswered or not self.replied(m):
                        out.append(m)
                continue
            if m.actor == actor:
                continue
            if m.to == actor or m.reply_to in wrote or (m.to is None and m.task in mine):
                if not unanswered or not self._answered(m, actor):
                    out.append(m)
        return out


def _str_list(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of strings")
    return tuple(value)


def check_offer(o) -> list[str]:
    if not isinstance(o, dict):
        return ["an offer must be an object"]
    errors = []
    if len(ev.canonical(o)) > 8000:
        return ["an offer must be at most 8000 bytes"]
    ms = o.get("models", [])
    if not isinstance(ms, list) or len(ms) > 50 or not all(
            isinstance(m, dict) and isinstance(m.get("id"), str) and ":" in m["id"] and m.get("tier") in TIERS
            for m in ms):
        errors.append('models must be a list of {"id": "provider:model", "tier", "family"}')
    slots = o.get("slots", 0)
    if not isinstance(slots, int) or isinstance(slots, bool) or not 0 <= slots <= 64:
        errors.append("slots must be an integer 0..64")
    hw = o.get("hardware")
    if hw is not None and (not isinstance(hw, dict) or not isinstance(hw.get("tags", []), list)):
        errors.append("hardware must be an object with a tags list")
    return errors


class _Folder:
    def __init__(self, policy: Policy):
        self.p = policy
        self.s = State(policy=policy)
        self.eff: dict[str, float] = {}

    def ignore(self, e: ev.Event, why: str):
        self.s.ignored[e.id] = why

    AUTHOR_ONLY = {"policy", "grant"}
    LEVEL0 = {"project", "admit", "decline", "approve", "reject", "executed", "revoke"}

    def allowed(self, e: ev.Event) -> str | None:
        a = self.s.actors.get(e.actor)
        if a is None:
            return "actor has not joined"
        if a.revoked:
            return f"{e.actor} was revoked"
        if a.key and e.key != a.key:
            return f"signed by a key that is not {e.actor}'s"
        if not a.key:
            a.key = e.key   # a private log's human: bound to the first key it signs with
        if e.kind in self.AUTHOR_ONLY:
            return None if a.right == "author" or (self.s.mode == "private" and a.right == "human") \
                else f"only the author may {e.kind}"
        if e.kind in self.LEVEL0 and not (a.level0 or e.actor in self.p.approvers):
            return f"{e.kind} is a level-0 decision (author and stewards)"
        return None

    def run(self, ordered: list[ev.Event]) -> State:
        genesis = ordered[0] if ordered and ordered[0].kind == "project" and ordered[0].body.get("name") else None
        if genesis is None:   # a private log: the local policy names its humans
            for h in sorted(self.p.humans):
                self.s.actors[h] = Actor(h, "human", right="human")
        for e in ordered:
            t = max([e.ts] + [self.eff.get(pid, 0.0) for pid in e.parents])
            self.eff[e.id] = t
            if e is genesis:
                self.s.actors[e.actor] = Actor(e.actor, "human" if e.actor.startswith("human:") else "member",
                                               joined=t, key=e.key, right="author")
                self.s.author = e.actor
            if e.kind not in ("join", "apply"):
                why = self.allowed(e)
                if why:
                    self.ignore(e, why)
                    continue
            try:
                getattr(self, "on_" + e.kind)(e, t)
            except (ValueError, TypeError, KeyError, AttributeError) as err:
                self.ignore(e, f"bad body: {err}")
        return self.s

    # -- authority: the author grants and revokes, and sets the policy ----------------------------------------------
    def on_grant(self, e, t):
        """{to, key, right: steward|member}: make `to` a steward (level 0) or a member, bound to `key`."""
        to, key, right = str(e.body["to"]), str(e.body.get("key", "")), e.body.get("right", "steward")
        if right not in ("steward", "member"):
            return self.ignore(e, "right must be steward or member")
        if not (len(key) == 64 and all(c in "0123456789abcdef" for c in key)):
            return self.ignore(e, "a grant names the public key it binds (64 hex characters)")
        if to == self.s.author:
            return self.ignore(e, "the author already holds every right")
        a = self.s.actors.get(to)
        if a is None:
            a = Actor(to, "human" if to.startswith("human:") else "member", joined=t)
            self.s.actors[to] = a
        a.key, a.right, a.revoked = key, right, False

    def on_revoke(self, e, t):
        target = self.s.actors.get(str(e.body["actor"]))
        if target is None:
            return self.ignore(e, "no such actor")
        if target.right == "author":
            return self.ignore(e, "the author cannot be revoked")
        if target.right == "steward" and self.s.actors[e.actor].right != "author":
            return self.ignore(e, "only the author revokes a steward")
        target.revoked = True
        for l in self.s.active_leases(t):
            if l.actor == target.name:
                l.released, l.expiry = True, t

    def on_policy(self, e, t):
        from .policy import parse
        text = e.body.get("toml")
        if not isinstance(text, str) or len(text) > 20000:
            return self.ignore(e, "a policy event carries the policy.toml text (at most 20000 characters)")
        try:
            new = parse(text)
        except ValueError as err:
            return self.ignore(e, f"bad policy: {err}")
        self.p = new
        self.s.policy = new
        self.s.policy_from = e.id

    # -- members --------------------------------------------------------------------------------------------------
    def _member(self, name: str, offer, t: float, key: str) -> str | None:
        if name in self.s.actors:
            return "already a member"
        if name.startswith("human:"):
            return "human:... names are for the author and the humans it grants"
        offer = offer or {}
        errors = check_offer(offer)
        if errors:
            return "; ".join(errors)
        a = Actor(name, "member", joined=t, key=key)
        self.s.actors[name] = a
        if offer.get("models"):
            self._set_offer(a, offer, t)
        return None

    def _set_offer(self, a: Actor, offer: dict, t: float):
        a.offer = dict(offer)
        a.offer["ttl"] = min(float(offer.get("ttl", self.p.presence_ttl)), self.p.presence_ttl)
        a.offer_at = t

    def on_join(self, e, t):
        if self.s.join_mode != "open":
            return self.ignore(e, f"this project takes members by application (join: {self.s.join_mode})")
        why = self._member(e.actor, e.body.get("offer"), t, e.key)
        if why:
            self.ignore(e, why)

    def on_offer(self, e, t):
        a = self.s.actors[e.actor]
        if a.kind != "member":
            return self.ignore(e, "only members offer machines")
        errors = check_offer(e.body)
        if errors:
            return self.ignore(e, "; ".join(errors))
        self._set_offer(a, e.body, t)

    # -- the project and its members ----------------------------------------------------------------------------
    def on_project(self, e, t):
        fields = dict(e.body)
        errors = mf.check_fields(fields)
        if not errors and set(fields) - set(mf.FIELDS):
            errors = [f"a project event sets only {', '.join(mf.FIELDS)}"]
        if errors:
            return self.ignore(e, "; ".join(errors))
        if self.s.project_id is None:
            if not fields.get("name"):
                return self.ignore(e, "the first project event (the genesis) needs a name")
            if not fields.get("repo") or not mf.git_url_ok(fields["repo"]) and not str(fields["repo"]).startswith("https://"):
                return self.ignore(e, "a project starts from a repository: the genesis needs repo (its git URL)")
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
        if e.actor.startswith("human:"):
            return self.ignore(e, "human:... names are for the humans listed in the policy")
        if self.s.join_mode == "closed":
            return self.ignore(e, "this project is closed to new members")
        if any(a.actor == e.actor and a.status == "pending" for a in self.s.applications.values()):
            return self.ignore(e, "an application from this actor is already pending")
        offer = e.body.get("offer", {})
        errors = check_offer(offer) if offer else []
        if errors:
            return self.ignore(e, "; ".join(errors))
        note = e.body.get("note", "")
        if not isinstance(note, str) or len(note) > 2000:
            return self.ignore(e, "note must be a string of at most 2000 characters")
        self.s.applications[e.id] = Application(e.id, e.actor, offer, e.key, note, t)

    def _application(self, e) -> Application | None:
        name = e.body["applicant"]
        pending = [a for a in self.s.applications.values() if a.actor == name and a.status == "pending"]
        if not pending:
            self.ignore(e, f"no pending application from {name}")
            return None
        return pending[-1]

    def on_admit(self, e, t):
        app = self._application(e)
        if app is None:
            return
        why = self._member(app.actor, app.offer, t, app.key)
        if why:
            return self.ignore(e, why)
        app.status, app.decided_by = "admitted", e.actor

    def on_decline(self, e, t):
        app = self._application(e)
        if app is None:
            return
        app.status, app.decided_by, app.reason = "declined", e.actor, str(e.body.get("reason", ""))[:500]

    # -- work packages --------------------------------------------------------------------------------------------
    def on_task(self, e, t):
        b = e.body
        errors = wp.check_package(b)
        if errors:
            return self.ignore(e, "; ".join(errors))
        human = self.s.actors[e.actor].level0 or e.actor in self.p.approvers
        parent = self.s.tasks.get(b["parent"]) if b.get("parent") else None
        if b.get("parent") and parent is None:
            return self.ignore(e, "no such parent package")
        if parent and parent.kind != "orchestrate":
            return self.ignore(e, "only an orchestrate package has child packages")
        kind = b.get("kind") or wp.default_kind(b)
        scope = tuple(paths.norm(x) for x in _str_list(b.get("paths")))
        if not human:
            if parent is None:
                return self.ignore(e, "members post packages only as children of an orchestrate package they hold")
            if not self.s.holds(e.actor, parent.id, t):
                return self.ignore(e, "post children only while holding the parent package's lease")
            if kind not in wp.CHILD_KINDS:
                return self.ignore(e, f"an orchestrator posts {', '.join(wp.CHILD_KINDS)} packages")
            if parent.paths and not all(any(paths.inside(p, q) for q in parent.paths) for p in scope or ("",)):
                return self.ignore(e, "a child's paths must lie inside its parent's")
        level = parent.level + 1 if parent else 0
        if kind == "orchestrate" and level >= self.p.max_depth:
            return self.ignore(e, f"level {level} is below the deepest orchestration level ({self.p.max_depth - 1})")
        if parent and len(parent.subtasks) >= self.p.max_children:
            return self.ignore(e, f"the parent already has {len(parent.subtasks)} children (max_children)")
        deps = _str_list(b.get("depends_on"))
        if any(d not in self.s.tasks or self.s.tasks[d].parent != (parent.id if parent else None) for d in deps):
            return self.ignore(e, "depends_on must name sibling packages")
        need = self.p.tier_for(kind, level)
        asked = (b.get("needs") or {}).get("tier")
        tier = asked if asked and tier_index(asked) > tier_index(need) else need
        task = Task(e.id, e.actor, b["title"].strip(), kind, level, parent.id if parent else None, t,
                    goal=b.get("goal", ""), detail=b.get("context") or b.get("detail") or "", paths=scope,
                    topics=_str_list(b.get("topics")), acceptance=list(b.get("acceptance") or []),
                    deliverables=list(b.get("deliverables") or []), constraints=list(b.get("constraints") or []),
                    inputs=list(b.get("inputs") or []), size=b.get("size"), tier=tier,
                    hardware=_str_list((b.get("needs") or {}).get("hardware")), depends_on=deps, base=b.get("base"), bundle=b.get("bundle"))
        self.s.tasks[e.id] = task
        if parent:
            parent.subtasks.append(e.id)

    def _derive(self, impl: Task, step: int, t: float) -> Task:
        kind = self.p.pipeline[step]
        pid = ev.digest({"derived": kind, "of": impl.id, "round": impl.rework, "step": step})
        title = ("Test: " if kind == "test" else "Review: ") + impl.title
        goal = ("Run the acceptance checks of the package and report each one; change nothing." if kind == "test" else
                "Review the change made for the package: correctness, the acceptance criteria, the constraints, "
                "risks. Change nothing; report findings with severities and a verdict.")
        d = Task(pid, "network", title, kind, impl.level, None, t, goal=goal, detail=impl.goal,
                 paths=impl.paths, topics=impl.topics + (f"check:{impl.id[:12]}",), acceptance=impl.acceptance,
                 constraints=impl.constraints, tier=self.p.tier_for(kind, impl.level), hardware=impl.hardware,
                 of=impl.id)
        self.s.tasks[pid] = d
        impl.checks.append(pid)
        return d

    def _finish(self, task: Task, outcome: str, t: float):
        task.outcome, task.ended = outcome, t

    def _check_done(self, check: Task, outcome: str, t: float):
        """A test or review of an implement package ended: pass it on, accept it, or send it back."""
        impl = self.s.tasks[check.of]
        if outcome == "escalated":
            impl.summary = f"its {check.kind} escalated: {check.summary}"
            return self._finish(impl, "escalated", t)
        ok = outcome == "done" and (wp.tests_passed(check.report) if check.kind == "test"
                                    else wp.review_passed(check.report))
        if ok:
            step = self.p.pipeline.index(check.kind) + 1
            if step < len(self.p.pipeline):
                self._derive(impl, step, t)
            else:
                impl.phase = ""
                self._finish(impl, "done", t)
            return
        impl.feedback.append(wp.failure_text(check.kind, check.summary, check.report))
        impl.rework += 1
        impl.phase, impl.lease = "", None
        impl.round_start = len(impl.runs)
        if impl.rework > self.p.max_rework:
            impl.summary = f"failed its checks {impl.rework} times; last: {check.summary}"
            self._finish(impl, "escalated", t)

    def on_done(self, e, t):
        task = self.s.tasks.get(e.body["task"])
        if task is None:
            return self.ignore(e, "no such package")
        outcome = e.body.get("outcome", "done")
        if outcome not in wp.OUTCOMES:
            return self.ignore(e, f"outcome must be one of {', '.join(wp.OUTCOMES)}")
        lease = self.s.leases.get(task.lease) if task.lease else None
        if not lease or not lease.active(t) or lease.actor != e.actor:
            return self.ignore(e, "only the actor holding the package's lease reports it")
        report = e.body.get("report", {})
        errors = wp.check_report(report)
        if errors:
            return self.ignore(e, "; ".join(errors))
        if outcome == "split":
            if task.kind != "orchestrate":
                return self.ignore(e, "only an orchestrate package is split")
            if not any(not self.s.tasks[c].outcome for c in task.subtasks):
                return self.ignore(e, "split means children are running: post them first")
        elif outcome == "done" and task.kind == "orchestrate" and any(not self.s.tasks[c].outcome for c in task.subtasks):
            return self.ignore(e, "children are still running: report split, and integrate when they end")
        task.summary, task.report = str(e.body.get("summary", ""))[:2000], dict(report)
        evidence = e.body.get("evidence", {})
        task.evidence = dict(evidence) if isinstance(evidence, dict) else {}
        lease.released, lease.expiry = True, t
        if outcome == "split":
            task.phase = "waiting"
            task.round_start = len(task.runs)
            return
        if task.kind == "implement" and outcome == "done" and self.p.pipeline:
            task.phase = "checking"
            self._derive(task, 0, t)
            return
        self._finish(task, outcome, t)
        if task.of:
            self._check_done(task, outcome, t)

    # -- leases -------------------------------------------------------------------------------------------------
    def on_claim(self, e, t):
        b = e.body
        actor = self.s.actors[e.actor]
        task = None
        model = None
        if b.get("task_id") is not None:
            task = self.s.tasks.get(b["task_id"])
            if task is None:
                return self.ignore(e, "no such package")
            status = self.s.task_status(task, t)
            if status != "open":
                self.s.denied[e.id] = f"package is {status}"
                return
            if actor.kind == "member":
                pick = network.assign(self.s, self.p, t).get(task.id)
                if pick is None or pick.member != e.actor:
                    self.s.denied[e.id] = network.why_not(self.s, self.p, t, task.id, e.actor)
                    return
                model = next((m for m in actor.offer.get("models", []) if m["id"] == b.get("model", pick.model)), None)
                want = network.need(task, self.p, t)
                if model is None or tier_index(model["tier"]) < tier_index(want):
                    self.s.denied[e.id] = f"the model must be one {e.actor} offers, of tier {want} or better"
                    return
        elif actor.kind == "member" and not actor.present(t):
            self.s.denied[e.id] = "a member claims only while present (send an offer first)"
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
        ref = model["id"] if model else str(b.get("model", ""))
        self.s.leases[e.id] = Lease(e.id, e.actor, str(b.get("task", "") or (task.title if task else "")), scope,
                                    topics, t, ttl, t + ttl, model=ref, package=task.id if task else None)
        if task is not None:
            task.lease = e.id
            task.runs.append({"member": e.actor, "model": ref, "family": (model or {}).get("family", ""),
                              "lease": e.id, "at": t})

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
        if e.actor != lease.actor and a.kind != "human" and e.actor not in self.p.approvers:
            return self.ignore(e, "only the holder, humans and approvers release a lease")
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
        self.s.messages[e.id] = Message(e.id, len(self.s.messages), e.actor, text, task, to, reply_to, root, t,
                                        ask=bool(b.get("ask")))

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
        if action in LEVEL0_ACTIONS and not self.s.actors[e.actor].level0:
            pr.status, pr.reason = "not_level0", f"only the author and its stewards may {action}"
            return
        same = [q for q in self.s.proposals.values() if q is not pr and q.key == key and q.counts]
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
        auto = self.p.auto.get(action, ())
        if e.actor in auto or ("stewards" in auto and self.s.actors[e.actor].level0):
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
    """The events whose whole history is present, in fold order, and why the others are held back."""
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
