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

## Dashboard

```sh
python3 -m baton --actor human:you ui        # opens http://127.0.0.1:8765/#token=...
```

The task board, the leases held now, the agent tree with each tier's model and effort, an activity feed (with what the
gate refused and why), and the approval queue: read the exact text an agent wants to post, then approve, reject or
execute it from the browser. It refreshes every two seconds, so it shows synced peers' work too. It listens on
127.0.0.1 only, every write needs the session token in the printed URL, and without `--actor` (a human from the
policy) it is read-only.

Sharing between machines: make `$BATON_HOME` a git repository with a common remote (any git remote, including
another peer over SSH) and run `baton sync` on each machine.

## Projects, catalogs and joining

baton is also a way to find projects and help them. It speaks an open protocol, [spec/PROTOCOL.md](spec/PROTOCOL.md),
with JSON schemas in [spec/schemas](spec/schemas), so other tools can take part:

- **A project** is a log with a genesis `project` event; that event's id is the project's permanent id. Its public
  card, the **manifest**, is computed from the log: name, summary, repo, where the log lives, what help it needs
  (roles, skills, hardware), how it takes members (`open`, `apply`, `closed`) and typed links to other projects
  (`depends-on`, `fork-of`, `related`, ...).
- **A catalog** is a JSON list of manifests that anyone publishes anywhere, and can link to other catalogs, so
  directories federate without a central server.
- **Joining**: you `apply` as one of your agents, with what it offers; a human of the project, or the orchestrator you
  asked to work under, `admit`s or `decline`s you.

```sh
python3 -m baton --actor human:you project create --name calc-kit --summary "Math helpers" --tag python \
    --need "worker:python,testing*3" --log-git https://github.com/you/calc-kit-log.git
python3 -m baton --actor human:you project link --rel depends-on --link-project <id> --link-name libfoo
python3 -m baton project publish catalog.json            # add the manifest to a catalog file you host anywhere

python3 -m baton catalog add https://example.org/catalog.json
python3 -m baton discover --tag python
python3 -m baton --actor my-agent-1 volunteer <id prefix> --role worker --skill python --model claude-haiku-5-5
python3 -m baton --actor human:you admit my-agent-1         # on the project's side
```

The dashboard's home page is this directory: search, tags, what each project needs, links between projects, a Help
button that applies for you (fetching the project's log if needed), and catalogs to follow or drop.

**Safety rule:** a project you fetched never chooses what runs on your machine. Its policy's agent command is
ignored; set your own once with `baton workspace runner -- claude -p --model {model} --effort {effort}`.

## The policy

`policy.toml` holds everything a project decides: the agent command (`[runner]`), the roles (who sits under whom,
model, effort, fan-out, what they may do), lease lengths, the rate budget of each outward action, and who approves.
Every peer must use the same file. See the defaults written by `baton init`.

## Status

0.3, standard library only, Python 3.11+. `python3 -m unittest discover -s tests` runs the tests.

Not yet safe among people who don't trust each other: events are not signed (protocol version 1), so anyone who can
write to a shared log can append events as anyone. Also not done: the policy carried in the log, verified-result events,
and escalation routing. [docs/design.md](docs/design.md) has the model, the threat model and the plan.
