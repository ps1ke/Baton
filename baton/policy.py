"""Policy: how the project's network shares out work, the pipeline every change goes through, lease lengths, rate
budgets and the approval gate, read from policy.toml.

There are no roles to pick. Members offer machines (models with tiers, slots, hardware); the network gives each work
package to a member and a model by the rules here (see network.py). What a package needs is set by its kind and, for
orchestration, by its level in the package tree.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
TIERS = ("fast", "standard", "strong", "frontier")
KINDS = ("orchestrate", "implement", "test", "review", "investigate")


def duration(text) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", str(text))
    if not m:
        raise ValueError(f"bad duration {text!r} (use e.g. 90s, 30m, 2h, 1d)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def rate(text) -> tuple[int, float] | None:
    """"5/1h" -> (5, 3600.0); None means no limit."""
    if text is None:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(.+)", str(text))
    if not m:
        raise ValueError(f"bad rate {text!r} (use e.g. 5/1h)")
    return int(m.group(1)), duration(m.group(2))


def tier_index(t: str) -> int:
    return TIERS.index(t)


@dataclass
class Budget:
    per_actor: tuple[int, float] | None = None
    total: tuple[int, float] | None = None
    needs_lease: bool = False


@dataclass
class Policy:
    budgets: dict[str, Budget] = field(default_factory=dict)
    humans: frozenset = frozenset()
    approvers: frozenset = frozenset()          # members that may also approve, execute, admit and govern
    auto: dict[str, frozenset] = field(default_factory=dict)   # action -> members whose proposals skip approval
    default_ttl: float = 7200.0
    max_ttl: float = 28800.0
    presence_ttl: float = 180.0                 # an offer counts this long unless renewed
    max_depth: int = 3                          # orchestration levels 0 .. max_depth-1
    max_children: int = 8                       # packages one orchestrate package may post
    max_rework: int = 2                         # failed checks before an implement package escalates
    max_attempts: int = 3                       # runs without a report before a package stalls
    downgrade_after: float = 600.0              # a package nobody can serve accepts one tier lower after this
    orchestrate: tuple[str, ...] = ("frontier", "strong", "standard")   # tier by level
    tiers: dict[str, str] = field(default_factory=lambda: {"implement": "standard", "test": "fast",
                                                            "review": "strong", "investigate": "fast"})
    pipeline: tuple[str, ...] = ("test", "review")   # what an implement package goes through before it is done

    def tier_for(self, kind: str, level: int) -> str:
        if kind == "orchestrate":
            return self.orchestrate[min(level, len(self.orchestrate) - 1)]
        return self.tiers.get(kind, "standard")

    def may_decide(self, actor: str) -> bool:
        return actor in self.humans or actor in self.approvers


DEFAULT = """
# baton policy. Every peer must fold with the same file: keep it in the shared log folder.
#
# Nobody picks a role. Members offer machines (allowed models with tiers, slots, hardware) and the network gives each
# work package to the member and model that fit it best (cheapest tier that is good enough, least loaded, a different
# model family for reviews). What runs on a machine is that machine's own choice (`baton machine`), never the policy's.

[leases]
default_ttl = "2h"
max_ttl = "8h"

[network]
presence_ttl = "3m"     # a member counts as present this long after its last offer
max_depth = 3           # orchestration levels 0, 1, 2; deeper packages cannot be split
max_children = 8        # packages one orchestrate package may post
max_rework = 2          # an implement package that fails its checks this many times more escalates
max_attempts = 3        # runs that end without a report before a package stalls for a human
downgrade_after = "10m" # a package no present machine can serve accepts one tier lower after this ("0s": never)

# The tier each kind of work needs. Tiers: fast < standard < strong < frontier.
[tiers]
orchestrate = ["frontier", "strong", "standard"]   # by level: 0 plans the whole, deeper levels plan parts
implement = "standard"
test = "fast"
review = "strong"
investigate = "fast"

# Every implement package goes through these before it counts as done; a failure sends it back for rework.
[pipeline]
implement = ["test", "review"]

# In a project, authority comes from keys: the author (who signed the genesis) and the stewards it grants. `humans`
# only matters in a private log with no project.
[gate]
humans = []
approvers = []

[budgets.comment]
per_actor = "3/1h"
total = "20/1h"

[budgets.issue_comment]
per_actor = "3/1h"
total = "20/1h"

[budgets.pr]
per_actor = "1/4h"
total = "5/4h"
needs_lease = true

# Pushing integrated work to the project's repository: only the author and its stewards (level 0) may propose it,
# and the author or a human steward approves it, unless `[gate] auto = {push = ["stewards"]}` lets stewards decide.
[budgets.push]
per_actor = "2/1h"
total = "4/1h"
"""


def parse(text: str) -> Policy:
    raw = tomllib.loads(text)
    p = Policy()
    leases = raw.get("leases", {})
    p.default_ttl = duration(leases.get("default_ttl", "2h"))
    p.max_ttl = duration(leases.get("max_ttl", "8h"))
    net = raw.get("network", {})
    p.presence_ttl = duration(net.get("presence_ttl", "3m"))
    p.max_depth = int(net.get("max_depth", 3))
    p.max_children = int(net.get("max_children", 8))
    p.max_rework = int(net.get("max_rework", 2))
    p.max_attempts = int(net.get("max_attempts", 3))
    p.downgrade_after = duration(net.get("downgrade_after", "10m"))
    tiers = dict(raw.get("tiers", {}))
    orch = tiers.pop("orchestrate", list(p.orchestrate))
    p.orchestrate = tuple([orch] if isinstance(orch, str) else orch) or p.orchestrate
    p.tiers.update(tiers)
    for t in list(p.orchestrate) + list(p.tiers.values()):
        if t not in TIERS:
            raise ValueError(f"unknown tier {t!r} (use {', '.join(TIERS)})")
    for k in p.tiers:
        if k not in KINDS:
            raise ValueError(f"unknown kind of work {k!r} in [tiers]")
    pipe = raw.get("pipeline", {}).get("implement", list(p.pipeline))
    if any(k not in ("test", "review") for k in pipe):
        raise ValueError("[pipeline] implement may list test and review")
    p.pipeline = tuple(pipe)
    for action, b in raw.get("budgets", {}).items():
        p.budgets[action] = Budget(rate(b.get("per_actor")), rate(b.get("total")), bool(b.get("needs_lease", False)))
    gate = raw.get("gate", {})
    p.humans = frozenset(gate.get("humans", []))
    p.approvers = frozenset(gate.get("approvers", []))
    p.auto = {action: frozenset(who) for action, who in gate.get("auto", {}).items()}
    if "roles" in raw or "runner" in raw:
        raise ValueError("this policy is from baton 0.4: roles and [runner] are gone (the network assigns work and "
                         "each machine chooses its own agents); start from `baton init` defaults")
    return p


def load(home: Path) -> Policy:
    path = Path(home) / "policy.toml"
    return parse(path.read_text(encoding="utf-8") if path.is_file() else DEFAULT)
