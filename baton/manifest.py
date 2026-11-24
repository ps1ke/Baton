"""The baton protocol's public documents: the project manifest and the catalog (spec/PROTOCOL.md, sections 6 and 7).

Both are plain JSON. Validation is by hand (standard library only) and mirrors spec/schemas/*.schema.json; everything
read from a catalog is untrusted data, so every field is checked for type and size before anything uses it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

PROTOCOL = "3"
CATALOG = "1"
JOIN_MODES = ("open", "apply", "closed")
RELS = ("depends-on", "dependency-of", "related", "fork-of", "upstream", "downstream", "successor", "predecessor")
# Fields a `project` event may set; the manifest adds id, humans, updated and activity.
FIELDS = ("name", "summary", "description", "homepage", "repo", "log", "inbox", "tags", "needs", "join", "links",
          "discoverable")

_ID = re.compile(r"[0-9a-f]{64}")
_TAG = re.compile(r"[a-z0-9][a-z0-9+.-]{0,39}")
_GIT = re.compile(r"(https://|ssh://|git@[\w.-]+:|file://)\S+")
_URL = re.compile(r"https?://\S+")


def is_id(v) -> bool:
    return isinstance(v, str) and bool(_ID.fullmatch(v))


def git_url_ok(url) -> bool:
    """Only transports that cannot run commands (no ext::, no bare paths that look like options)."""
    return isinstance(url, str) and len(url) <= 500 and bool(_GIT.fullmatch(url)) and "::" not in url


def _str(errors, where, v, limit, required=False):
    if v is None:
        if required:
            errors.append(f"{where} is required")
        return
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        errors.append(f"{where} must be a non-empty string of at most {limit} characters")


def _url(errors, where, v):
    if v is not None and (not isinstance(v, str) or len(v) > 500 or not _URL.fullmatch(v)):
        errors.append(f"{where} must be an http(s) URL")


def _strings(errors, where, v, item_limit, count, pattern=None):
    if v is None:
        return
    if not isinstance(v, list) or len(v) > count or not all(isinstance(x, str) and 0 < len(x) <= item_limit for x in v):
        errors.append(f"{where} must be a list of at most {count} strings of at most {item_limit} characters")
    elif pattern and not all(pattern.fullmatch(x) for x in v):
        errors.append(f"{where} items must be lower-case words (a-z, 0-9, + . -)")


def check_fields(d: dict) -> list[str]:
    """Errors in the project fields of `d` (all optional here; a full manifest also needs id and name)."""
    errors: list[str] = []
    if not isinstance(d, dict):
        return ["must be an object"]
    # Unknown fields are allowed here (a newer peer may add some, spec section 11); a `project` event refuses them.
    _str(errors, "name", d.get("name"), 120)
    _str(errors, "summary", d.get("summary"), 280)
    _str(errors, "description", d.get("description"), 10000)
    _url(errors, "homepage", d.get("homepage"))
    repo = d.get("repo")
    if repo is not None and not (git_url_ok(repo) or (isinstance(repo, str) and _URL.fullmatch(repo) and len(repo) <= 500)):
        errors.append("repo must be the repository's URL (https://, ssh://, git@host: or file://)")
    log = d.get("log")
    if log is not None:
        if not isinstance(log, dict) or not git_url_ok(log.get("git")):
            errors.append("log.git must be an https://, ssh://, git@host: or file:// git URL")
        elif "branch" in log and (not isinstance(log["branch"], str) or not re.fullmatch(r"[\w./-]{1,100}", log["branch"])):
            errors.append("log.branch must be a branch name")
    inbox = d.get("inbox")
    if inbox is not None:
        if not isinstance(inbox, dict) or inbox.get("type") not in ("git", "url") or \
                not (git_url_ok(inbox.get("url")) if inbox.get("type") == "git" else _URL.fullmatch(str(inbox.get("url", "")))):
            errors.append('inbox must be {"type": "git"|"url", "url": ...}')
    _strings(errors, "tags", d.get("tags"), 40, 20, _TAG)
    needs = d.get("needs")
    if needs is not None:
        if not isinstance(needs, list) or len(needs) > 50:
            errors.append("needs must be a list of at most 50 entries")
        else:
            for i, n in enumerate(needs):
                if not isinstance(n, dict):
                    errors.append(f"needs[{i}] must be an object")
                    continue
                if n.get("kind") is not None and n["kind"] not in ("orchestrate", "implement", "test", "review", "investigate"):
                    errors.append(f"needs[{i}].kind must be a kind of work")
                if n.get("tier") is not None and n["tier"] not in ("fast", "standard", "strong", "frontier"):
                    errors.append(f"needs[{i}].tier must be fast, standard, strong or frontier")
                _strings(errors, f"needs[{i}].skills", n.get("skills"), 40, 20)
                _strings(errors, f"needs[{i}].hardware", n.get("hardware"), 80, 10)
                if "count" in n and (not isinstance(n["count"], int) or isinstance(n["count"], bool) or not 0 <= n["count"] <= 10000):
                    errors.append(f"needs[{i}].count must be an integer 0..10000")
                if "note" in n:
                    _str(errors, f"needs[{i}].note", n["note"], 500)
    if d.get("discoverable") is not None and not isinstance(d.get("discoverable"), bool):
        errors.append("discoverable must be true or false")
    if d.get("join") is not None and d.get("join") not in JOIN_MODES:
        errors.append(f"join must be one of {', '.join(JOIN_MODES)}")
    links = d.get("links")
    if links is not None:
        if not isinstance(links, list) or len(links) > 100:
            errors.append("links must be a list of at most 100 entries")
        else:
            for i, l in enumerate(links):
                if not isinstance(l, dict) or l.get("rel") not in RELS:
                    errors.append(f"links[{i}].rel must be one of {', '.join(RELS)}")
                    continue
                if "project" in l and not is_id(l["project"]):
                    errors.append(f"links[{i}].project must be a project id (64 hex characters)")
                if "project" not in l and "manifest" not in l:
                    errors.append(f"links[{i}] needs a project id or a manifest URL")
                _url(errors, f"links[{i}].manifest", l.get("manifest"))
                if "name" in l:
                    _str(errors, f"links[{i}].name", l["name"], 120)
    return errors


def check_manifest(d) -> list[str]:
    if not isinstance(d, dict):
        return ["a manifest must be an object"]
    errors = []
    if d.get("baton") != PROTOCOL:
        errors.append(f'baton must be "{PROTOCOL}"')
    if not is_id(d.get("id")):
        errors.append("id must be the project's genesis event id (64 hex characters)")
    if d.get("name") is None:
        errors.append("name is required")
    _strings(errors, "humans", d.get("humans"), 120, 100)
    return errors + check_fields(d)


def check_catalog(d) -> list[str]:
    if not isinstance(d, dict):
        return ["a catalog must be an object"]
    errors = []
    if d.get("baton_catalog") != CATALOG:
        errors.append(f'baton_catalog must be "{CATALOG}"')
    _str(errors, "name", d.get("name"), 120, required=True)
    if not isinstance(d.get("projects", []), list) or len(d.get("projects", [])) > 5000:
        errors.append("projects must be a list of at most 5000 manifests")
    cats = d.get("catalogs", [])
    if not isinstance(cats, list) or len(cats) > 200 or not all(
            isinstance(c, dict) and isinstance(c.get("url"), str) and len(c["url"]) <= 500 for c in cats):
        errors.append("catalogs must be a list of at most 200 {name, url} entries")
    return errors


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_catalog(name: str, description: str = "") -> dict:
    c = {"baton_catalog": CATALOG, "name": name, "projects": [], "catalogs": []}
    if description:
        c["description"] = description
    return c
