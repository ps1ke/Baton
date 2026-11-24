"""Auto-update on a fake installed copy: no network, a tarball made here stands in for GitHub."""

import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

import helpers  # noqa: F401  (a throwaway workspace)
from baton import update

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = textwrap.dedent('''
    import sys
    from pathlib import Path
    from baton import update
    tarball = Path(sys.argv[1]).read_bytes()
    update._get = lambda url, accept, limit: tarball
    try:
        print("NEW", update.apply("b" * 40))
    except Exception as err:
        print("ERR", type(err).__name__, err)
''')


def tarball(version: str, broken: bool = False, not_baton: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in (ROOT / "baton").glob("*"):
            if f.is_file():
                data = f.read_bytes()
                if f.name == "__init__.py":
                    data = data.replace(b'__version__ = "', b'__version__ = "' + version.encode() + b"-")
                if broken and f.name == "cli.py":
                    data = b"this is not python\n"
                if not_baton and f.name == "cli.py":
                    continue
                info = tarfile.TarInfo(f"Baton-bbbb/baton/{f.name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Path(self.tmp.name) / "lib" / "baton" / "app"
        shutil.copytree(ROOT / "baton", self.app / "baton")
        (self.app / ".baton-commit").write_text("a" * 40 + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_update(self, data: bytes) -> str:
        t = Path(self.tmp.name) / "release.tar.gz"
        t.write_bytes(data)
        r = subprocess.run([sys.executable, "-c", SCRIPT, str(t)], env={"PYTHONPATH": str(self.app), "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=120, cwd=self.tmp.name)
        return r.stdout.strip() + r.stderr.strip()

    def test_an_installed_copy_updates_to_the_new_commit_and_keeps_the_old_one(self):
        out = self.run_update(tarball("9.9"))
        self.assertTrue(out.startswith("NEW 9.9-"), out)
        self.assertEqual((self.app / ".baton-commit").read_text().strip(), "b" * 40)
        self.assertTrue((self.app.with_name("app.old") / "baton" / "cli.py").exists())

    def test_a_broken_or_foreign_download_changes_nothing(self):
        before = (self.app / "baton" / "__init__.py").read_text()
        self.assertIn("does not start", self.run_update(tarball("9.9", broken=True)))
        self.assertIn("does not look like baton", self.run_update(tarball("9.9", not_baton=True)))
        self.assertEqual((self.app / "baton" / "__init__.py").read_text(), before)
        self.assertEqual((self.app / ".baton-commit").read_text().strip(), "a" * 40)

    def test_a_development_checkout_never_updates_itself(self):
        self.assertFalse(update.installed())          # these tests run from the git checkout
        with self.assertRaises(RuntimeError):
            update.apply("c" * 40)


if __name__ == "__main__":
    unittest.main()
