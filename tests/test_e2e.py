"""Two machines, one forge repository, no server: the whole design end to end.

Machine A belongs to the author (human:ana) and runs m-a, which the author made a steward (level 0). Machine B runs
m-b, an ordinary member that joined through an invite. Each machine has its own keys, workspace, log copy and clone of
the repository, and they talk only peer to peer over TLS. Agents are stand-in scripts that really edit files.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from baton import crypto, p2p, swarm
from baton.store import Store
from baton.workspace import Workspace

from helpers import FABLE, HAIKU, OPUS, POLICY, SONNET, agent, fake_probe

AGENT = r"""
import os, re, subprocess, sys, pathlib
prompt = sys.stdin.read()
b = [sys.executable, '-m', 'baton']
def baton(*args):
    r = subprocess.run(b + list(args), capture_output=True, text=True)
    if r.returncode: print('baton failed', args, r.stdout, r.stderr, file=sys.stderr)
    return r.stdout
task = os.environ.get('BATON_TASK', '')
here = pathlib.Path.cwd()
if 'All the children of this package have ended' in prompt:
    ok = (here / 'src/x/x.py').exists() and (here / 'src/y/y.py').exists()
    baton('done', task, '--outcome', 'done' if ok else 'failed', '--summary', 'both parts present' if ok else 'missing parts')
elif 'You orchestrate' in prompt:
    baton('task', 'part x', '--parent', task, '--kind', 'implement', '--path', 'src/x', '--check', 'x=test -f src/x/x.py')
    baton('task', 'part y', '--parent', task, '--kind', 'implement', '--path', 'src/y', '--check', 'y=test -f src/y/y.py')
    baton('done', task, '--outcome', 'split', '--summary', 'two parts')
elif '(implement,' in prompt:
    part = 'x' if 'part x' in prompt else 'y'
    (here / 'src' / part).mkdir(parents=True, exist_ok=True)
    (here / 'src' / part / (part + '.py')).write_text(part.upper() + ' = 1\n')
    (here / 'OUTSIDE.txt').write_text('not leased\n')
    baton('done', task, '--summary', 'wrote ' + part, '--check', 'file=passed')
elif '(test,' in prompt:
    part = 'x' if 'part x' in prompt else 'y'
    ok = (here / 'src' / part / (part + '.py')).exists() and not (here / 'OUTSIDE.txt').exists()
    baton('done', task, '--summary', 'checked ' + part, '--check', 'file=' + ('passed' if ok else 'failed'))
