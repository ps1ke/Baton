"""The forge adapter (GitHub through the `gh` CLI). Read-only checks run freely; writes run only for an approved
proposal, from `baton execute`, and the result is recorded in the log so no peer runs it twice."""

from __future__ import annotations

import json
import subprocess

from . import paths

ACTIONS = {"comment", "issue_comment"}


def open_pulls(repo: str, limit: int = 500, run=subprocess.run) -> list[dict]:
    r = run(["gh", "pr", "list", "-R", repo, "--state", "open", "--limit", str(limit),
             "--json", "number,title,files,author"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "gh pr list failed")
    return json.loads(r.stdout)


def touching(pulls: list[dict], scope) -> list[dict]:
    """Open pull requests that change a file inside the scope: the duplicate check the case study never had."""
    out = []
    for pr in pulls:
        hit = [f["path"] for f in pr.get("files") or [] if any(paths.inside(f["path"], s) for s in scope)]
        if hit:
            out.append({"number": pr["number"], "title": pr["title"], "author": (pr.get("author") or {}).get("login"),
                        "files": hit})
    return out


def execute(action: str, target: str, payload: dict, run=subprocess.run) -> dict:
    if action not in ACTIONS:
        raise NotImplementedError(f"no executor for {action!r} yet (have: {', '.join(sorted(ACTIONS))})")
    repo, body = payload.get("repo"), payload.get("body")
    if not repo or not body or not target.lstrip("#").isdigit():
        raise ValueError("a comment needs payload.repo, payload.body and a numeric target")
    cmd = ["gh", "pr" if action == "comment" else "issue", "comment", target.lstrip("#"), "-R", repo, "--body-file", "-"]
    r = run(cmd, input=body, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "gh comment failed")
    return {"url": r.stdout.strip()}
