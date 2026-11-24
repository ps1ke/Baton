# The baton protocol, version 3

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
 "key": "<the author's Ed25519 public key, 64 hex>",
 "id": "<sha256 of the canonical form of the seven fields above>",
 "sig": "<Ed25519 signature, by key, of the bytes 'baton-event-v3:' + id>"
}
```

- `id` MUST equal the SHA-256 of the canonical form of the object without `id` and `sig`. `sig` MUST be a valid
  Ed25519 signature by `key` of `baton-event-v3:` followed by the id (ASCII). A peer MUST reject an event whose id or
  signature does not check, whatever channel brought it.
- `parents` are the ids of the events the author had seen and that no other seen event names as a parent (its
  *heads*), sorted. `lamport` MUST be greater than every parent's `lamport`.
- `ts` is the author's clock in seconds since the Unix epoch. It is advisory (section 5).
- An event is stored as a file named `<id>.json` in the log's `events/` folder. Files are never changed or removed, so
  two copies of a log merge by taking the union of their files.
- A peer MUST hold back (not fold) an event until all its parents are present, and MUST NOT fold an event whose
  `lamport` is not above its parents'.

## 4. Event kinds and folding

Every peer computes the same state by folding the admitted events in **ascending `(lamport, id)` order** with these
rules. An event that breaks a rule is kept in the log but has no effect ("ignored", with a reason). A claim that is
refused is "denied". *Effective time* is defined in section 5.

**Authority comes from keys.** Every actor is bound to one public key, and an event counts only if it is signed by its
actor's key (otherwise it is ignored: "signed by a key that is not X's"):

- The **author** is whoever signs the genesis (the first `project` event, which MUST name the project's `repo`): a
  project always starts from a repository. The author holds every right, alone grants and revokes stewards, and alone
  sets the policy (`policy` events).
- **Stewards** are the actors the author `grant`s: machines that run the top level, or humans who decide. Together
  with the author they are **level 0**: they post root packages, plan and integrate the top level (4.3), admit and
  decline, approve, reject and execute proposals, change the project, and alone may propose and execute a `push` to
  the repository.
- **Members** are machines bound to the key they joined or applied with. They choose no role: the network (4.3)
  decides which package each does with which model, at levels 1 and deeper, and in root packages that are not
  orchestration. They cannot make level-0 decisions.
- A log without a genesis is **private**: the humans listed in the local policy (`gate.humans`) decide, each bound to
  the first key it signs with.

When no steward is present, the level-0 packages wait and every other package goes on; decisions and pushes resume
when a steward returns. A revoked actor's later events are ignored and its leases end at once.

| kind | body | who | effect |
|---|---|---|---|
| `project` | any of the manifest fields (section 6); `null` removes a field | the genesis: anyone (who becomes the author); later: level 0 | the genesis MUST have `name` and `repo`; its id is the project id. Later ones update fields. |
| `grant` | `to`, `key`, `right` (`steward` or `member`) | the author | binds `to` to `key` with that right (also a key rotation) |
| `revoke` | `actor`, optional `reason` | the author (anyone but itself); stewards (members only) | the actor's later events are ignored; its leases end |
| `policy` | `toml` (the policy file's text, ≤ 20000 chars) | the author | the policy in force from this event on |
| `join` | optional `offer` | anyone not yet a member, only while `join` is `open` | makes the actor a member; with an offer, it is present from now |
| `apply` | `offer`, `note` (≤ 2000 chars) | a non-member, unless `join` is `closed` | a pending application; one pending per actor |
| `admit` / `decline` | `applicant`, optional `reason` | level 0 | makes the applicant a member (its offer counts from now) / closes the application |
| `offer` | an offer (4.1) | members | replaces the member's offer and marks it present for `min(ttl, presence_ttl)` |
| `task` | a work package (4.2) | level 0 anywhere; members only as children of an orchestrate package they hold | adds a package to the tree |
| `claim` | `task_id` and `model`, or `task` + `paths`/`topics`; optional `ttl` (seconds) | members (present), humans | grants a lease, or denies it (4.3) |
| `renew` | `lease` | the holder, while the lease is active | moves the expiry to effective time + ttl |
| `release` | `lease` | the holder, level 0 | ends the lease |
| `done` | `task`, `outcome` (`done` `split` `failed` `escalated`), `summary`, `report` (4.2), `evidence` | the holder of the package's lease | reports the package (4.4) and releases the lease |
| `message` | `text` (1–4000 chars), optional `task`, `to` (a member), `reply_to`, `ask` (bool) | any member or human | adds a message to the conversation (4.5); changes nothing else |
| `propose` | `action`, `target`, `payload` (object; `paths` in it are checked against leases) | any member or human | creates a proposal and decides its first status (4.6) |
| `approve` / `reject` | `proposal`, optional `reason` | level 0; never the proposal's author | |
| `executed` | `proposal`, `result` | level 0 | marks an approved proposal done; only once |

**Join mode**: `open` (anyone may `join`), `apply` (members come in through `apply` + `admit`; the default once a
project exists), `closed` (no new members). A log with no `project` event behaves as `open`. Names starting with
`human:` cannot join or apply; humans come from the genesis and from grants.

**Paths** are repository-relative, `/`-separated, with no `..`; `""` is the whole tree. Path `a` *covers* `b` when they
are equal or `b` starts with `a/`. Two scopes *overlap* when either covers the other. Topics overlap when equal.

### 4.1 Offers and presence

An offer (schema: [`schemas/offer.schema.json`](schemas/offer.schema.json)) says what one machine puts at the
project's disposal:

```json
{"models": [{"id": "claude:claude-sonnet-5-5", "tier": "standard", "family": "claude"},
            {"id": "codex:gpt-6.1-sol", "tier": "strong", "family": "codex"}],
 "slots": 3, "ttl": 180,
 "hardware": {"tags": ["cores:8", "ram:32g", "gpu", "gpu:nvidia", "vram:24g", "disk:400g"], "cpu_cores": 16,
              "cpu_utilization": 12.5, "ram_gb": 32, "gpus": [{"name": "...", "vram_gb": 24}], "disk_free_gb": 400}}
