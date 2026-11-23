"""What this machine is and what it offers to a project.

`probe()` finds out, without asking the user:

- the hardware: CPU (model, cores, utilization, load), memory, GPUs (nvidia-smi, rocm-smi, lspci), disk space;
- the agents it can run: Claude Code (installed, logged in, the plan of the login), Codex (installed, logged in, plan,
  its model list), and local model servers on this machine (Ollama, and OpenAI-compatible servers such as LM Studio,
  llama.cpp and vLLM), each with the models they serve.

Only this machine is asked: local files, local commands and servers on 127.0.0.1. Credentials files are read for the
plan name only; no token ever leaves the function.

The user then decides, in the workspace's `machine` settings, which of those models baton may use, at which tier,
and how many work packages may run at once (`slots`). The *offer* built from that is what the machine announces to
a project; the project's network, not the user, decides which packages the machine gets (see network.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TIERS = ("fast", "standard", "strong", "frontier")

# Known models per provider and the tier baton gives them by default. The user can change any tier.
CATALOG = {
    "claude": [("claude-haiku-5-5", "fast"), ("claude-sonnet-5-5", "standard"), ("claude-opus-5-5", "strong"),
               ("claude-fable-5-2", "frontier")],
    "codex": [("gpt-6.1-luna", "fast"), ("gpt-6.1-terra", "standard"), ("gpt-6.1-sol", "strong"),
              ("gpt-6.1-astra", "frontier")],
}
EFFORT = {   # per provider, the effort each tier runs at
    "claude": {"fast": "low", "standard": "medium", "strong": "high", "frontier": "high"},
    "codex": {"fast": "low", "standard": "medium", "strong": "high", "frontier": "high"},
}
LOCAL_SERVERS = [   # (provider id, base URL, kind)
    ("ollama", "http://127.0.0.1:11434", "ollama"),
    ("lmstudio", "http://127.0.0.1:1234", "openai"),
    ("llamacpp", "http://127.0.0.1:8080", "openai"),
    ("vllm", "http://127.0.0.1:8000", "openai"),
]
PY = sys.executable


def _run(cmd, timeout=5.0, stdin=None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as err:
        return -1, str(err)


# -- hardware ------------------------------------------------------------------------------------------------------
def _cpu_times():
    try:
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return idle, sum(parts)
    except (OSError, ValueError, IndexError):
        return None


def cpu(sample: float = 0.25) -> dict:
    out = {"logical_cores": os.cpu_count() or 1, "physical_cores": None, "model": platform.processor() or "",
           "utilization_percent": None, "load": None, "arch": platform.machine()}
    try:
        text = Path("/proc/cpuinfo").read_text()
        m = re.search(r"model name\s*:\s*(.+)", text)
        if m:
            out["model"] = m.group(1).strip()
        pairs = set(re.findall(r"physical id\s*:\s*(\d+).*?core id\s*:\s*(\d+)", text, re.S))
        if pairs:
            out["physical_cores"] = len(pairs)
    except OSError:
        if sys.platform == "darwin":
            out["model"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"])[1].strip()
            code, n = _run(["sysctl", "-n", "hw.physicalcpu"])
            out["physical_cores"] = int(n) if code == 0 and n.strip().isdigit() else None
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    a = _cpu_times()
    if a:
        time.sleep(sample)
        b = _cpu_times()
        if b and b[1] > a[1]:
            out["utilization_percent"] = round(100.0 * (1 - (b[0] - a[0]) / (b[1] - a[1])), 1)
    elif out["load"]:
        out["utilization_percent"] = round(min(100.0, 100.0 * out["load"][0] / out["logical_cores"]), 1)
    return out


def memory() -> dict:
    out = {"total_gb": None, "available_gb": None}
    try:
        info = dict(re.findall(r"(\w+):\s+(\d+) kB", Path("/proc/meminfo").read_text()))
        out["total_gb"] = round(int(info["MemTotal"]) / 1048576, 1)
        out["available_gb"] = round(int(info.get("MemAvailable", info.get("MemFree", 0))) / 1048576, 1)
    except (OSError, KeyError, ValueError):
        if sys.platform == "darwin":
            code, n = _run(["sysctl", "-n", "hw.memsize"])
            if code == 0 and n.strip().isdigit():
                out["total_gb"] = round(int(n) / 2**30, 1)
    return out


def gpus() -> list[dict]:
    found = []
    if shutil.which("nvidia-smi"):
        code, text = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                           "--format=csv,noheader,nounits"])
        if code == 0:
            for line in text.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    num = lambda v: float(v) if re.fullmatch(r"[\d.]+", v) else None
                    total, used, util = num(parts[1]), num(parts[2]), num(parts[3])
                    found.append({"vendor": "nvidia", "name": parts[0],
                                  "vram_gb": round(total / 1024, 1) if total else None,
                                  "vram_used_gb": round(used / 1024, 1) if used is not None else None,
                                  "utilization_percent": util})
    if shutil.which("rocm-smi"):
        code, text = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
        if code == 0:
            try:
                for card, d in json.loads(text[text.index("{"):]).items():
                    total = next((int(v) for k, v in d.items() if "Total Memory" in k and str(v).isdigit()), None)
                    used = next((int(v) for k, v in d.items() if "Total Used" in k and str(v).isdigit()), None)
                    found.append({"vendor": "amd", "name": d.get("Card series") or d.get("Card SKU") or card,
                                  "vram_gb": round(total / 2**30, 1) if total else None,
                                  "vram_used_gb": round(used / 2**30, 1) if used is not None else None,
                                  "utilization_percent": None})
            except (ValueError, AttributeError):
                pass
    if not found and shutil.which("lspci"):
        code, text = _run(["lspci"])
        for line in text.splitlines() if code == 0 else []:
            if re.search(r"VGA|3D controller|Display controller", line):
                name = line.split(": ", 1)[-1]
                vendor = "nvidia" if "NVIDIA" in name else "amd" if re.search(r"AMD|ATI", name) else \
                    "intel" if "Intel" in name else "other"
                found.append({"vendor": vendor, "name": name, "vram_gb": None, "vram_used_gb": None,
                              "utilization_percent": None})
    if not found and sys.platform == "darwin" and platform.machine() == "arm64":
        found.append({"vendor": "apple", "name": "Apple silicon (unified memory)", "vram_gb": None,
                      "vram_used_gb": None, "utilization_percent": None})
    return found


def disk(paths) -> list[dict]:
    out, seen = [], set()
    for p in paths:
        p = Path(p).expanduser()
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        key = (u.total, u.free)
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": str(p.resolve()), "total_gb": round(u.total / 2**30, 1), "free_gb": round(u.free / 2**30, 1)})
    return out


# -- agents --------------------------------------------------------------------------------------------------------
def _jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def claude(limits: bool = False) -> dict:
    path = shutil.which(os.environ.get("BATON_CLAUDE_BIN", "claude"))
    out = {"id": "claude", "kind": "subscription", "installed": bool(path), "version": None, "logged_in": False,
           "plan": None, "auth": None, "models": [], "limits": None}
    if not path:
        return out
    code, text = _run([path, "--version"])
    out["version"] = text.strip().splitlines()[0] if code == 0 and text.strip() else None
    cred = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json"
    try:
        oauth = json.loads(cred.read_text()).get("claudeAiOauth") or {}
        out["logged_in"] = bool(oauth.get("accessToken")) and (oauth.get("refreshTokenExpiresAt") or 0) / 1000 > time.time() \
            if oauth.get("refreshTokenExpiresAt") else bool(oauth.get("accessToken"))
        out["plan"] = oauth.get("subscriptionType")
        out["auth"] = "claude.ai"
    except (OSError, ValueError, AttributeError):
        if os.environ.get("ANTHROPIC_API_KEY"):
            out.update(logged_in=True, auth="api key", kind="api")
        elif sys.platform == "darwin":
            out.update(logged_in=None, auth="keychain (not checked)")
    out["models"] = [{"model": m, "tier": t} for m, t in CATALOG["claude"]]
    if limits and out["logged_in"]:
        out["limits"] = claude_limits(path)
    return out


def claude_limits(path: str) -> dict:
    code, text = _run([path, "-p", "--no-session-persistence", "--output-format", "json", "/usage"], timeout=25)
    try:
        text = json.loads(text).get("result", "") if code == 0 else text
    except ValueError:
        pass
    week = re.search(r"Current week[^:]*:\s*([\d.]+)%\s*used", text, re.I)
    session = re.search(r"Current session:\s*([\d.]+)%\s*used", text, re.I)
    return {"week_percent": float(week.group(1)) if week else None,
            "session_percent": float(session.group(1)) if session else None,
            "error": None if (week or session) else text.strip()[-200:] or "no usage reported"}


def _codex_app(path: str, requests: list[tuple[str, dict | None]], timeout: float = 10.0) -> list[dict]:
    """Ask `codex app-server` (JSON lines on stdin/stdout) a few read-only questions."""
    lines = [{"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "baton", "title": "baton", "version": "1"}}},
             {"method": "initialized", "params": {}}]
    for i, (method, params) in enumerate(requests, 1):
        lines.append({"method": method, "id": i, **({"params": params} if params is not None else {})})
    code, text = _run([path, "app-server"], timeout=timeout, stdin="\n".join(json.dumps(l) for l in lines) + "\n")
    answers = {}
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("id"), int) and d["id"] > 0:
            answers[d["id"]] = d.get("result") or {"error": d.get("error")}
    return [answers.get(i, {}) for i in range(1, len(requests) + 1)]


def codex(limits: bool = False) -> dict:
    path = shutil.which(os.environ.get("BATON_CODEX_BIN", "codex"))
    out = {"id": "codex", "kind": "subscription", "installed": bool(path), "version": None, "logged_in": False,
           "plan": None, "auth": None, "models": [], "limits": None}
    if not path:
        return out
    code, text = _run([path, "--version"])
    out["version"] = text.strip().splitlines()[0] if code == 0 and text.strip() else None
    try:
        auth = json.loads((Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json").read_text())
        tokens = auth.get("tokens") or {}
        if tokens.get("id_token") or tokens.get("access_token"):
            claims = _jwt_claims(tokens.get("id_token", ""))
            info = claims.get("https://api.openai.com/auth") or {}
            out.update(logged_in=True, auth="chatgpt", plan=info.get("chatgpt_plan_type"))
        elif auth.get("OPENAI_API_KEY"):
            out.update(logged_in=True, auth="api key", kind="api")
    except (OSError, ValueError, AttributeError):
        if os.environ.get("OPENAI_API_KEY"):
            out.update(logged_in=True, auth="api key", kind="api")
    reqs = [("model/list", {"limit": 100, "includeHidden": False})] + ([("account/rateLimits/read", None)] if limits else [])
    answers = _codex_app(path, reqs) if out["logged_in"] else [{}] * len(reqs)
    listed = [i.get("model") or i.get("id") for i in (answers[0].get("data") or []) if isinstance(i, dict)]
    known = dict(CATALOG["codex"])
    if listed:
        out["models"] = [{"model": m, "tier": known.get(m) or _guess_codex_tier(m)} for m in listed if m]
    else:
        out["models"] = [{"model": m, "tier": t, "unverified": True} for m, t in CATALOG["codex"]]
    if limits and len(answers) > 1:
        rl = answers[1].get("rateLimits") or {}
        out["limits"] = {"primary_percent": (rl.get("primary") or {}).get("usedPercent"),
                         "secondary_percent": (rl.get("secondary") or {}).get("usedPercent"),
                         "error": None if rl else "no rate limits reported"}
    return out


def _guess_codex_tier(model: str) -> str:
    m = model.lower()
    return "frontier" if "astra" in m else "strong" if "sol" in m else "fast" if re.search(r"luna|mini|nano", m) else "standard"


def guess_local_tier(model: str) -> str:
    """Local models: by parameter count when the name says it (≥70b strong, ≥27b standard, else fast)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model.lower().replace(":", " ").replace("-", " "))
    size = float(m.group(1)) if m else 0
    return "strong" if size >= 70 else "standard" if size >= 27 else "fast"


