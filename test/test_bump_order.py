#!/usr/bin/env python3
"""Controls for .github/scripts/bump_order.py (k8scc#31), offline.

A throwaway git repo with a linear main (c1 -> c2 -> c3) and a side branch (s1
off c1) stands in for k8scc's history. Every outcome is asserted, and every
"leave it" outcome has the push it must NOT become: an "older" that pushed is
the race itself, an "unknown" that pushed is the race with an excuse.

Run: python3 test/test_bump_order.py
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
sys.dont_write_bytecode = True
import bump_order  # noqa: E402

SCRIPT = ROOT / ".github" / "scripts" / "bump_order.py"


class Order(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        def git(*a):
            return subprocess.run(["git", *a], check=True, capture_output=True, text=True, env=env).stdout.strip()
        git("init", "-q", "-b", "main")
        cls.c = {}
        for name in ("c1", "c2", "c3"):
            git("commit", "-q", "--allow-empty", "-m", name)
            cls.c[name] = git("rev-parse", "--short=7", "HEAD")
        git("checkout", "-q", "-b", "side", cls.c["c1"])
        git("commit", "-q", "--allow-empty", "-m", "s1")
        cls.c["s1"] = git("rev-parse", "--short=7", "HEAD")
        git("checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        cls.tmp.cleanup()

    def run_script(self, on_branch, new):
        r = subprocess.run([sys.executable, str(SCRIPT), on_branch, new], capture_output=True, text=True)
        return r.returncode, r.stdout.split("\t", 1)[0]

    def test_newer_pushes(self):
        self.assertEqual(bump_order.order(self.c["c2"], self.c["c3"])[0], "newer")
        self.assertEqual(self.run_script(self.c["c1"], self.c["c3"]), (0, "newer"))

    def test_older_does_not_push(self):
        # 2026-10-15's would-be race: c3 already on the branch, c2's build finishes last.
        self.assertEqual(bump_order.order(self.c["c3"], self.c["c2"])[0], "older")
        self.assertEqual(self.run_script(self.c["c3"], self.c["c2"]), (1, "older"))
        with self.subTest(control="the same pair the other way round pushes"):
            self.assertEqual(self.run_script(self.c["c2"], self.c["c3"]), (0, "newer"))

    def test_absent_pushes(self):
        self.assertEqual(self.run_script("", self.c["c1"]), (0, "absent"))

    def test_same_does_not_push(self):
        self.assertEqual(self.run_script(self.c["c2"], self.c["c2"]), (1, "same"))

    def test_unknown_never_pushes(self):
        cases = {
            "diverged": (self.c["s1"], self.c["c3"]),
            "commit not in checkout": ("deadbee", self.c["c3"]),
            "new not in checkout": (self.c["c2"], "deadbee"),
            "garbage pin": ("<unreadable>", self.c["c3"]),
        }
        for label, (b, n) in cases.items():
            with self.subTest(case=label):
                rc, outcome = self.run_script(b, n)
                self.assertEqual((rc, outcome), (1, "unknown"))
        with self.subTest(control="the diverged commit's own ancestor line still reads newer"):
            self.assertEqual(self.run_script(self.c["c1"], self.c["s1"]), (0, "newer"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
