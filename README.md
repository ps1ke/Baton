# baton

A peer-to-peer network of AI agents working on one repository: machines connect to each other directly, the
network decides who does what, and only the author's trusted machines can decide for the project or write to its
repository.

Point a few agents at a codebase and they work. Point hundreds at it and they write the same fix six times, collide on
the same files, flood reviewers faster than anyone can read, and lean on one human to keep track of who does what.
baton is the layer between the agents and the project that stops that:

- **Machines, not roles.** Each machine finds out what it is (CPU, memory, GPUs, disk) and which agents it can run
  (Claude Code and its plan, Codex and its plan and models, local model servers such as Ollama, LM Studio, llama.cpp
  and vLLM). Its owner picks which subscriptions and models may be used, at which tier, and how many work packages may
  run at once. That is all a machine offers.
- **The network decides who does what.** Every open work package goes to one present machine and one model, by a
  deterministic rule every peer computes the same way: the cheapest tier good enough, the least loaded machine, a
  model of another family for reviews. Orchestrators per level, implementers, testers, reviewers and investigators
  are whatever the packages need, not something anyone volunteers to be.
- **Work packages in a standard format.** Title, kind, goal, context, scope, acceptance checks, deliverables,
  constraints, size, needs, dependencies, and a standard report at the end (changes, verification, findings, verdict).
- **Orchestration by level.** A big package is split by an orchestrator at level 0 (a frontier model by default) into
  children; those may be split again at level 1 (strong) and level 2 (standard), down to a maximum depth. When all
  children end, the package comes back to be integrated.
- **Nothing counts until it is checked.** An implemented change is tested, then reviewed (preferably by another model
  family, on another machine); failures go back to the implementer with the findings, a few times at most, then
  escalate.
- **Leases.** Taking a package leases its paths; overlapping claims are refused, so two agents never edit the same
  files at once.
- **A gate.** Anything that leaves the machine (a comment, a pull request) is only *proposed*: duplicate detection,
  rate budgets, a lease check, then a human approves it, and it runs once.
- **Talk in the log.** Agents ask whoever posted their package (`baton message "…" --ask --wait 15m`) and get answers
  in the package's thread; everything reaches every peer and humans can read it all.
- **It starts from a repository, and only level 0 writes to it.** A project begins with its author signing a genesis
  that names the repository. The author grants *stewards*: the machines (and people) trusted with level 0. Only they
  plan the top level, admit machines, approve, and push to the repository. Everyone else works at level 1 and below,
  and never needs write access.
- **Signed, peer to peer.** Every event is signed with its author's Ed25519 key, and machines exchange events and
  patches directly over TLS, pinned by certificate fingerprints they learn from each other's signed offers. Members
  get the log; an invite only shows the project's public card; strangers get nothing.
- **Work continues without the top.** If every steward goes offline, level-0 decisions (planning, integrating,
  admitting, pushing) wait, and everything already planned keeps being built, tested and reviewed.
- **Code travels as patches.** Each package runs in its own git worktree; what changed inside its lease becomes a
  patch named by its hash, which other machines apply to test, review and integrate it. Changes outside the lease are
  dropped.

## Install

```sh
curl -fsSL https://ps1ke.github.io/Baton/install.sh | sh
baton
```

The installer needs Python 3.11+ and git (Linux or macOS) and installs for your user only: the code in
`~/.local/lib/baton`, the `baton` command in `~/.local/bin`. Run it again, or `baton update`, to update.

`baton` opens the app in your browser, and that is where you do everything:

1. **Say who you are**, and **set up this machine**: it finds your CPU, memory, GPU and disk, your Claude Code or Codex
   login and plan, and local model servers; you tick the models baton may use and how many packages may run at once.
2. **Start a project from a repository**: paste its URL, or a folder on this machine (other machines then get the code
   from yours, as a git bundle, and finished work comes back into the folder as `baton/...` branches). You become the author, and this machine a steward (level 0).
   Post the work, make invite links, admit machines, make other machines stewards, approve pushes.
3. **Or join one**: paste an invite link someone sent you (or open their `…/join.html#baton://…` link), or the id of a
   project its author made discoverable. Once
   admitted, press **Start this machine** on the project's page, and the network gives it work.

Website: https://ps1ke.github.io/Baton/

## From the terminal

```sh
export PYTHONPATH=/path/to/baton BATON_HOME=.baton          # or: pip install /path/to/baton
python3 -m baton init                                        # then put yourself in [gate] humans in .baton/policy.toml

python3 -m baton machine                                     # what this machine is and can run
python3 -m baton machine allow claude:claude-haiku-5-5 claude:claude-sonnet-5-5 claude:claude-opus-5-5
python3 -m baton machine slots 3                             # work packages at once

python3 -m baton --actor human:you task "Import bank CSV files" --size L --path importer \
    --goal "the importer reads the 3 bank formats" --check "unit=python3 -m unittest"
python3 -m baton swarm --once                                # this machine joins and works until nothing is left
python3 -m baton tasks --all
```

`baton machine` on a desktop with a Claude Pro login looks like:

```
machine m-6247340e  (Linux 7.2.11-arch1-1)
  cpu     AMD Ryzen 5 5600G with Radeon Graphics: 6 cores / 12 threads, 7.2% busy, load 0.82 1.4 2.58
  memory  15.5 GB, 3.5 GB available
  gpu     NVIDIA GeForce RTX 3060 Ti: 8.0 GB (3.8 used), 49% busy
  disk    /home/you: 31.4 GB free of 196.0 GB
agents:
  claude    logged in, plan pro (claude.ai), 2.1.330 (Claude Code)
  codex     not installed
models (allowed ones are offered; slots: 3):
  [x] claude:claude-haiku-5-5                  fast
  [x] claude:claude-sonnet-5-5                 standard
  [x] claude:claude-opus-5-5                   strong
  [ ] claude:claude-fable-5-2                  frontier
```

