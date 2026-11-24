"""Keep an installed baton up to date from its GitHub repository.

The installer puts the code in <prefix>/lib/baton/app and records the commit it came from in `.baton-commit`. Here:

- `check()` asks GitHub for the latest commit of the branch (one small HTTPS request);
- `apply()` downloads that commit's tarball into a side folder, checks that it imports, then swaps it in (the
  previous version is kept as app.old, so a failed swap rolls back).

Only an installed copy updates itself: a development checkout (a git clone) is never touched. Auto-update is on by
default; the workspace setting `auto_update: false` or BATON_NO_UPDATE=1 turns it off. The source is fixed to the
project's repository over HTTPS.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

REPO = "ps1ke/Baton"
BRANCH = "main"
EVERY = 6 * 3600
MAX_TARBALL = 50 * 1024 * 1024


def app_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def installed() -> bool:
    """Running from an installer's copy (…/lib/baton/app), not from a git checkout."""
    d = app_dir()
    return d.name == "app" and d.parent.name == "baton" and not (d / ".git").exists()


def current_commit() -> str | None:
    f = app_dir() / ".baton-commit"
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


def enabled(ws=None) -> bool:
    if os.environ.get("BATON_NO_UPDATE"):
        return False
    if ws is not None and ws.load().get("auto_update") is False:
        return False
    return installed()


def _get(url: str, accept: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "baton-updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("the download is larger than expected")
    return data


def latest(branch: str = BRANCH) -> str:
    """The latest commit of the branch on GitHub."""
    sha = _get(f"https://api.github.com/repos/{REPO}/commits/{branch}", "application/vnd.github.sha", 100).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("GitHub did not answer with a commit")
    return sha


def check(branch: str = BRANCH) -> dict:
    cur, new = current_commit(), latest(branch)
    return {"current": cur, "latest": new, "available": new != cur}


def apply(commit: str) -> str:
    """Install `commit` in place of the running copy. Returns the new version."""
    if not installed():
        raise RuntimeError("this baton is not an installed copy (a development checkout updates with git)")
    data = _get(f"https://codeload.github.com/{REPO}/tar.gz/{commit}", "application/octet-stream", MAX_TARBALL)
    app = app_dir()
    new, old = app.with_name("app.new"), app.with_name("app.old")
    shutil.rmtree(new, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        top = None
        for m in tar.getmembers():
            if top is None:
                top = m.name.split("/")[0]
        tar.extractall(new.parent / ".update", filter="data")
    src = new.parent / ".update" / top
    if not (src / "baton" / "cli.py").exists():
        shutil.rmtree(new.parent / ".update", ignore_errors=True)
        raise ValueError("the download does not look like baton")
    src.rename(new)
    shutil.rmtree(new.parent / ".update", ignore_errors=True)
    (new / ".baton-commit").write_text(commit + "\n")
    r = subprocess.run([sys.executable, "-c", "import baton, baton.cli, baton.ui; print(baton.__version__)"],
                       env={**os.environ, "PYTHONPATH": str(new)}, capture_output=True, text=True, timeout=60, cwd=str(new))
    if r.returncode:
        shutil.rmtree(new, ignore_errors=True)
        raise RuntimeError(f"the new version does not start: {r.stderr.strip()[-300:]}")
    shutil.rmtree(old, ignore_errors=True)
    app.rename(old)
    try:
        new.rename(app)
    except OSError:
        old.rename(app)   # roll back
        raise
    return r.stdout.strip()


def state_file(ws) -> Path:
    return Path(ws.dir) / "update.json"


def remember(ws, **info):
    f = state_file(ws)
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        d = {}
    d.update(info, at=time.time())
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d))


def status(ws) -> dict:
    try:
        d = json.loads(state_file(ws).read_text())
    except (OSError, ValueError):
        d = {}
    return {**d, "enabled": enabled(ws), "installed": installed(), "commit": current_commit()}


def auto(ws, log=print) -> str | None:
    """Check and, if there is a newer commit, install it. The new version's number, or None."""
    if not enabled(ws):
        return None
    try:
        c = check()
        if not c["available"]:
            remember(ws, checked=True, error=None)
            return None
        version = apply(c["latest"])
        remember(ws, updated_to=version, commit=c["latest"], error=None, restart=True)
        log(f"baton updated itself to {version} ({c['latest'][:7]})")
        return version
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as err:
        remember(ws, error=str(err))
        log(f"baton could not update itself: {err}")
        return None
