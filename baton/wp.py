"""Work packages and reports: the standard formats of the work itself (spec/PROTOCOL.md section 4.2,
spec/schemas/package.schema.json and report.schema.json).

A *work package* is the body of a `task` event: one bounded piece of work with what done means.

    {"title": "Parse ISO dates in the importer", "kind": "implement",
     "goal": "import accepts 2026-11-23T20:00:00Z and +01:00 offsets",
     "context": "Background, decisions already taken, links.",
     "paths": ["src/importer"], "topics": ["dates"],
     "acceptance": [{"check": "unit", "run": "python3 -m unittest tests.test_importer"},
                    {"criterion": "no new dependencies"}],
     "deliverables": ["code", "tests"], "constraints": ["keep the public API"],
     "size": "S", "needs": {"tier": "standard", "hardware": ["gpu"]},
     "parent": "<orchestrate package id>", "depends_on": ["<sibling id>"], "inputs": ["https://..."]}

A *report* is what a `done` event carries about how it ended:

    {"changes": [{"path": "src/importer/dates.py", "summary": "new parser"}],
     "verification": {"status": "passed", "checks": [{"name": "unit", "status": "passed", "command": "..."}]},
     "findings": [{"severity": "high", "text": "offset ignored", "path": "src/x.py", "line": 12}],
     "verdict": "passed", "notes": "..."}
"""

from __future__ import annotations

import re

from . import events as ev
from .policy import KINDS, TIERS

SIZES = ("S", "M", "L", "XL")
CHILD_KINDS = ("orchestrate", "implement", "investigate", "test")   # what an orchestrator may post
VERIFY = ("passed", "failed", "not_run")
SEVERITY = ("critical", "high", "medium", "low", "info")
BLOCKING = ("critical", "high")
VERDICTS = ("passed", "changes_requested")
OUTCOMES = ("done", "split", "failed", "escalated")
_ID = re.compile(r"[0-9a-f]{64}")


def _text(errors, where, v, limit, required=False):
    if v is None or v == "":
        if required:
            errors.append(f"{where} is required")
        return
    if not isinstance(v, str) or len(v) > limit:
        errors.append(f"{where} must be text of at most {limit} characters")


def _list(errors, where, v, count, limit=500):
    if v is None:
        return
    if not isinstance(v, list) or len(v) > count or not all(isinstance(x, str) and 0 < len(x) <= limit for x in v):
        errors.append(f"{where} must be a list of at most {count} strings")


def default_kind(body: dict) -> str:
    return "orchestrate" if body.get("size") in ("L", "XL") else "implement"


def check_package(b: dict) -> list[str]:
    """Errors in a work package (a `task` body). Unknown fields are refused, so every peer reads the same package."""
    errors: list[str] = []
    if not isinstance(b, dict):
        return ["a work package must be an object"]
    known = {"title", "kind", "goal", "context", "detail", "paths", "topics", "acceptance", "deliverables",
             "constraints", "size", "needs", "parent", "depends_on", "inputs"}
    extra = set(b) - known
    if extra:
        errors.append(f"unknown work package fields: {', '.join(sorted(extra))}")
    _text(errors, "title", (b.get("title") or "").strip() or None, 200, required=True)
    if b.get("kind") is not None and b["kind"] not in KINDS:
        errors.append(f"kind must be one of {', '.join(KINDS)}")
    _text(errors, "goal", b.get("goal"), 2000)
    _text(errors, "context", b.get("context"), 10000)
    _text(errors, "detail", b.get("detail"), 10000)
    _list(errors, "paths", b.get("paths"), 50)
    _list(errors, "topics", b.get("topics"), 20, 100)
    _list(errors, "deliverables", b.get("deliverables"), 20, 200)
    _list(errors, "constraints", b.get("constraints"), 30)
    _list(errors, "inputs", b.get("inputs"), 20)
    acc = b.get("acceptance")
    if acc is not None:
        if not isinstance(acc, list) or len(acc) > 30:
            errors.append("acceptance must be a list of at most 30 entries")
        else:
            for i, a in enumerate(acc):
                if not isinstance(a, dict) or not (isinstance(a.get("criterion"), str) or isinstance(a.get("run"), str)):
                    errors.append(f"acceptance[{i}] needs a criterion or a run command")
                    continue
                for k in ("criterion", "run", "check"):
                    _text(errors, f"acceptance[{i}].{k}", a.get(k), 1000)
    if b.get("size") is not None and b["size"] not in SIZES:
        errors.append(f"size must be one of {', '.join(SIZES)}")
    needs = b.get("needs")
    if needs is not None:
        if not isinstance(needs, dict) or set(needs) - {"tier", "hardware"}:
            errors.append("needs may only have tier and hardware")
        else:
            if needs.get("tier") is not None and needs["tier"] not in TIERS:
                errors.append(f"needs.tier must be one of {', '.join(TIERS)}")
            _list(errors, "needs.hardware", needs.get("hardware"), 10, 40)
    if b.get("parent") is not None and not (isinstance(b["parent"], str) and _ID.fullmatch(b["parent"])):
        errors.append("parent must be a package id")
    deps = b.get("depends_on")
    if deps is not None and (not isinstance(deps, list) or len(deps) > 20
                             or not all(isinstance(d, str) and _ID.fullmatch(d) for d in deps)):
        errors.append("depends_on must be a list of at most 20 package ids")
    return errors