```

- `models[].id` is `provider:model`; `tier` is one of `fast` < `standard` < `strong` < `frontier`; `family` groups
  models that tend to make the same mistakes (a provider, or `local`). The machine's owner chooses which models are
  offered and their tiers; the protocol only uses the tiers.
- `slots` (0–64) is how many packages the member runs at once. Its active leases count against it.
- A member is **present** at time *t* when its last offer's effective time ≤ *t* < that time + `min(ttl,
  presence_ttl)`. Only present members are given work.

### 4.2 Work packages and reports

A **work package** is the body of a `task` event (schema: [`schemas/package.schema.json`](schemas/package.schema.json)):
`title` (required), `kind` (`orchestrate`, `implement`, `test`, `review`, `investigate`; default `orchestrate` for
size `L`/`XL`, else `implement`), `goal`, `context`, `paths`, `topics`, `acceptance` (list of `{"criterion"}` or
`{"check", "run"}`), `deliverables`, `constraints`, `inputs`, `size` (`S` `M` `L` `XL`), `needs` (`tier`,
`hardware` tags), `parent` (a package id), `depends_on` (sibling package ids). Unknown fields make the event ignored,
so every peer reads the same package.

- **Level**: a package without a parent is at level 0; a child is one level below its parent.
- **Tier**: the policy's tier for the kind (for `orchestrate`, the tier of its level: `tiers.orchestrate[level]`),
  raised to `needs.tier` if that is higher.
- Children: only an `orchestrate` package has children. A member may post a child only while it holds the parent's
  lease, only of kind `orchestrate`, `implement`, `investigate` or `test`, only with paths inside the parent's, at most
  `max_children` per parent, and an `orchestrate` child only above `max_depth`.
- `depends_on` names siblings; the package waits until all of them are `done`, and is `blocked` if one failed or
  escalated.

A **report** (the `report` of a `done`; schema: [`schemas/report.schema.json`](schemas/report.schema.json)) has
`changes` (`[{path, summary}]`), `verification` (`{status: passed|failed|not_run, checks: [{name, status, command,
detail}]}`; it cannot be `passed` while a check is not), `findings` (`[{severity: critical|high|medium|low|info,
text, path, line}]`), `verdict` (`passed` or `changes_requested`, for reviews) and `notes`.

### 4.3 Assignment: the network decides

At any time *t* the network's **assignment** gives every open package at most one present member and one model. Every
peer computes it from the state alone:

0. In a project, an `orchestrate` package at level 0 goes only to a level-0 member (a steward machine). While none is
   present it waits; nothing else waits for it.
1. Packages are taken in this order: `review` and `test`, then `orchestrate`, then `investigate`, then `implement`;
   within a kind by level, then age, then id.
2. The tier sought is the package's tier while any present member that has the package's hardware tags offers a model
   of that tier or above (busy or not). Otherwise, once the package has waited `downgrade_after` × *n*, it accepts
   the tier *n* steps lower, so work never waits forever for a model nobody offers.
3. Candidates are present members with a free slot, the package's hardware tags, and a model of the tier sought or
   above. Each candidate's model is its cheapest such model (for a review: preferring another family than the one
   that wrote the change).
4. The chosen candidate minimises, in order: for a review, using the same family as the change's author; for a test or
   review, being the member that wrote the change; the tier distance above what is sought; its load (active leases /
   slots); `sha256(package id + "/" + member)` as hex. Its free slots then drop by one.

A member's claim on a package (`claim` with `task_id`) is **denied** unless the package is open and the assignment at
the claim's effective time gives it to that member, and the `model` claimed is one it offers of at least the tier
sought. A human's claim is only checked for the package being open. A claim for a package takes the package's paths
and topics and adds the topic `task:<package id>`; any claim is denied if its scope overlaps a lease held by another
actor that is active at the claim's effective time. A lease starts at the effective time and ends at `effective time
+ min(ttl, max_ttl)`.

### 4.4 Package status and the pipeline

**Status** at time *t*: the outcome (`done`, `failed`, `escalated`) if it has one; else `taken` if its lease is active;
else `checking` (an implement package in its pipeline); else for an orchestrate package that was split, `waiting`
until every child has an outcome and then `open` (to integrate); else `stalled` if it was claimed `max_attempts` times
in this round without a report; else `waiting`/`blocked` by its dependencies; else `open`.

`done` rules:

- `split`: only for an orchestrate package that has a child without an outcome. The package waits for its children,
  then reopens to be integrated (by whichever member the network picks). `done` on an orchestrate package is refused
  while a child has no outcome.
- An `implement` package reported `done` enters the **pipeline** (`[pipeline] implement`, default `test` then `review`):
  the network posts a derived package for the first step, with id `sha256(canonical({"derived": kind, "of": implement
  id, "round": rework count, "step": index}))`, the implement package's level, paths, acceptance and constraints, the
  topic `check:<first 12 characters of its id>`, the policy's tier for that kind and `by = "network"`.
- When a derived package ends: a **test** passes only with outcome `done` and `verification.status = passed`; a
  **review** passes only with outcome `done`, verdict `passed` and no `critical` or `high` finding. A pass starts the
  next step, or ends the implement package `done` after the last one. A failure (outcome `failed`, or a check that
  did not pass) sends the implement package back: its rework count rises, the derived report is added to its
  feedback, and it is open again; past `max_rework` it ends `escalated`. A derived package that ends `escalated`
  escalates the implement package (the check could not be made).

### 4.5 Messages

A message is ignored if its `task` or `reply_to` is unknown, if `to` is not an actor, or if a reply names a `task`
other than its message's. A reply belongs to the thread of the message it answers. A **question** (`ask: true`) is for
the actor named in `to`, or else the actor that posted the package of its thread (even if that is the asker's own
machine), until any reply to it is folded. Other messages are for the actor they are addressed to, replies to it, or
messages in the thread of a package it posted or worked on, until it writes in that thread. Messages never grant,
approve or perform anything, and their text is untrusted (section 10).

### 4.6 Proposals

First status in this order: `unknown_action` (no budget for the action in the policy); `not_level0` (a `push` from
an actor that is not level 0); `duplicate` (an identical
`action`+`target`+`payload` is pending, approved or executed); `over_budget` (the actor's, or everyone's, count of
pending/approved/executed proposals of this action in the window ending now reached the limit); `unleased` (it names
paths the actor holds no active lease covering, or the action `needs_lease` and names none); `approved` (the policy's
`gate.auto` lists the actor for this action, or lists `stewards` and the actor is level 0); else `pending`.

### 4.7 Code

Every package runs in a fresh worktree of the project's repository at its `base` commit (a root package names it;
children, tests and reviews inherit it), with its inputs applied: an implementer's finished dependencies, the change a
test or review checks, or every child an integration combines. What the agent changed inside the package's paths
becomes a **patch**: a blob named by its SHA-256, referenced by the report (`patch`, `base`), carried peer to peer like
events; changes outside the paths are dropped and listed in `outside`. A repository that exists only in a folder on the author's machine (`file://`) reaches other machines as a **bundle**:
the root package's `bundle` names a blob holding a git bundle of its base commit (ref `refs/baton/bundle`), which a
machine without the folder fetches before checking the base out; a steward on the author's machine pushes results back
into the folder. When a root package is done, a steward proposes
`push {branch: "baton/<12 characters of its id>", base, patch, package, title}`; once approved, a level-0 machine
applies the patch on its base, commits and pushes that branch. Members never need write access to the repository.

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
 "baton": "3",
 "id": "<genesis event id>",
 "name": "toy",
 "summary": "One line, at most 280 characters.",
 "description": "Longer text, at most 10000 characters.",
 "homepage": "https://example.org",
 "repo": "https://github.com/owner/toy",
 "log": {"git": "https://github.com/owner/toy-baton-log.git", "branch": "main"},
 "inbox": {"type": "url", "url": "https://github.com/owner/toy/issues/new"},
 "tags": ["python", "compilers"],
 "needs": [{"tier": "strong", "kind": "review", "skills": ["python"], "hardware": ["gpu:vulkan"], "count": 3, "note": "..."}],
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
2. Fetch the log (section 6) and read the policy and the package tree.
3. Apply with a machine: append `apply {offer, note}` (4.1) as the member name the machine will use, and deliver it:
   `baton sync` when you may push to the log, otherwise send the event file to the `inbox`. There is no role to ask
   for: the network decides what the machine does.
