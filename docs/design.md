# baton design

baton coordinates any number of agents (any model, any agent program) working on one project. It assumes nothing
about the project beyond a tree of files, and nothing about the agents beyond a command line and an exit status.

## Why: a case study

What happens without such a layer was measured on one public repository that hundreds of AI agents contributed to
(late 2026, about 68 hours after its first pull request). Nothing in baton is specific to it;
the numbers are here because they show the failure modes at scale:

| | |
|---|---|
| PR/issue numbers used | ~8,400 in about 68 hours |
| Peak rate | about 7 a minute at its busiest |
| Pull requests | 3,634 merged, ~845 closed unmerged, 3,637 open |
| Contributors | 414; 931 of the newest 1,000 PRs say they are AI-assisted |
| Coordination issue | re-created about 50 times, each at its 300-comment cap |
| Hot spots | the top-level build file in 124 open PRs, one design document in 48 |
| Duplicates | one one-line fix in 3 open PRs, then 6 a few hours later; another in 5 |
| Trunk | ~33 unit-test files red on `main`, each with a PR (or several) that fixes it |

What failed, and the rule baton takes from it:

1. **Claims were free text in a chat thread** ("no code found" next to many of them), and a script plus one person
   reconciled them. → A claim is a *lease* on paths and topics, with an expiry, decided by a deterministic rule.
2. **Duplicates were found after the work, by reviewers.** → `baton check` looks at leases *and* open pull requests
   on the same paths before work starts, and the gate refuses an identical proposal.
3. **Throughput was set by the agents, not by the reviewers.** → Every outward action has a per-actor and total
   budget per time window, counted from the shared log so no peer can exceed it alone.
4. **Agents posted with authority they had not earned** (including the author of this document, who told a PR
   author two PRs did not overlap without checking). → Agents only *propose*; a human (or a member the policy names)
   approves, and the executed result is recorded so it runs once.
5. **One coordinator was the real single point of failure.** → No server: every peer holds the whole log and
   computes the same state from it.

## Model

### Events

Peers exchange only events. An event is JSON with `kind`, `actor`, `lamport`, `ts`, `parents` (the heads its author
had seen) and `body`; its id is the SHA-256 of that content, and its file is `events/<id>.json`. Files are never
edited, so two peers merge by union, and git carries them without conflicts (`baton sync`). An edited file no longer
matches its id and is reported, not folded. An event whose parents have not arrived yet is held back until they do.

### Order and time

Events fold in `(lamport, id)` order: every peer with the same events gets the same order, and an event always comes
after everything its author had seen. Decisions use the *effective time* `max(ts, parents' effective times)`, so an
event cannot be backdated before what its author already knew.

### Machines and the network

The first versions had roles: a member joined as planner, orchestrator or worker, under a parent, and that fixed its
model. It was the wrong unit. A person helping a project has a machine and some subscriptions, not a preference for
being a "worker", and a project's need for planning, implementation and review changes by the hour. So a member is
now a machine with an **offer**: the models its owner allowed (`provider:model`, a tier, a family), its slots (how
many packages at once), and hardware tags. `baton machine` finds all of it out (CPU and load, memory, GPUs and VRAM,
disk; Claude Code's login and plan, Codex's login, plan and model list, Ollama and OpenAI-compatible servers on
localhost), and the owner only chooses.

The **network** (network.py) gives each open package to one present member and model. It is a pure function of the
folded state and the time, so every peer computes the same answer, and the fold denies a member's claim on a package
the assignment gave to someone else. That is what "the network decides" means here: no coordinator, no election, just
a rule everyone evaluates the same way. The rule prefers, in order: an independent reviewer (another model family,
another machine), the cheapest tier good enough, the least loaded machine, and a hash for an even spread. A package
nobody present can serve steps down one tier per `downgrade_after`, so a project without a frontier model still gets
its plan made, more slowly and visibly.

### Work packages and levels

A `task` body is a **work package** (wp.py): title, kind, goal, context, paths, topics, acceptance (criteria and
runnable checks), deliverables, constraints, inputs, size, needs (tier, hardware), parent, depends_on. Unknown fields
are refused so every peer reads the same package. The report at the end is standard too: changes, verification
(status and checks), findings (with severities), verdict, notes.

