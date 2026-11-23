"""How code moves between machines: worktrees in, patches out, one push at the top.

A project starts from a repository. Every machine keeps its own clone of it (read-only for everyone but level 0) in
the workspace, and every package runs in a fresh git worktree at the package's base commit, so agents never share a
working directory and nothing depends on a common server:

- before a run, the worktree gets the patches the package builds on: an implementer's finished dependencies; for a
  test or a review, the change it checks; for an integration, the patches of all the children;
- after the run, what changed inside the package's paths becomes a patch (a blob named by its SHA-256, travelling
  peer to peer with the events), and changes outside the lease are dropped and listed in the report;
- when a root package is done, a steward proposes a `push` of its patch as a branch; once approved, the steward's
  machine applies it to a clean worktree, commits and pushes. Members never hold write access to the repository.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .store import Store


def git(cwd, *args, check=True, input_bytes=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "protocol.ext.allow=never", "-C", str(cwd), *args], capture_output=True,
                          check=check, input=input_bytes, env=_env())


def _env():
    import os
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "baton")
    env.setdefault("GIT_AUTHOR_EMAIL", "baton@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "baton")
    env.setdefault("GIT_COMMITTER_EMAIL", "baton@localhost")
    return env


def checkout(ws, store: Store) -> Path | None:
    """This machine's clone of the project's repository (made on first use), or None for a log without a repo."""
    s = store.state()
    repo = s.project.get("repo") if s.project_id else None
    if not repo:
        return None
    dest = Path(ws.dir) / "checkouts" / s.project_id[:16]
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "-c", "protocol.ext.allow=never", "clone", "-q", "--", repo, str(dest)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode:
            raise RuntimeError(f"cannot clone {repo}: {r.stderr.strip()}")
    else:
        git(dest, "fetch", "-q", "origin", check=False)
    return dest


def base_of(s, task) -> str | None:
    t = task
    while t is not None:
        if t.base:
            return t.base
        t = s.tasks.get(t.of) if t.of else s.tasks.get(t.parent) if t.parent else None
    return None


def patch_of(task) -> str | None:
    p = (task.report or {}).get("patch")
    return p if isinstance(p, str) and len(p) == 64 else None


def inputs(s, task) -> list:
    """The packages whose patches go into this package's worktree, in the order they are applied."""
    def with_deps(t, seen):
        out = []
        for d in t.depends_on:
            if d not in seen and d in s.tasks:
                seen.add(d)
                out += with_deps(s.tasks[d], seen) + [s.tasks[d]]
        return out

    if task.of:                                      # a test or a review: the change it checks
        target = s.tasks[task.of]
        return with_deps(target, set()) + [target]
    if task.kind == "orchestrate" and task.phase == "waiting":    # integrating: every child that produced a patch
        return [s.tasks[c] for c in task.subtasks if s.tasks[c].outcome == "done"]
    return with_deps(task, set())


def prepare(store: Store, s, task, repo: Path, where: Path) -> tuple[Path, str]:
    """A fresh worktree for the package with its input patches applied: (path, base commit)."""
    base = base_of(s, task)
    if not base:
        r = git(repo, "rev-parse", "HEAD")
        base = r.stdout.decode().strip()
    if where.exists():
        git(repo, "worktree", "remove", "--force", str(where), check=False)
        shutil.rmtree(where, ignore_errors=True)
    where.parent.mkdir(parents=True, exist_ok=True)
    r = git(repo, "worktree", "add", "-q", "--detach", str(where), base, check=False)
    if r.returncode:
        raise RuntimeError(f"cannot check out {base[:12]}: {r.stderr.decode().strip()}")
    for t in inputs(s, task):
        h = patch_of(t)
        if not h:
            continue
        data = store.blob(h)
        if data is None:
            raise RuntimeError(f"the patch of {t.id[:12]} has not arrived yet ({h[:12]}); sync and try again")
        if data.strip():
            r = git(where, "apply", "--index", "--whitespace=nowarn", "-", check=False, input_bytes=data)
            if r.returncode:
                raise RuntimeError(f"the patch of {t.id[:12]} does not apply: {r.stderr.decode().strip()[:300]}")
    if inputs(s, task):
        git(where, "commit", "-q", "--allow-empty", "-m", "baton: inputs")
    return where, base


def capture(store: Store, task, where: Path, base: str) -> dict:
    """What the agent changed: the patch of everything inside the package's paths (from the base, so it includes the
    inputs an integration combined), and the paths outside the lease it touched, which are dropped."""
    git(where, "add", "-A")
    names = git(where, "diff", "--cached", "--name-only", base).stdout.decode().split()
    def inside(n):
        return not task.paths or any(p == "" or n == p or n.startswith(p.rstrip("/") + "/") for p in task.paths)
    keep = [n for n in names if inside(n)]
    outside = [n for n in names if not inside(n)]
    data = git(where, "diff", "--cached", "--binary", base, "--", *keep).stdout if keep else b""
    report = {"patch": store.put_blob(data), "base": base}
    if outside:
        report["outside"] = outside[:200]
    if keep:
        report.setdefault("changes", [{"path": n} for n in keep[:500]])
    return report


def cleanup(repo: Path, where: Path):
    git(repo, "worktree", "remove", "--force", str(where), check=False)
    shutil.rmtree(where, ignore_errors=True)


def push(store: Store, repo: Path, payload: dict) -> dict:
    """Execute an approved push: apply the patch on its base in a clean worktree, commit, push the branch."""
    branch, base, h = payload["branch"], payload["base"], payload["patch"]
    if not branch.startswith("baton/"):
        raise ValueError("baton pushes only branches named baton/...")
    data = store.blob(h)
    if data is None:
        raise RuntimeError(f"patch {h[:12]} is not on this machine yet")
    where = repo.parent / ".push" / hashlib.sha256(branch.encode()).hexdigest()[:12]
    cleanup(repo, where)
    where.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-B", branch, str(where), base)
    try:
        if data.strip():
            git(where, "apply", "--index", "--whitespace=nowarn", "-", input_bytes=data)
        git(where, "commit", "-q", "--allow-empty", "-m", payload.get("title") or "baton", "-m",
            f"baton package {payload.get('package', '')}\npatch sha256 {h}")
        r = git(where, "push", "-q", "origin", f"{branch}:{branch}", check=False)
        if r.returncode:
            raise RuntimeError(f"push failed: {r.stderr.decode().strip()}")
        commit = git(where, "rev-parse", "HEAD").stdout.decode().strip()
    finally:
        git(repo, "worktree", "remove", "--force", str(where), check=False)
    return {"branch": branch, "commit": commit}
