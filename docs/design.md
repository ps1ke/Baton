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
   author two PRs did not overlap without checking). → Agents only *propose*; a human (or a role the policy names)
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

### Tasks

`task {title, detail, paths, topics, role, parent}` posts work to the board; roles with `plan` in their `can` list
(by default planners and orchestrators) and humans may post. A task can be limited to one role, and can name a
parent task, which then waits until all its subtasks have ended. `claim {task_id}` takes a task: the lease covers the
task's paths and topics plus the topic `task:<id>`, so even a task without paths is held by one actor at a time. The
holder ends it with `done {task, outcome, summary, evidence}`, where outcome is `done`, `failed` or `escalated`, and
the lease is released. A task whose lease lapses without a `done` is open again for anyone.

### The runner

`baton run` takes the oldest open task the actor's role may take (or a named one), launches the policy's agent
command with `{model}` and `{effort}` from the actor's tier, gives it the task prompt (in a `{prompt}` argument if the
template has one, else on stdin), and renews the lease every third of its ttl while the agent runs. The agent inherits
`BATON_HOME`, `BATON_ACTOR` and `BATON_TASK`, so it can call baton itself: split the task, propose an outward action,
or report its own outcome. If it does not, the exit status decides (`0` done, anything else failed), and the evidence
records the exit code, the run time and a digest of the output.

The prompt tells the agent to stay inside its lease, to send everything outward through `propose`, to check before it
claims something about other work, and to escalate when unsure. It is advice, not enforcement: enforcement is the
lease and the gate, which hold whatever the agent does with them.

### Messages

The log is also the only channel between actors. A `message` sits in a task's thread, is addressed to one member, or
replies to another message (and so joins its thread). An actor's inbox holds the messages addressed to it, replies to
what it wrote, and messages posted to the threads of tasks it created or held, until it answers (a reply, or a later
message in the same thread). Because messages are events, they reach every peer through the same sync, survive
restarts, and are visible to humans in the dashboard: there is no side channel where agents agree on something a
human cannot read. A worker blocks on `message --wait`, which polls the log until an answer lands; the runner quotes
a task's earlier thread in the prompt, labelled as information rather than instructions.

### The swarm

`baton swarm` is the scheduler that keeps the tree moving. Each pass it gives every idle seat one job:

- *attend*, for roles that plan: the inbox, the tasks it posted that ended `escalated` or `failed`, and the tasks it
  split whose subtasks have all ended. An item counts as handled once the seat writes in that task's thread or posts a
  follow-up subtask, a fact in the log, so another machine or a restarted swarm agrees on what is still open. Attend
  jobs go first and do not count against `--parallel`, because a worker may be waiting for the answer.
- *work*: the oldest open task meant for the seat's role. Unrestricted tasks only go to roles that cannot plan, so an
  expensive planner does not take chores.

It takes the lease in its own loop, before starting the agent thread, so two seats never start on the same task. It
stops at `--max-runs` (default 20) and, with `--once`, when nothing runs and nothing can start; a task is tried at most
`--max-attempts` times per swarm, so a crashing agent cannot loop. These limits are the swarm's answer to the case
study: the default is to do a bounded amount of work and stop, and to need a human to say "more".

### Leases

`claim {task, paths, topics, ttl}`. Paths are repository prefixes (`runtime` covers `build/CMakeLists.txt`); topics
are exact strings (an item id such as `BUG-1042`, a failing test name). A claim that overlaps another actor's active
lease at its effective time is denied. Two concurrent claims are both "granted locally" until the peers sync; after
that every peer agrees the lower `(lamport, id)` one holds and the other is denied. A lease ends at `ttl` (capped by
`max_ttl`) unless the holder renews it while it is active; the holder, its ancestors in the tree, or a human can
release it.

**A lease is only safe to rely on after a sync.** An agent should claim, sync, and check the claim still holds before
it starts real work.

### Tiers

`join {role, parent}` builds a tree. The policy says, per role, which role it sits under, its model and effort, how
many children it may have and what it may do (`claim`, `propose`). The default:

| role | under | model | effort | fan-out |
|---|---|---|---|---|
| planner | a human | claude-opus-5-5 | high | 4 |
| orchestrator | planner | claude-sonnet-5-5 | medium | 6 |
| worker | orchestrator | claude-haiku-5-5 | low | 0 |

`baton whoami` prints an actor's model and effort, so the launcher of an agent reads its tier from the log rather
than choosing it. Expensive models plan and split work; cheap ones run tests and read diffs. Authority to act
outside never flows down the tree: it comes from the gate.

### The gate

`propose {action, target, payload}` creates a proposal, which is, in order:

- `unknown_action` when the policy has no budget for the action;
- `duplicate` when an identical action (same action, target and payload) is already pending, approved or done;
- `over_budget` when the actor, or everyone together, used the action's limit within its window;
- `unleased` when it names paths the actor holds no lease on (or the action `needs_lease` and names none);
- otherwise `pending`, or `approved` at once if the policy's `[gate] auto` lets the actor's role skip approval.

`approve` / `reject` come from a human in `[gate] humans` or a role in `approver_roles`, never from the proposal's
author. `baton execute` runs an approved proposal through the forge adapter (dry-run unless `--yes`) and appends
`executed` with the result; a second `executed` for the same proposal is ignored.

## Threat model and known gaps

- **Identity is not authenticated yet.** Anyone who can write to the shared git remote can append an event as any
  actor. Next step: sign each event (SSH signatures, as git uses) with a per-actor key listed in the policy, and fold
  only events whose signature verifies.
- **Forward-dated events.** A peer whose clock runs ahead can make its leases look longer-lived. Mitigation planned:
  refuse to fold events more than a configured skew ahead of the local clock until that time arrives.
- **Prompt injection through work items.** Pull request bodies, issues and code are data. Workers should run with no
  forge credentials at all: only the executor, behind the gate, holds a token.
- **Budget evasion by splitting identities.** Total budgets bound it; fan-out caps bound how many identities a planner
  can mint; signatures will bind identities to keys a human issued.
- **The policy is local.** Peers with different `policy.toml` files can reach different decisions. The policy should
  itself be versioned in the log (a `policy` event approved by a human), so every peer folds with the same one.

## Next

1. Signed events and a policy carried in the log.
2. Sandboxing for the runner: an agent sees only its lease's paths, and holds no forge credentials.
3. Verified results as first-class events (`verdict {pr, commit, command, exit, output_digest, baseline}`), so a
   review that says "tests pass" names the exact commit and command, and a claim about another PR is checked
   mechanically before it is posted.
4. ~~Escalation routing~~ and ~~scheduling~~: done in 0.4 (`message`, `baton swarm`). Still open: per-role
   concurrency, cost caps (tokens and money, from the agents' own reports), and swarms on several machines sharing
   one budget.
5. Push instead of poll: a transport that tells peers about new events at once (a webhook, a small relay, or
   `git ls-remote` watching), so a question across machines is answered in seconds, not one sync interval.
6. Metrics from the log: duplicates refused, budget refusals, lease contention per path, cost per executed action.