Every model has a tier (`fast` < `standard` < `strong` < `frontier`); change one with `baton machine tier <model>
<tier>`. Local models get a tier from their size (≥70b strong, ≥27b standard) and run through an agent harness (Codex
`--oss` or aider) when one is installed. `baton machine --limits` also reads the subscriptions' usage. The command run
for each provider is this machine's own choice (`baton machine cmd claude -- claude -p --model {model} ...`); no
project can set it.

## Many machines, peer to peer

```sh
# the author, in a clone of the repository:
baton --actor human:you project create --name importer --repo https://github.com/you/importer.git --join apply
baton --actor human:you grant m-6247340e          # make your own machine a steward (level 0)
baton --actor human:you invite 203.0.113.7:7420   # prints baton://203.0.113.7:7420/<project>?cert=...&token=...
baton swarm --p2p 0.0.0.0:7420 --advertise 203.0.113.7:7420

# a friend's machine:
baton connect 'baton://203.0.113.7:7420/<project>?cert=...&token=...'   # sees the card, applies with its offer
baton --home <printed path> swarm --p2p 0.0.0.0:7420 --advertise <its address>:7420 --peer 203.0.113.7:7420=<cert>

# back on the author's side:
baton --actor human:you admit m-1a2b3c4d
baton --actor human:you approve <push proposal>   # then a steward machine pushes the branch baton/<package>
```

Machines learn each other's addresses from the log, so after the first contact every member reaches every other. A
machine behind a home router needs no setup: it asks the router to forward its port (NAT-PMP or UPnP), and if the
router will not, it registers with a member that relays, and others reach it through a hole punch or that relay. A
project whose author makes it *discoverable* can also be found on the BitTorrent DHT by its id, like a magnet link.
A git remote still works as a transport (`baton swarm --sync 15s`).

With several machines on one project, each runs its own swarm on its own copy of the log. `baton assign`
shows what the network gives to whom right now, and why a package waits.

Work packages can come from a file in the standard format (`baton task --file package.json`,
[spec/schemas/package.schema.json](spec/schemas/package.schema.json)). Agents report in the standard format too:

```sh
baton done <id> --summary "dates parse" --change importer/dates.py --check "unit=passed:python3 -m unittest"
baton done <id> --verdict changes_requested --finding "high:offset ignored@importer/dates.py:41"
baton done <id> --outcome split --summary "three parts"      # an orchestrator, after posting children
```

Outward actions:

```sh
python3 -m baton check --path src/parser --repo owner/repo   # leases and open PRs on these paths
python3 -m baton propose comment --target 123 --repo owner/repo --body-file review.md
python3 -m baton --actor human:you approve <id prefix>
python3 -m baton --actor human:you execute <id prefix> --yes
```

## Dashboard

```sh
python3 -m baton --actor human:you ui --sync 15s     # opens http://127.0.0.1:8765/#token=...
```

- **This machine**: hardware meters, the agents found with their login and plan, every model with a checkbox and a
  tier, the slots, and the exact offer projects will see.
- **Projects**: the directory of projects from your workspace and the catalogs you follow; Help applies with this
  machine.
- **A project**: the package board (kind, level, tier, status, who has it with which model, the network's next pick,
  its checks, verification and findings), the machines present with their slots in use, the work tree by level,
  leases, applications, an activity feed, and "Waiting for you": questions, applications and proposals to approve.

It listens on 127.0.0.1 only, every write needs the session token in the printed URL, and without `--actor` (a human
from the policy) it can only change this machine's settings.

## Projects, catalogs and joining

baton speaks an open protocol, [spec/PROTOCOL.md](spec/PROTOCOL.md) (version 2), with JSON schemas in
[spec/schemas](spec/schemas): events, work packages, reports, machine offers, manifests and catalogs.

- **A project** is a log with a genesis `project` event; that event's id is the project's permanent id. Its public
  card, the **manifest**, is computed from the log: name, summary, repo, where the log lives, what capacity it needs
  (tiers, kinds of work, skills, hardware), how it takes members (`open`, `apply`, `closed`) and typed links to other
  projects (`depends-on`, `fork-of`, `related`, ...).
- **A catalog** is a JSON list of manifests that anyone publishes anywhere, and can link to other catalogs.
- **Joining**: a machine `apply`s with its offer; a human admits it. There is no role in the application.

```sh
python3 -m baton --actor human:you project create --name calc-kit --summary "Math helpers" --tag python \
    --need "strong:python*2" --log-git https://github.com/you/calc-kit-log.git
python3 -m baton project publish catalog.json

python3 -m baton catalog add https://example.org/catalog.json
python3 -m baton discover --tag python
python3 -m baton volunteer <id prefix> --note "evenings only"
python3 -m baton --actor human:you admit m-6247340e          # on the project's side
```

## The policy

`policy.toml` holds what a project decides: orchestration depth, fan-out and the tier each kind of work and each level
needs (`[network]`, `[tiers]`), the checks every change goes through (`[pipeline]`), lease lengths, the rate budget of
each outward action, and who approves. It never holds a command. Every peer must use the same file.

## Status

0.6 (protocol 3), standard library only, Python 3.11+. `python3 -m unittest discover -s tests` runs the tests.

Every event is signed and authority comes from the author's key; what is still open: an offer is a machine's own
claim (the independent checks limit the damage), nodes need a reachable address (no NAT traversal or relays yet),
agents are not sandboxed beyond their worktree and tool permissions, and there is no cost accounting per model. [docs/design.md](docs/design.md) has the model, the threat model and
the plan.
