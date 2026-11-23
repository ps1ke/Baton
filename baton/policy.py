"""Policy: the tiers (role, model, effort, fan-out), the rate budgets and the approval gate, read from policy.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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


@dataclass
class Role:
    name: str
    under: str | None          # the parent's role, or "human"
    model: str = ""
    effort: str = ""
    max_children: int = 0
    can: frozenset = frozenset()
    runner: list[str] | None = None   # overrides [runner] cmd for this role


@dataclass
class Budget:
    per_actor: tuple[int, float] | None = None
    total: tuple[int, float] | None = None
    needs_lease: bool = False


@dataclass
class Policy:
    roles: dict[str, Role] = field(default_factory=dict)
    budgets: dict[str, Budget] = field(default_factory=dict)
    humans: frozenset = frozenset()
    approver_roles: frozenset = frozenset()
    auto: dict[str, frozenset] = field(default_factory=dict)   # action -> roles that skip approval
    default_ttl: float = 7200.0
    max_ttl: float = 28800.0
    runner: list[str] = field(default_factory=list)   # the agent command template, see [runner]

    def runner_for(self, role: str) -> list[str]:
        r = self.roles.get(role)
        return list(r.runner) if r and r.runner else list(self.runner)

    def role_of_kind(self, kind: str) -> str:
        return {"claim": "claim", "renew": "claim", "release": "claim", "done": "claim", "task": "plan",
                "propose": "propose",
                "approve": "approve", "reject": "approve", "executed": "execute"}.get(kind, kind)


DEFAULT = """
# baton policy. Every peer must fold with the same file: keep it in the shared log folder.

# The agent each role runs. {model} and {effort} are filled in; the task prompt goes in a "{prompt}" argument if the
# command has one, else on stdin. BATON_HOME, BATON_ACTOR and BATON_TASK are set in its environment, so the agent can
# call baton itself (propose, done). Any CLI agent works: swap the command for codex, aider, a script... A role can
# override it with its own `runner = [...]`.
[runner]
cmd = ["claude", "-p", "--model", "{model}", "--effort", "{effort}"]

[leases]
default_ttl = "2h"
max_ttl = "8h"

[roles.planner]
under = "human"
model = "claude-opus-5-5"
effort = "high"
max_children = 4
can = ["plan", "claim", "propose"]

[roles.orchestrator]
under = "planner"
model = "claude-sonnet-5-5"
effort = "medium"
max_children = 6
can = ["plan", "claim", "propose"]

[roles.worker]
under = "orchestrator"
model = "claude-haiku-5-5"
effort = "low"
max_children = 0
can = ["claim", "propose"]

[gate]
humans = []
approver_roles = []

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
"""


def parse(text: str) -> Policy:
    raw = tomllib.loads(text)
    p = Policy()
    leases = raw.get("leases", {})
    p.default_ttl = duration(leases.get("default_ttl", "2h"))
    p.max_ttl = duration(leases.get("max_ttl", "8h"))
    for name, r in raw.get("roles", {}).items():
        if name == "human":
            raise ValueError('"human" is reserved for the actors listed in [gate] humans')
        p.roles[name] = Role(name, r.get("under"), r.get("model", ""), r.get("effort", ""),
                             int(r.get("max_children", 0)), frozenset(r.get("can", [])), r.get("runner"))
    for role in p.roles.values():
        if role.under not in (None, "human") and role.under not in p.roles:
            raise ValueError(f"role {role.name} is under unknown role {role.under!r}")
    for action, b in raw.get("budgets", {}).items():
        p.budgets[action] = Budget(rate(b.get("per_actor")), rate(b.get("total")), bool(b.get("needs_lease", False)))
    p.runner = list(raw.get("runner", {}).get("cmd", []))
    gate = raw.get("gate", {})
    p.humans = frozenset(gate.get("humans", []))
    p.approver_roles = frozenset(gate.get("approver_roles", []))
    p.auto = {action: frozenset(roles) for action, roles in gate.get("auto", {}).items()}
    return p


def load(home: Path) -> Policy:
    path = Path(home) / "policy.toml"
    return parse(path.read_text(encoding="utf-8") if path.is_file() else DEFAULT)