Kinds are `orchestrate`, `implement`, `test`, `review`, `investigate`. An orchestrate package at level *n* is split by
whoever the network gives it to (at the tier for level *n*: frontier, strong, standard by default) into children at
level *n+1*, inside its paths, at most `max_children`, and only orchestrate children above `max_depth`. It then waits;
when all its children have ended it reopens and the network gives it out again, to integrate: check the children's
results together, post more children, or close or escalate it. Splitting and integrating are separate runs, so an
orchestrator does not hold a slot while its children work.

### Trust: the author, stewards and members

A project starts from a repository and an author: the genesis event names the repository and is signed by the
author's key, which becomes the root of trust. The author `grant`s steward rights to keys it trusts (its own machines,
co-maintainers). The author and the stewards are **level 0**: they alone plan and integrate the top level, admit
machines, approve and execute proposals, and push to the repository. Members run everything below level 0 and the
pipeline's checks; they cannot decide for the project and need no write access anywhere.

Because every event is signed and every actor is bound to one key, nothing depends on trusting the transport or other
peers: a member's copy of the log can be relayed by anyone, and a forged approval or a "steward" that the author never
granted is simply ignored by every fold. If all stewards go offline, the network goes on with what was already
planned; the top-level decisions, admissions and pushes wait for them. That is the point: losing level 0 loses
direction, not work, and losing a member loses neither.

### Peer to peer

Machines run nodes and connect directly over TLS, each pinning the other's certificate by the fingerprint published
in its signed offer, so discovery itself is authenticated by the log. A connection proves both keys with a challenge
bound to the TLS certificate, then exchanges what each side lacks. Access follows trust: members get the log and the
patches, an invite holder only the project's public card plus the right to deliver its application, strangers
nothing. There is no server and no certificate authority; a git remote can still carry the same files.

### Code as patches

Each package runs in its own worktree at the package's base commit, with its inputs applied from patches: the change a
test or review checks, an implementer's dependencies, an integration's children. The result is captured as a patch
inside the package's paths, so the lease is enforced on the diff itself rather than trusted. Patches are blobs named
by their hash and travel with the events. When a root package is done, a steward proposes a push of a `baton/...`
branch; after approval a level-0 machine applies, commits and pushes it.

### The pipeline

