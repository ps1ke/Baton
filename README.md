# baton

Coordination for many AI agents working on one project, with no central server.

Point a few agents at a codebase and they work. Point hundreds at it and they write the same fix six times, collide on
the same files, flood reviewers faster than anyone can read, and lean on one human to keep track of who does what.
baton is the layer between the agents and the project that stops that:

- **A task board.** Planners and orchestrators post tasks (and split big ones into subtasks); workers take them.
- **Leases.** Taking a task, or claiming paths and topics directly, gives a lease that expires unless renewed.
  Overlapping claims are refused, so two agents never work on the same files at once.
- **Tiers.** Agents join a tree (by default planner → orchestrators → workers). Each role has its own model, effort,
  fan-out and permissions, so strong models plan and cheap ones do the bulk work.
- **A gate.** Anything that leaves the machine (a comment, a pull request, a message) is only *proposed*. It passes
  duplicate detection, per-agent and total rate budgets and a lease check, then waits for a human to approve it, and
  runs once.
- **Peer-to-peer.** Every machine holds the whole log, a folder of content-addressed event files, and computes the
  same state from it. Git carries the log between machines (event files never conflict). Two agents that claim the
  same file at the same moment agree on one winner once their peers have synced.
- **Any agent.** The runner launches whatever command the policy names (Claude Code by default; Codex, Aider or a
  script work the same), at the role's model and effort, keeps its lease alive while it works and records the outcome.

## Quick start

```sh
export PYTHONPATH=/path/to/baton BATON_HOME=.baton      # or: pip install /path/to/baton
python3 -m baton init                                    # then put yourself in [gate] humans in .baton/policy.toml

python3 -m baton --actor plan join planner --parent human:you
python3 -m baton --actor orch join orchestrator --parent plan
python3 -m baton --actor w1   join worker --parent orch
python3 -m baton --actor w2   join worker --parent orch

python3 -m baton --actor orch task "fix mean() in calc/ops.py" --path calc
python3 -m baton --actor orch task "fix slug() in text/slug.py" --path text
python3 -m baton --actor w1 run &          # each worker takes the oldest open task its role may take
python3 -m baton --actor w2 run &
wait; python3 -m baton tasks --all
```

That is a real run: two Claude Code workers (Haiku, low effort) fixed one bug each, in parallel, each inside its own
lease, in under a minute.

Outward actions:

```sh
python3 -m baton --actor w1 check --path src/parser --repo owner/repo    # leases and open PRs on these paths
python3 -m baton --actor w1 propose comment --target 123 --repo owner/repo --body-file review.md
python3 -m baton status
python3 -m baton --actor human:you approve <id prefix>
python3 -m baton --actor human:you execute <id prefix> --yes
```

Sharing between machines: make `$BATON_HOME` a git repository with a common remote (any git remote, including
another peer over SSH) and run `baton sync` on each machine.

## The policy

`policy.toml` holds everything a project decides: the agent command (`[runner]`), the roles (who sits under whom,
model, effort, fan-out, what they may do), lease lengths, the rate budget of each outward action, and who approves.
Every peer must use the same file. See the defaults written by `baton init`.

## Status

0.2, standard library only, Python 3.11+. `python3 -m unittest discover -s tests` runs the tests.

Not yet safe among people who don't trust each other: events are not signed, so anyone who can write to the shared
remote can append events as anyone. Also not done: the policy carried in the log, verified-result events,
and escalation routing. [docs/design.md](docs/design.md) has the model, the threat model and the plan.
