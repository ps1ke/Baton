# The baton protocol, version 1

Status: draft. This document defines the data formats and rules that let any number of agents and humans, on any
number of machines, find projects, join them and work on them together without a central server. `baton` (this
repository) is one implementation; another that follows this document interoperates with it.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Layers

| Layer | Document | Who agrees on it | Section |
|---|---|---|---|
| Log | event files, one per event | every peer of a project, by folding the same events | 2–5 |
| Project | the manifest, computed from the log | every peer of the project | 6 |
| Directory | catalogs: lists of manifests that link to other catalogs | nobody needs to: anyone publishes one | 7 |

A *peer* is any machine with a copy of a project's log. An *actor* is a named participant: a human (`human:<name>`)
or an agent. A *project* is identified by the id of its genesis event, so it keeps its identity across renames, forks
of its code and moves of its log.

## 2. Encoding

- Documents are UTF-8 JSON. Numbers in events are integers or finite IEEE-754 doubles.
- **Canonical form** (for hashing): keys sorted by code point, no insignificant whitespace (separators `,` and `:`),
  non-ASCII characters written as themselves (not `\u` escapes). This is Python's
  `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
- Ids are lower-case hex SHA-256 digests, 64 characters.
- Durations in policies are strings `<number><s|m|h|d>`; rates are `<count>/<duration>`.

## 3. Events

An event is a JSON object (schema: [`schemas/event.schema.json`](schemas/event.schema.json)):

```json
{
 "kind": "claim",
 "actor": "w1",
 "lamport": 42,
 "ts": 1795000000.5,
 "parents": ["<id>", "<id>"],
 "body": {"task_id": "<id>"},
 "id": "<sha256 of the canonical form of the six fields above>"
}
```

- `id` MUST equal the SHA-256 of the canonical form of the object without `id`. A peer MUST reject an event whose id
  does not match.
- `parents` are the ids of the events the author had seen and that no other seen event names as a parent (its
  *heads*), sorted. `lamport` MUST be greater than every parent's `lamport`.
- `ts` is the author's clock in seconds since the Unix epoch. It is advisory (section 5).
- An event is stored as a file named `<id>.json` in the log's `events/` folder. Files are never changed or removed, so
  two copies of a log merge by taking the union of their files.
- A peer MUST hold back (not fold) an event until all its parents are present, and MUST NOT fold an event whose
  `lamport` is not above its parents'.

## 4. Event kinds and folding

Every peer computes the same state by folding the admitted events in **ascending `(lamport, id)` order** with these
rules. An event that breaks a rule is kept in the log but has no effect ("ignored", with a reason). A claim that
overlaps is "denied". *Effective time* is defined in section 5.

Permissions come from the project's policy (`policy.toml`): `roles.<name>.can` lists `plan`, `claim`, `propose` and
`admit`; `gate.humans` lists the human actors; `gate.approver_roles` lists roles that may also approve, execute and
govern. Humans may do everything. Only `join` and `apply` may come from an actor that is not yet a member.

| kind | body | who | effect |
|---|---|---|---|
| `project` | any of the manifest fields (section 6); `null` removes a field | humans, approver roles | the first one is the **genesis** (MUST have `name`); its id is the project id. Later ones update fields. |
| `join` | `role`, `parent` | anyone, only while `join` is `open` | adds the actor to the tree (placement rules below) |
| `apply` | `role`, optional `parent`, `offer` (object ≤ 4000 bytes canonical), `note` (≤ 2000 chars) | a non-member, unless `join` is `closed` | records a pending application; one pending per actor |
| `admit` | `applicant`, optional `role`, `parent` | humans, approver roles, or the would-be parent (needs `admit` in `can`) | places the applicant in the tree |
| `decline` | `applicant`, optional `reason` | as `admit` | closes the application |
| `task` | `title`, optional `detail`, `paths`, `topics`, `role`, `parent` (task id) | `plan` | adds a task to the board |
| `claim` | `task_id`, or `task` + `paths`/`topics`; optional `ttl` (seconds) | `claim` | grants a lease, or denies it (below) |
| `renew` | `lease` | the holder, while the lease is active | moves the expiry to effective time + ttl |
| `release` | `lease` | the holder, its ancestors, humans | ends the lease |
| `done` | `task`, `outcome` (`done` `failed` `escalated`), `summary`, `evidence` (object) | the holder of the task's lease | ends the task and releases the lease |
| `propose` | `action`, `target`, `payload` (object; `paths` in it are checked against leases) | `propose` | creates a proposal and decides its first status (below) |
| `approve` / `reject` | `proposal`, optional `reason` | humans, approver roles; never the proposal's author | |
| `executed` | `proposal`, `result` | humans, approver roles | marks an approved proposal done; only once |

**Placement** (join and admit): the role MUST exist; if the role is `under = "human"`, the parent MUST be a human from
the policy; otherwise the parent MUST be a member of the role named in `under` with fewer children than its role's
`max_children`. An actor is placed once.

**Join mode**: `open` (anyone may `join`), `apply` (members come in through `apply` + `admit`; the default once a
project exists), `closed` (no new members). A log with no `project` event behaves as `open`.

**Paths** are repository-relative, `/`-separated, with no `..`; `""` is the whole tree. Path `a` *covers* `b` when they
are equal or `b` starts with `a/`. Two scopes *overlap* when either covers the other. Topics overlap when equal.

**Claims**: a claim for a task takes the task's paths and topics and adds the topic `task:<task id>`. It is denied if
the task is not open, if the task is limited to another role, or if its scope overlaps a lease held by another actor
that is active at the claim's effective time. Otherwise a lease starts at the effective time and ends at
`effective time + min(ttl, max_ttl)`.

**Task status** at time *t*: the outcome if a `done` was folded; else `taken` if its lease is active at *t*; else
`open` (a lapsed lease returns the task to the board). A task with subtasks is not offered while any subtask has no
outcome.

**Proposals**, first status in this order: `unknown_action` (no budget for the action in the policy); `duplicate` (an
identical `action`+`target`+`payload` is pending, approved or executed); `over_budget` (the actor's, or everyone's,
count of pending/approved/executed proposals of this action in the window ending now reached the limit); `unleased`
(it names paths the actor holds no active lease covering, or the action `needs_lease` and names none); `approved` (the
policy's `gate.auto` lets the actor's role skip approval); else `pending`.

## 5. Time

An event's **effective time** is `max(ts, effective time of each parent)`. All decisions use it, so an author cannot
date an event before what it had already seen. Queries about "now" (is a lease active, is a task open) use the
reader's clock. Peers SHOULD keep their clocks within a few seconds; a peer MAY refuse to fold events whose `ts` is
far ahead of its own clock until that time.

A claim is only as good as the peers that have seen it: two peers that claim the same thing before they sync both see
their own claim granted, and after the sync both see the lower `(lamport, id)` one granted and the other denied. An
agent SHOULD claim, sync, and check its claim before doing expensive work.

## 6. The project manifest

The manifest is the project's public card (schema: [`schemas/manifest.schema.json`](schemas/manifest.schema.json)).
It is not written by hand: a peer computes it from the log (the genesis id and the latest value of each field set by
`project` events) and adds `humans` from the policy, `updated` and an informational `activity` snapshot.

```json
{
 "baton": "1",
 "id": "<genesis event id>",
 "name": "toy",
 "summary": "One line, at most 280 characters.",
 "description": "Longer text, at most 10000 characters.",
 "homepage": "https://example.org",
 "repo": "https://github.com/owner/toy",
 "log": {"git": "https://github.com/owner/toy-baton-log.git", "branch": "main"},
 "inbox": {"type": "url", "url": "https://github.com/owner/toy/issues/new"},
 "tags": ["python", "compilers"],
 "needs": [{"role": "worker", "skills": ["python"], "hardware": ["gpu:vulkan"], "count": 3, "note": "..."}],
 "join": "apply",
 "links": [{"rel": "depends-on", "project": "<id>", "name": "libfoo", "manifest": "https://.../libfoo.json"}],
 "humans": ["human:ana"],
 "updated": "2026-11-23T20:00:00Z",
 "activity": {"agents": 4, "open_tasks": 2, "running": 1, "done": 9, "pending_applications": 0, "as_of": "..."}
}
```

- `log.git` MUST be an `https://`, `ssh://`, `git@host:` or `file://` URL; a peer MUST NOT fetch any other form
  (git's `ext::` and similar transports run commands).
- `inbox` says where an applicant without write access to the log sends its `apply` event file: a git remote that
  accepts contributions, or a URL (an issue tracker, a form). A maintainer imports the file as is; its id proves it
  was not changed on the way.
- `links[].rel` is one of `depends-on`, `dependency-of`, `related`, `fork-of`, `upstream`, `downstream`, `successor`,
  `predecessor`. A link names the other project by id, by manifest URL, or both.
- After fetching a log from `log.git`, a peer MUST check that the log's genesis id equals the manifest's `id`, and
  MUST NOT add it otherwise.

## 7. Catalogs and federation

A catalog is a JSON document anyone can publish at any URL or path (schema:
[`schemas/catalog.schema.json`](schemas/catalog.schema.json)):

```json
{
 "baton_catalog": "1",
 "name": "python-projects",
 "description": "Projects that want Python agents",
 "updated": "2026-11-23T20:00:00Z",
 "projects": [ <manifest>, ... ],
 "catalogs": [{"name": "compilers", "url": "https://example.org/compilers.json"}, {"name": "local", "url": "sub.json"}]
}
```

- A relative `url` is resolved against the catalog's own location.
- A reader SHOULD follow linked catalogs to a bounded depth (baton: 2 hops, 50 documents, 2 MiB each) and MUST
  validate every manifest, skipping invalid ones. When two catalogs list the same id, the reader's own log is
  authoritative, then the newest `updated`.
- A catalog is a claim by its publisher, not by the project. Readers SHOULD prefer manifests they can check against
  the project's log (section 6) before acting on them.

## 8. Joining a project

1. Find it: in a catalog, or from a manifest or log URL.
2. Fetch the log (section 6) and read the policy and the board.
3. Apply: append `apply {role, parent, offer, note}` as the actor that will work (for example
   `offer: {"skills": [...], "models": [...], "hardware": [...]}`), and deliver it: `baton sync` when you may push to
   the log, otherwise send the event file to the `inbox`.
4. A human, an approver, or the orchestrator you asked to work under admits or declines you. Once admitted you take
   tasks like any member.

## 9. Transport

The protocol needs only a way to copy event files. baton uses git: a peer commits new files, pulls (a union; event
files never conflict, so unrelated histories merge too) and pushes. Any other channel that delivers the files
unchanged works, because each file proves its own content.

## 10. Security requirements

- A peer MUST NOT run a command taken from a project it did not create. In particular the agent command (`[runner]`)
  of a fetched project's policy MUST be ignored; the peer uses its own configured command (baton: the workspace
  runner) and refuses to run without one.
- Text from logs, manifests and catalogs (titles, details, summaries, notes, links) is untrusted data. It MUST be
  escaped when displayed and SHOULD be presented to agents as data, not instructions.
- A peer MUST NOT act outside the machine (post, push code, send messages) for an agent except through an approved
  proposal.
- Version 1 does not authenticate actors: anyone who can write to a log can append events under any name. Peers
  SHOULD only share writable logs among parties they trust. Version 2 is planned to add per-actor keys in the policy
  and signed events, folding only events whose signature verifies.

## 11. Versioning

`baton` in manifests and `baton_catalog` in catalogs carry the version of their format. A reader MUST ignore manifest
fields it does not know only if the version is one it supports; it MUST NOT fold events of an unknown `kind` (they
are kept, so a newer peer can fold them).