4. A human or an approver admits or declines it. Once admitted, the machine keeps its offer fresh (`offer` events
   before `presence_ttl` runs out) and takes the packages the network assigns to it.

## 9. Transport

The protocol needs only a way to copy event files and blobs; each proves its own content (events by id and
signature, blobs by hash). baton connects machines directly:

- Every machine that takes part runs a **node** listening on TCP, with a self-signed TLS certificate. Other nodes pin
  it by the SHA-256 fingerprint of its DER encoding, learnt from the member's signed offer (`peer: {addr, cert}`), from
  an invite, or given by hand. There is no certificate authority and no server.
- Before authentication a node SHOULD accept only small messages, finish the handshake within seconds, and limit
  connections per address and in total.
- After TLS, the client sends `hello {v: 3, actor, key, nonce, project, token}`; the server answers `challenge
  {nonce}`; the client signs `baton-p2p-v3|` + that nonce + `|` + the server's certificate fingerprint (binding the
  proof to this TLS session) and sends `auth {sig}`; the server signs `baton-p2p-v3|server|` + the client's nonce +
  `|` + the client's key and sends `welcome {access, actor, key, sig}`.
- **Access**, from the server's own state: a key bound to an actor that is not revoked gets `full` access; the holder
  of a valid invite token, or anyone when the project's join mode is `open`, gets `applicant` access (it sees only
  `project`, `policy` and `grant` events, and only its own `apply` or `join` is accepted from it); anyone else is refused.