elif '(review,' in prompt:
    baton('done', task, '--summary', 'fine', '--verdict', 'passed')
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        # the forge: a bare repository with one commit
        self.forge = r / "forge.git"
        git("init", "-q", "--bare", "-b", "main", str(self.forge))
        seed = r / "seed"
        git("clone", "-q", str(self.forge), str(seed))
        (seed / "src").mkdir()
        (seed / "src" / "app.py").write_text("print('hi')\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "start", cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        self.base = git("rev-parse", "HEAD", cwd=seed)
        # two machines
        self.m = {}
        for name in ("A", "B"):
            ws = Workspace(r / name / "ws")
            ws.set_machine(allowed=["claude:" + x[0].split(":")[1] for x in (FABLE, OPUS, SONNET, HAIKU)], slots=2,
                           commands={"claude": agent(AGENT)})
            store = Store(r / name / "log", keyring=crypto.Keyring(ws.dir / "keys"))
            store.init()
            (store.home / "policy.toml").write_text(POLICY, encoding="utf-8")
            self.m[name] = (ws, store)
        wsa, a = self.m["A"]
        a.append("project", "human:ana", {"name": "app", "repo": "file://" + str(self.forge), "join": "apply"})
        a.append("join", "m-a", {})       # refused (apply mode): the author grants its own machine instead
        a.append("grant", "human:ana", {"to": "m-a", "key": a.keyring.public("m-a"), "right": "steward"})
        self.root = a.append("task", "human:ana", {"title": "add parts x and y", "size": "L", "paths": ["src"],
                                                   "base": self.base}).id
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_machines_build_it_peer_to_peer_and_only_the_steward_pushes(self):
        wsa, a = self.m["A"]
        wsb, b = self.m["B"]
        pa, pb = free_port(), free_port()
        # B uses an invite from the author's machine to apply; the author admits it
        node_a = p2p.Node(a, "human:ana", wsa.dir / "tls")
        node_a.serve("127.0.0.1", pa)
        inv = p2p.parse_invite(p2p.invite_url(f"127.0.0.1:{pa}", a.state().project_id, node_a.fp,
                                              node_a.invites.new(a.state().project_id, 600)))
        node_b = p2p.Node(b, "m-b", wsb.dir / "tls")
        node_b.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
        self.assertNotIn("task", {e.kind for e in b.read()[0].values()})        # applicants do not see the board
        b.append("apply", "m-b", {"note": "a member machine"})
        node_b.sync_with(inv["addr"], inv["cert"], token=inv["token"], project=inv["project"])
        a.append("admit", "human:ana", {"applicant": "m-b"})
        node_a.stop()
        fp_a = p2p.tls_identity(wsa.dir / "tls")[2]
        fp_b = p2p.tls_identity(wsb.dir / "tls")[2]
        # each machine runs its swarm with a peer node; they only know each other's address and certificate
        sa = swarm.Swarm(a, "m-a", wsa.machine(), fake_probe("claude"), once=True, interval=0.2, timeout=60, ws=wsa,
                         log=lambda l: self.lines.append("A " + l),
                         p2p={"listen": f"127.0.0.1:{pa}", "advertise": f"127.0.0.1:{pa}", "every": 0.5,
                              "peers": {f"127.0.0.1:{pb}": fp_b}})
        sa.cfg = {**sa.cfg, "allowed": ["claude:claude-fable-5-2", "claude:claude-haiku-5-5"]}   # A: plans and tests
        sb = swarm.Swarm(b, "m-b", wsb.machine(), fake_probe("claude"), once=True, interval=0.2, timeout=60, ws=wsb,
                         log=lambda l: self.lines.append("B " + l),
                         p2p={"listen": f"127.0.0.1:{pb}", "advertise": f"127.0.0.1:{pb}", "every": 0.5,
                              "peers": {f"127.0.0.1:{pa}": fp_a}})
        sb.cfg = {**sb.cfg, "allowed": ["claude:claude-sonnet-5-5", "claude:claude-opus-5-5"]}   # B: builds and reviews
        threads = [threading.Thread(target=sw.run) for sw in (sa, sb)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=240)
        log = "\n".join(self.lines)
        s = a.state()
        root = s.tasks[self.root]
        self.assertEqual(root.outcome, "done", log)
        self.assertEqual(root.runs[0]["member"], "m-a")                          # level 0: the steward only
        kids = [s.tasks[c] for c in root.subtasks]
        self.assertTrue(all(k.runs[0]["member"] == "m-b" for k in kids), log)    # the member built the parts
        self.assertTrue(all(k.report.get("outside") == ["OUTSIDE.txt"] for k in kids))   # dropped: not leased
        tests = [s.tasks[k.checks[0]] for k in kids]
        self.assertTrue(all(t.runs[0]["member"] == "m-a" for t in tests))        # checked on the other machine,
        self.assertTrue(all(t.report["verification"]["status"] == "passed" for t in tests))   # from patches it got
        # the steward proposed the push; only level 0 may approve and execute it
        push = next(p for p in s.proposals.values() if p.action == "push")
        self.assertEqual((push.actor, push.status, push.payload["branch"]), ("m-a", "pending", f"baton/{self.root[:12]}"))
        e = b.append("approve", "m-b", {"proposal": push.id})
        node_b2 = p2p.Node(b, "m-b", wsb.dir / "tls")
        node_a2 = p2p.Node(a, "human:ana", wsa.dir / "tls")
        node_a2.serve("127.0.0.1", pa)
        node_b2.sync_with(f"127.0.0.1:{pa}", fp_a)
        node_a2.stop()
        self.assertIn("level-0 decision", a.state().ignored[e.id])
        a.append("approve", "human:ana", {"proposal": push.id})
        from baton import codeflow
        out = codeflow.push(a, codeflow.checkout(wsa, a), push.payload)
        a.append("executed", "human:ana", {"proposal": push.id, "result": out})
        files = git("ls-tree", "-r", "--name-only", push.payload["branch"], cwd=self.forge).split()
        self.assertEqual(sorted(files), ["src/app.py", "src/x/x.py", "src/y/y.py"])   # no OUTSIDE.txt
        self.assertEqual(git("rev-parse", "main", cwd=self.forge), self.base)          # main untouched


if __name__ == "__main__":
    unittest.main()