def check_report(r) -> list[str]:
    errors: list[str] = []
    if not isinstance(r, dict):
        return ["a report must be an object"]
    if len(ev.canonical(r)) > 32000:
        return ["a report must be at most 32000 bytes"]
    extra = set(r) - {"changes", "verification", "findings", "verdict", "notes"}
    if extra:
        errors.append(f"unknown report fields: {', '.join(sorted(extra))}")
    ch = r.get("changes")
    if ch is not None and (not isinstance(ch, list) or len(ch) > 500
                           or not all(isinstance(c, dict) and isinstance(c.get("path"), str) for c in ch)):
        errors.append("changes must be a list of {path, summary}")
    v = r.get("verification")
    if v is not None:
        if not isinstance(v, dict) or v.get("status") not in VERIFY:
            errors.append(f"verification.status must be one of {', '.join(VERIFY)}")
        else:
            checks = v.get("checks", [])
            if not isinstance(checks, list) or len(checks) > 100 or not all(
                    isinstance(c, dict) and isinstance(c.get("name"), str) and c.get("status") in VERIFY for c in checks):
                errors.append("verification.checks must be a list of {name, status, command}")
            elif v["status"] == "passed" and any(c["status"] != "passed" for c in checks):
                errors.append("verification cannot pass while a check did not")
    f = r.get("findings")
    if f is not None and (not isinstance(f, list) or len(f) > 200 or not all(
            isinstance(x, dict) and x.get("severity") in SEVERITY and isinstance(x.get("text"), str) for x in f)):
        errors.append(f"findings must be a list of {{severity ({'/'.join(SEVERITY)}), text, path, line}}")
    if r.get("verdict") is not None and r["verdict"] not in VERDICTS:
        errors.append(f"verdict must be one of {', '.join(VERDICTS)}")
    _text(errors, "notes", r.get("notes"), 4000)
    return errors


def tests_passed(report: dict) -> bool:
    """A test package passes only on verification that actually ran and passed (not_run is not a pass)."""
    return (report.get("verification") or {}).get("status") == "passed"


def review_passed(report: dict) -> bool:
    blocking = [f for f in report.get("findings", []) if f.get("severity") in BLOCKING]
    return report.get("verdict") == "passed" and not blocking


def failure_text(kind: str, summary: str, report: dict) -> str:
    """What an implementer is told when its work came back from a check."""
    lines = [f"{kind} said: {summary or '(no summary)'}"]
    for c in (report.get("verification") or {}).get("checks", []):
        if c.get("status") != "passed":
            lines.append(f"- check {c['name']}: {c['status']}" + (f" ({c['command']})" if c.get("command") else "")
                         + (f": {c['detail']}" if c.get("detail") else ""))
    for x in report.get("findings", []):
        where = f" {x['path']}" + (f":{x['line']}" if x.get("line") else "") if x.get("path") else ""
        lines.append(f"- {x['severity']}{where}: {x['text']}")
    return "\n".join(lines)[:4000]
