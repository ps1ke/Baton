# baton

Leases, rate budgets and an approval gate for many AI agents sharing one repository, with no central server.

An open project with hundreds of agents behind it produced about 8,400 pull requests and issues in under three days:
3,637 still open, one-line fixes written six times over, a coordination thread re-created fifty times, and one human
reconciling claims by hand ([docs/design.md](docs/design.md) has the numbers). baton is the layer that was missing.
Agents do not talk to the forge directly. They:

1. **join** a tree of roles (planner → orchestrators → workers), each with its own model, effort and fan-out;
2. **check** whether anyone is on the paths they want, in the shared log or in an open pull request;
3. **claim** a lease on those paths and topics, which expires unless renewed;
4. **propose** outward actions (a comment, a pull request), which pass duplicate detection, per-actor and total
   rate budgets and a lease check, and wait for a human to **approve** them;
5. and only then does **execute** perform the action, once.

Every peer keeps the whole log (one content-addressed JSON file per event) and computes the same state from it, so
two agents that claim the same file at the same moment end up agreeing on one winner once their peers sync. Git is the
transport: event files never conflict.

## Quick start

```sh
python3 -m baton --home .baton init           # writes .baton/policy.toml; put your name in [gate] humans
export BATON_HOME=.baton
python3 -m baton --actor plan join planner --parent human:you
python3 -m baton --actor orch join orchestrator --parent plan
python3 -m baton --actor w1   join worker --parent orch

python3 -m baton --actor w1 check --path tools/wave_diff.py --repo owner/repo   # anyone here already?
python3 -m baton --actor w1 claim "pin LF in wave_diff" --path tools/wave_diff.py --ttl 1h
python3 -m baton --actor w1 propose comment --target 123 --repo owner/repo --body-file review.md
python3 -m baton status
python3 -m baton --actor human:you approve <proposal id prefix>
python3 -m baton --actor human:you execute <proposal id prefix>          # dry run; add --yes to post
```

To share the log between machines, make `.baton` a git repository with a common remote and run `baton sync` on each
peer (any git remote works, including another peer over SSH).

## Status

0.1: the log, leases, tiers, budgets, the gate, a GitHub adapter for the duplicate check and for comments, and
`sync`. Standard library only, Python 3.11+. `python3 -m unittest discover -s tests` runs the tests.

Not yet: signed events (anyone with write access to the remote can append as anyone), a policy carried in the log,
the worker runner that launches agents at their tier, and verified-result events. See
[docs/design.md](docs/design.md#next).