- Then each side sends the ids of what it holds (`have {events, blobs}`), and each sends the events the other lacks
  (`data {events, sending, want}`), then each announced blob in its own `blob {id, data}` frame, then `blobs-done`. A
  receiver accepts only blobs that were announced and match their hash.
  Messages are JSON, each prefixed by its length as 4 bytes, big-endian. Every received event and blob is checked
  before it is written.
- An invite is `baton://<host:port>/<project id>?cert=<fingerprint>&token=<token>`; only level 0 issues them, and
  the issuing node keeps only a hash of each token, with an expiry.

**Reaching nodes behind routers.** A node's offer carries `peer`: `{addr, cert, relay}` when it can be reached
(`relay: true` if it relays for members), or `{via, cert}` naming the member that relays for it, and in either case
MAY list `addrs`: its addresses on local networks and VPNs, which members that share such a network try in turn. A node that cannot be
reached SHOULD first ask its router to forward its port (NAT-PMP, then UPnP IGD), mapping only its own TCP port, for
a limited lease that it renews and removes on exit. Otherwise it keeps one connection open to a relay
(`relay-register`, answered by `registered {observed}`). To reach it, a member asks the relay:

- `punch {to}`: the relay answers `punch-ok {at}` with the target's address as the relay sees it, and tells the
  target the asker's; both then connect to each other at once from the ports the relay saw (TCP simultaneous open).
  On the resulting connection, the actor whose name sorts first acts as the TLS server.
- `relay-connect {to}`: the relay tells the target `dial {session}`, the target opens a second connection and answers
  `relay-accept {session}`, both receive `relay-ok`, and the relay copies bytes between the two connections. The two
  ends then authenticate each other as in a direct connection, binding to the target's own certificate.