def _get_json(url: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "baton"}), timeout=timeout) as r:
            return json.loads(r.read(1_000_000))
    except (OSError, ValueError):
        return None


def local_servers(timeout: float = 0.4) -> list[dict]:
    servers = list(LOCAL_SERVERS)
    host = os.environ.get("OLLAMA_HOST", "")
    if host and re.fullmatch(r"(https?://)?(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", host):
        servers[0] = ("ollama", host if host.startswith("http") else "http://" + host, "ollama")
    out = []
    for pid, base, kind in servers:
        d = _get_json(base + ("/api/tags" if kind == "ollama" else "/v1/models"), timeout)
        if d is None:
            continue
        names = [m.get("name") or m.get("model") for m in d.get("models", [])] if kind == "ollama" else \
            [m.get("id") for m in d.get("data", [])]
        out.append({"id": pid, "kind": "local", "api": kind, "url": base, "installed": True, "logged_in": True,
                    "plan": "local", "models": [{"model": n, "tier": guess_local_tier(n)} for n in names if n]})
    return out


def harness_for_local(server: dict) -> list[str] | None:
    """A command that lets a local model act as an agent (edit files, run commands), if one is installed."""
    if shutil.which("codex") and server["api"] == "ollama":
        return ["codex", "exec", "--oss", "--model", "{model}", "--sandbox", "workspace-write", "-"]
    if shutil.which("aider"):
        model = "ollama_chat/{model}" if server["api"] == "ollama" else "openai/{model}"
        extra = [] if server["api"] == "ollama" else ["--openai-api-base", server["url"] + "/v1"]
        return ["aider", "--model", model, *extra, "--yes-always", "--no-auto-commits", "--message", "{prompt}"]
    return None