An implement package reported `done` is not done. The fold derives a test package from it (deterministic id, same
level and paths, the policy's tier for tests), then a review package once the test passes. A test passes only on
verification that ran and passed (`not_run` is not a pass), a review only with verdict `passed` and no critical or
high finding. A failure sends the implement package back to the network with the check's report in its feedback,
which the next implementer sees; after `max_rework` it escalates to the integrating orchestrator. This is the case
study's main lesson in code: a fix that "passes" only counts once a different agent, ideally a different model family,
has run its checks and read it.

### The runner

`baton run` and `baton swarm` run an agent on a package with the model the network picked, using this machine's
command for that provider (`machine.commands`, or a default for Claude Code, Codex and local harnesses), `{model}`
and `{effort}` filled in (effort from the tier), the prompt for the package's kind on stdin or in a `{prompt}`
argument, and the lease renewed every third of its ttl. The agent inherits `BATON_HOME`, `BATON_ACTOR` and
`BATON_TASK` and reports with `baton done`. If it does not report, an implement or investigate package that exited 0
counts as done (and goes to its checks); anything else is released back to the network, and a package released
`max_attempts` times in a round stalls for a human instead of looping.

The prompts tell the agent to stay inside its lease, to send everything outward through `propose`, to ask when
unsure, and, for tests and reviews, to change nothing. It is advice, not enforcement: enforcement is the lease, the
pipeline and the gate.

### Messages

The log is also the only channel between actors. A `message` sits in a task's thread, is addressed to one member, or
replies to another message (and so joins its thread). An actor's inbox holds the messages addressed to it, replies to
what it wrote, and messages posted to the threads of tasks it created or held, until it answers (a reply, or a later
message in the same thread). Because messages are events, they reach every peer through the same sync, survive
restarts, and are visible to humans in the dashboard: there is no side channel where agents agree on something a
human cannot read. A worker blocks on `message --wait`, which polls the log until an answer lands; the runner quotes
a task's earlier thread in the prompt, labelled as information rather than instructions.

### The swarm

`baton swarm` is one machine's part in the network. Each pass it renews the machine's offer (before `presence_ttl`
runs out, or at once when the owner changed it), answers the questions waiting for it (asked by agents working on
packages its own agents posted, with a model of the tier of the package they came from), and takes the packages the
assignment gives it, up to its slots, each with the picked model. It takes the lease in its own loop before starting
the agent's thread, syncs in the background, keeps every run's transcript, and stops at `--max-runs` (default 20) or,
with `--once`, when nothing is left for it. These limits are the answer to the case study: the default is a bounded
amount of work, and a human says "more".

### Leases

`claim {task, paths, topics, ttl}`. Paths are repository prefixes (`runtime` covers `build/CMakeLists.txt`); topics
are exact strings (an item id such as `BUG-1042`, a failing test name). A claim that overlaps another actor's active
lease at its effective time is denied. Two concurrent claims are both "granted locally" until the peers sync; after
that every peer agrees the lower `(lamport, id)` one holds and the other is denied. A lease ends at `ttl` (capped by
`max_ttl`) unless the holder renews it while it is active; the holder, a human or an approver can release it.

**A lease is only safe to rely on after a sync.** An agent should claim, sync, and check the claim still holds before
it starts real work.

### The gate

`propose {action, target, payload}` creates a proposal, which is, in order:

- `unknown_action` when the policy has no budget for the action;
- `duplicate` when an identical action (same action, target and payload) is already pending, approved or done;
- `over_budget` when the actor, or everyone together, used the action's limit within its window;
- `unleased` when it names paths the actor holds no lease on (or the action `needs_lease` and names none);
- otherwise `pending`, or `approved` at once if the policy's `[gate] auto` lists the actor for that action.

`approve` / `reject` come from a human in `[gate] humans` or a member in `approvers`, never from the proposal's
author. `baton execute` runs an approved proposal through the forge adapter (dry-run unless `--yes`) and appends
`executed` with the result; a second `executed` for the same proposal is ignored.

## Threat model and known gaps

- **Identity.** Every event is signed (Ed25519) and every actor bound to one key; authority flows only from the
  author's key. The author's key is the single root: it should live on a machine that does not run agents.
- **Forward-dated events.** A peer whose clock runs ahead can make its leases look longer-lived. Mitigation planned:
  refuse to fold events more than a configured skew ahead of the local clock until that time arrives.
- **Prompt injection through work items.** Pull request bodies, issues and code are data. Workers should run with no
  forge credentials at all: only the executor, behind the gate, holds a token.
- **Budget evasion by splitting identities.** Total budgets bound it; fan-out caps bound how many identities a planner
  can mint; signatures will bind identities to keys a human issued.
- **Offers are claims.** A machine can offer a model it will not really use, or hardware it lacks. The pipeline
  limits the damage (another member tests and reviews), and the transcript digest in each report helps audits; signed
  events will at least bind each offer to a key.
- **The policy.** The author publishes it into the log (`baton policy`), so peers fold with the same one; a log whose
  author never published one falls back to each peer's local file.
- **Reachability.** Nodes need an address others can reach; there is no NAT traversal or relay yet.

## Next

1. ~~Signed events and a policy carried in the log~~: done in 0.6. Next: NAT traversal and relays for nodes.
2. Sandboxing for the runner: an agent sees only its lease's paths, and holds no forge credentials.
3. Verified results as first-class events (`verdict {pr, commit, command, exit, output_digest, baseline}`), so a
   review that says "tests pass" names the exact commit and command, and a claim about another PR is checked
   mechanically before it is posted.
4. ~~Escalation routing~~, ~~scheduling~~ and roles chosen by the network: done in 0.4 and 0.5 (`message`, `baton swarm`). Still open: per-model
   concurrency, cost caps (tokens and money, from the agents' own reports), and swarms on several machines sharing
   one budget.
5. Push instead of poll: a transport that tells peers about new events at once (a webhook, a small relay, or
   `git ls-remote` watching), so a question across machines is answered in seconds, not one sync interval.
6. Metrics from the log: duplicates refused, budget refusals, lease contention per path, cost per executed action.