A relay MUST only introduce and bridge members of its project (keys bound, not revoked) and SHOULD limit concurrent
bridges, their duration and their size. A relay holds the log already, and every event is signed, so it can neither
learn nor alter anything a member could not.

**UDP hole punching.** For a discoverable project, and for a machine that applied and waits to be found, each node
keeps one UDP socket. From it, it announces itself on the DHT under the project's UDP key (the first 20 bytes of
SHA-256(`baton-v3-udp:` + project id)) with `implied_port: 1`, so the DHT records the public address and port its
router gave that socket, and it looks the key up and sends punch packets to every address found. Packets start with
`BTN3` and a type: `PUNCH` (1) carries a random 8-byte nonce and the UDP key; `DATA` (2) a connection id (the first 8
bytes of SHA-256 of the two nonces, smaller first), a 32-bit sequence number and up to 1100 bytes; `ACK` (3) the
connection id and the next sequence number expected; `FIN` (4) the connection id. Data is retransmitted until
acknowledged, with at most 64 packets in flight. Over the stream the node with the larger nonce is the TLS server,
and the connection then proceeds exactly as over TCP. Two routers that both give each destination a different port
(symmetric NAT) cannot be punched.

**Local discovery.** A node of a discoverable project answers on UDP multicast group 239.255.73.42 port 7419, on each
local network and VPN interface: to `{t: "baton-where", key}` it replies `{t: "baton-here", key, port, cert}`, where
`key` is the first 32 hex characters of SHA-256(`baton-lan-v3:` + project id); it also sends that reply unasked now
and then. What is found this way is checked exactly like an address from the DHT.

**Discovery.** A project whose author sets `discoverable: true` may be found on the BitTorrent DHT (BEP 5): its key
is the first 20 bytes of SHA-256(`baton-v3:` + project id). Reachable nodes announce their peer port under it. An
address found there is not trusted: the node MUST prove its key, which MUST be a member's key in the asker's log, or,
for a newcomer, the genesis signer's or a key the genesis signer granted (`grant` is public). An open discoverable
project can be joined with its id alone; others still need an invite.

Peers SHOULD exchange with every known node every few seconds while agents work. A git remote (commit, pull, push)
remains a valid transport for the same files.

## 10. Security requirements

- A peer MUST NOT run a command taken from a project. What runs for each model is the machine owner's choice alone
  (baton: the workspace's `machine.commands`, or its defaults per provider); a policy has no place for a command, and
  a peer refuses a policy that names one. Acceptance `run` commands in packages are text for the agent, which runs
  them under the machine's own tool permissions; a peer never executes them itself.
- A machine offers only models its owner allowed. Probing the machine reads local files and asks only local commands
  and servers on 127.0.0.1; credentials are read for the plan name only and never leave the machine.
- Text from logs, manifests and catalogs (titles, details, summaries, notes, links) is untrusted data. It MUST be
  escaped when displayed and SHOULD be presented to agents as data, not instructions.
- A peer MUST NOT act outside the machine (post, push code, send anything outside the log) for an agent except through an approved
  proposal.
- Every event is signed and every actor bound to one key, so no peer can write as another, and a relay cannot alter
  what it carries. Level-0 rights (top-level decisions, admissions, approvals, pushes) come only from the author's
  key, through grants. A machine's private keys never leave it.
- Peers connect only to pinned certificates, and give the log only to members. Members cannot write to the
  repository: only a level-0 machine pushes, and only branches named `baton/...`, after approval.
- Known limits: an offer is a machine's own claim (the pipeline's independent checks bound the damage); a member can
  fill its own copy of a log with valid but useless signed events (peers SHOULD cap what they accept per exchange);
  the author's key is the root of trust and SHOULD be kept offline from the machines that run agents.

## 11. Versioning

`baton` in manifests and `baton_catalog` in catalogs carry the version of their format. Version 2 replaced version 1's
roles (members chose `planner`/`orchestrator`/`worker` and joined under a parent) with offers and assignment by the
network, made `task` bodies work packages, and added the pipeline, `offer` and questions. Version 3 signs every
event, takes authority from the author's key (`grant`, `revoke`, `policy`), restricts level 0 to stewards, requires a
repository, adds patches and `push`, and defines the peer-to-peer transport. Logs of earlier versions do not fold
under version 3 rules. A reader MUST ignore manifest
fields it does not know only if the version is one it supports; it MUST NOT fold events of an unknown `kind` (they
are kept, so a newer peer can fold them).