def default_command(provider: str, server: dict | None = None) -> list[str] | None:
    if provider == "claude":
        tools = ",".join(["Read", "Edit", "Write", "Glob", "Grep", "Bash(git diff:*)", "Bash(git status:*)",
                          "Bash(git log:*)", "Bash(python3:*)", f"Bash({PY}:*)"])
        return ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--permission-mode", "acceptEdits",
                "--allowedTools", tools]
    if provider == "codex":
        return ["codex", "exec", "--model", "{model}", "-c", 'model_reasoning_effort="{effort}"',
                "--sandbox", "workspace-write", "-"]
    return harness_for_local(server) if server else None


def probe(paths=(), limits: bool = False) -> dict:
    """Everything this machine can tell about itself. Slow parts: ~0.3 s of CPU sampling; Codex's model list."""
    agents = [claude(limits), codex(limits)] + local_servers()
    return {"at": time.time(), "os": f"{platform.system()} {platform.release()}", "cpu": cpu(), "memory": memory(),
            "gpus": gpus(), "disk": disk(list(paths) + [Path.home()]), "agents": agents}


# -- the user's choices and the offer --------------------------------------------------------------------------------
DEFAULTS = {"slots": None, "allowed": [], "tiers": {}, "commands": {}, "share_hardware": True, "name": None}


