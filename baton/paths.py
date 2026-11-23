"""Path scopes. A lease covers repository paths as prefixes: "tools" covers "tools/x.py"; "" or "." is the whole tree.

The case study's hot spots were files (one build file touched by 124 open pull requests), so overlap is decided on paths,
not on the free-text title of a task.
"""

from __future__ import annotations

import posixpath


def norm(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = posixpath.normpath(p) if p else "."
    if p.startswith("../") or p == ".." or p.startswith("/"):
        raise ValueError(f"path must stay inside the repository: {path!r}")
    return "" if p == "." else p.rstrip("/")


def inside(path: str, scope: str) -> bool:
    """`path` is `scope` or below it."""
    path, scope = norm(path), norm(scope)
    return scope == "" or path == scope or path.startswith(scope + "/")


def overlaps(a: str, b: str) -> bool:
    return inside(a, b) or inside(b, a)


def any_overlap(left, right) -> bool:
    return any(overlaps(a, b) for a in left for b in right)
