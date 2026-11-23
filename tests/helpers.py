"""Shared test setup: a log with human:ana and machines that offer models (no roles: the network assigns work)."""

import json
import sys
from pathlib import Path

from baton import policy as pol
from baton.store import Store

T0 = 1_000_000.0
POLICY = (pol.DEFAULT.replace('humans = []', 'humans = ["human:ana"]')
          .replace('presence_ttl = "3m"', 'presence_ttl = "1000d"'))

SONNET = ("claude:claude-sonnet-5-5", "standard", "claude")
HAIKU = ("claude:claude-haiku-5-5", "fast", "claude")
OPUS = ("claude:claude-opus-5-5", "strong", "claude")
FABLE = ("claude:claude-fable-5-2", "frontier", "claude")
SOL = ("codex:gpt-6.1-sol", "strong", "codex")


def offer(*models, slots=2, tags=(), ttl=10**9):
    models = models or (SONNET,)
    return {"models": [{"id": i, "tier": t, "family": f} for i, t, f in models], "slots": slots,
            "hardware": {"tags": list(tags)}, "ttl": ttl}


def peer(root: Path, name: str, policy: str = POLICY) -> Store:
    s = Store(root / name)
    s.init()
    (s.home / "policy.toml").write_text(policy, encoding="utf-8")
    return s


def machines(s: Store, t=T0, **offers):
    """Join each name with its offer (all of them can do everything: the default is one standard model)."""
    for i, (name, o) in enumerate(offers.items()):
        s.append("join", name, {"offer": o}, t + i)


def agent(script: str) -> list[str]:
    """A stand-in agent command: a Python one-liner, so tests need no real model."""
    return [sys.executable, "-c", script]


def machine_cfg(ws, cmd, refs, slots=2):
    """Configure the test workspace's machine: every provider runs `cmd`, `refs` are allowed."""
    ws.set_machine(allowed=list(refs), slots=slots, commands={"claude": cmd, "codex": cmd})


def fake_probe(*providers):
    """A probe with the given providers installed and logged in (no real detection)."""
    from baton import machine as mc
    agents = []
    for pid in providers or ("claude", "codex"):
        agents.append({"id": pid, "kind": "subscription", "installed": True, "logged_in": True, "plan": "test",
                       "models": [{"model": m, "tier": t} for m, t in mc.CATALOG[pid]]})
    return {"at": 0, "os": "test", "cpu": {"logical_cores": 8, "physical_cores": 4, "model": "test", "arch": "x",
                                           "utilization_percent": 1.0, "load": None},
            "memory": {"total_gb": 32.0, "available_gb": 16.0}, "gpus": [], "disk": [], "agents": agents}