def default_name() -> str:
    """A stable, anonymous member name for this machine (the host name is not put in shared logs)."""
    return "m-" + hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]


def suggested_slots(p: dict) -> int:
    """Agents mostly wait on the model: memory bounds them more than cores (about 2 GB each for the CLI and tools)."""
    cores = p["cpu"]["physical_cores"] or p["cpu"]["logical_cores"]
    ram = p["memory"]["available_gb"] or p["memory"]["total_gb"] or 4
    return max(1, min(8, cores // 2, int(ram // 2)))


def models(p: dict, cfg: dict) -> list[dict]:
    """Every model this machine could run, with its tier, whether it is usable and whether the user allowed it."""
    out = []
    for a in p["agents"]:
        server = a if a.get("kind") == "local" else None
        cmd = cfg.get("commands", {}).get(a["id"]) or default_command(a["id"], server)
        for m in a.get("models", []):
            ref = f"{a['id']}:{m['model']}"
            why = None
            if not a.get("installed"):
                why = f"{a['id']} is not installed"
            elif a.get("logged_in") is False:
                why = f"{a['id']} is not logged in"
            elif not cmd:
                why = "no agent harness for a local model: install codex or aider, or set a command"
            out.append({"ref": ref, "provider": a["id"], "model": m["model"],
                        "family": "local" if server else a["id"], "tier": cfg.get("tiers", {}).get(ref, m["tier"]),
                        "plan": a.get("plan"), "usable": why is None, "why": why, "unverified": m.get("unverified", False),
                        "allowed": ref in cfg.get("allowed", [])})
    return out


def hardware_tags(p: dict) -> list[str]:
    tags = [f"cores:{p['cpu']['physical_cores'] or p['cpu']['logical_cores']}"]
    if p["memory"]["total_gb"]:
        tags.append(f"ram:{int(p['memory']['total_gb'])}g")
    for g in p["gpus"]:
        tags += ["gpu", f"gpu:{g['vendor']}"] + ([f"vram:{int(g['vram_gb'])}g"] if g.get("vram_gb") else [])
    free = max((d["free_gb"] for d in p["disk"]), default=0)
    if free:
        tags.append(f"disk:{int(free)}g")
    return sorted(set(tags))


def offer(p: dict, cfg: dict, ttl: float) -> dict:
    """What this machine announces to a project: the allowed, usable models, its slots, its hardware."""
    ms = [m for m in models(p, cfg) if m["allowed"] and m["usable"]]
    o = {"models": [{"id": m["ref"], "tier": m["tier"], "family": m["family"]} for m in ms],
         "slots": int(cfg.get("slots") or suggested_slots(p)), "ttl": float(ttl)}
    if cfg.get("share_hardware", True):
        o["hardware"] = {"tags": hardware_tags(p), "cpu_cores": p["cpu"]["logical_cores"],
                         "cpu_utilization": p["cpu"]["utilization_percent"], "ram_gb": p["memory"]["total_gb"],
                         "gpus": [{"name": g["name"], "vram_gb": g["vram_gb"]} for g in p["gpus"]][:8],
                         "disk_free_gb": max((d["free_gb"] for d in p["disk"]), default=None)}
    return o


def command_for(p: dict, cfg: dict, ref: str) -> tuple[list[str], str]:
    """(command template, effort) for running model `ref` ("provider:model") here."""
    provider, _, model = ref.partition(":")
    entry = next((m for m in models(p, cfg) if m["ref"] == ref), None)
    if entry is None or not entry["usable"]:
        raise ValueError(f"{ref} cannot run here" + (f": {entry['why']}" if entry else ""))
    if not entry["allowed"]:
        raise PermissionError(f"{ref} is not allowed on this machine (`baton machine allow {ref}`)")
    server = next((a for a in p["agents"] if a["id"] == provider and a.get("kind") == "local"), None)
    cmd = cfg.get("commands", {}).get(provider) or default_command(provider, server)
    effort = EFFORT.get(provider, {}).get(entry["tier"], "medium")
    return cmd, effort
