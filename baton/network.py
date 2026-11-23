"""The network decides who does what.

`assign(state, policy, t)` gives every open work package to one present member and one of its models, or to nobody.
It reads only the folded state, so every peer computes the same answer, and the fold refuses a claim on a package
the network gave to someone else (state.on_claim). Nobody chooses to be an orchestrator, an implementer or a
reviewer: a member is whatever the packages it is given need.

The rule, package by package in a fixed order (checks first, since they unblock finished work; then orchestration,
shallow levels first; then the rest, oldest first):

1. Candidates: members present at t (an offer within its ttl) with a free slot (slots minus active leases), offering
   a model of at least the package's tier, and every hardware tag the package needs.
2. For a review, a model of another family than the one that wrote the change, and another member, are preferred
   (an independent second opinion); for a test, another member is preferred.
3. Then the smallest tier above what is needed (a package nobody can serve accepts one tier lower after each
   `downgrade_after`) (do not spend a frontier model on a chore), then the least loaded
   member, then a hash of package and member (an even, deterministic spread).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .policy import TIERS, Policy, tier_index

ORDER = {"review": 0, "test": 0, "orchestrate": 1, "investigate": 2, "implement": 3}


@dataclass(frozen=True)
class Pick:
    member: str
    model: str          # "provider:model"
    tier: str
    family: str


def need(p, policy: Policy, t: float) -> str:
    """The tier a package needs at time t. A package nobody can take is not stuck forever: after `downgrade_after`
    it accepts one tier lower, and so on (deterministic, so every peer agrees)."""
    base = tier_index(p.tier)
    if policy.downgrade_after > 0:
        base -= int(max(0.0, t - p.created) // policy.downgrade_after)
    return TIERS[max(0, base)]


def _spread(pkg_id: str, member: str) -> str:
    return hashlib.sha256(f"{pkg_id}/{member}".encode()).hexdigest()


def best_model(offer: dict, need: str, avoid_family: str | None = None):
    """The cheapest offered model good enough for `need`, preferring another family than `avoid_family`."""
    ok = [m for m in offer.get("models", []) if m.get("tier") in ("fast", "standard", "strong", "frontier")
          and tier_index(m["tier"]) >= tier_index(need)]
    if not ok:
        return None
    return min(ok, key=lambda m: (bool(avoid_family) and m.get("family") == avoid_family, tier_index(m["tier"]), m["id"]))


def author_of_change(state, pkg) -> tuple[str | None, str | None]:
    """(member, family) that last worked on the implement package a test or review checks."""
    target = state.tasks.get(pkg.of) if pkg.of else None
    if not target or not target.runs:
        return None, None
    last = target.runs[-1]
    return last["member"], last.get("family")


def load(state, t: float) -> dict[str, int]:
    used: dict[str, int] = {}
    for l in state.active_leases(t):
        used[l.actor] = used.get(l.actor, 0) + 1
    return used


def assign(state, policy: Policy, t: float) -> dict[str, Pick]:
    members = [a for a in state.actors.values() if a.kind == "member" and a.present(t)]
    used = load(state, t)
    free = {a.name: max(0, int(a.offer.get("slots", 0)) - used.get(a.name, 0)) for a in members}
    out: dict[str, Pick] = {}
    order = sorted(state.open_tasks(t), key=lambda p: (ORDER.get(p.kind, 9), p.level, p.created, p.id))
    for p in order:
        writer, family = author_of_change(state, p) if p.kind in ("review", "test") else (None, None)
        fit = [a for a in members if set(p.hardware) <= set((a.offer.get("hardware") or {}).get("tags", []))]
        # the package's own tier while any present member can serve it (even if busy now); lower ones only after
        # it waited (need()) and nobody present can serve the tier above
        want = p.tier
        floor = tier_index(need(p, policy, t))
        while tier_index(want) > floor and not any(best_model(a.offer, want) for a in fit):
            want = TIERS[tier_index(want) - 1]
        best = None
        for a in fit:
            if free[a.name] <= 0:
                continue
            m = best_model(a.offer, want, family if p.kind == "review" else None)
            if m is None:
                continue
            slots = max(1, int(a.offer.get("slots", 1)))
            key = (p.kind == "review" and family is not None and m.get("family") == family,
                   writer is not None and a.name == writer,
                   tier_index(m["tier"]) - tier_index(want),
                   used.get(a.name, 0) / slots,
                   _spread(p.id, a.name))
            if best is None or key < best[0]:
                best = (key, Pick(a.name, m["id"], m["tier"], m.get("family", "")))
        if best:
            out[p.id] = best[1]
            free[best[1].member] -= 1
            used[best[1].member] = used.get(best[1].member, 0) + 1
    return out


def why_not(state, policy: Policy, t: float, pkg_id: str, member: str) -> str:
    """Why `member` was not given package `pkg_id` (for refusals and the dashboard)."""
    a = state.actors.get(member)
    p = state.tasks.get(pkg_id)
    if p is None:
        return "no such package"
    if a is None or a.kind != "member":
        return f"{member} is not a member"
    if not a.present(t):
        return f"{member} is not present (no current offer: run `baton swarm` or `baton offer`)"
    if not set(p.hardware) <= set((a.offer.get("hardware") or {}).get("tags", [])):
        return f"it needs hardware {', '.join(p.hardware)}"
    if best_model(a.offer, need(p, policy, t)) is None:
        return f"it needs a {p.tier} model (or {need(p, policy, t)} after waiting) and {member} offers none"
    pick = assign(state, policy, t).get(pkg_id)
    if pick is None:
        return f"{member} has no free slot"
    return f"the network gave it to {pick.member} ({pick.model})"
