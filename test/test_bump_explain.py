#!/usr/bin/env python3
"""Controls for .github/scripts/bump_explain.py (k8scc#15), offline.

The three cells must each be able to be false on their own -- three fields that
always move together are one reading printed three times, and read as if more
had been checked (jgb-handler [20db54]). So the scenarios are the three real
bumps measured live on 2026-10-16, reduced to layer lists of the same shape:

  35f411b -> 3a23d9d   definition no,  20/22 replaced, base moved   (the issue's own case)
  fb98a11 -> e029956   definition yes,  6/23 replaced, base not moved
  d8d0fd3 -> 4e9aaaa   definition no,  amd64 11/23 and arm64 0/23,  base not moved

and every cell of every scenario is asserted, plus "not measured" paths, which
must never read as "no".

Run: python3 test/test_bump_explain.py
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
sys.dont_write_bytecode = True
import bump_explain as be  # noqa: E402

AMD, ARM = "linux/amd64", "linux/arm64"


def layers(prefix, n, l0):
    return [l0] + [f"sha256:{prefix}{i:02d}" for i in range(1, n)]


def swap(lst, k, tag):
    """the same list with the last k layers replaced"""
    return lst[:len(lst) - k] + [f"sha256:{tag}{i:02d}" for i in range(k)]


DEB_A, DEB_B, DEB_C = "sha256:a8ac7f6c67ab" + "0" * 52, "sha256:774043ccc8cc" + "0" * 52, "sha256:7fd1ebe418b5" + "0" * 52


def cells(text):
    rows = [r for r in text.splitlines() if r.startswith("| ") and not r.startswith("| |")]
    return {r.split("|")[1].strip(): r.split("|")[2].strip() for r in rows}


class BaseStageInputs(unittest.TestCase):
    def test_reads_the_real_dockerfile(self):
        from_image, sources = be.base_stage_inputs((ROOT / "Dockerfile").read_text())
        self.assertEqual(from_image, "debian:12-slim")
        for f in ("entrypoint.sh", "claude-session", "talos_mcp_server.py", "im-policy.md", "login-link-helper.js"):
            self.assertIn(f, sources)
        with self.subTest(control="builder and factory stages are not the base image's inputs"):
            self.assertFalse([s for s in sources if s.startswith("omni-machine-watch")], sources)
            self.assertFalse([s for s in sources if "/out/" in s], sources)

    def test_flags_and_destination_are_not_sources(self):
        _, sources = be.base_stage_inputs("FROM x AS base\nCOPY --chown=1:1 a b /dst/\nFROM base AS f\nCOPY c /d\n")
        self.assertEqual(sources, ["a", "b"])


class Render(unittest.TestCase):
    def run_case(self, changed, old, new, upstream):
        return cells(be.render("o", "n", changed, "", be.layers_replaced(old, new), "",
                               old[AMD][0], new[AMD][0], upstream, "debian:12-slim", ""))

    def test_issue_case_definition_unchanged_but_base_moved(self):
        old = {p: layers("old", 22, DEB_A) for p in (AMD, ARM)}
        new = {p: [DEB_B] + swap(old[p][1:], 19, "new") for p in (AMD, ARM)}
        c = self.run_case([], old, new, DEB_C)
        self.assertTrue(c["image definition changed?"].startswith("**no**"))
        self.assertIn("linux/amd64: **20/22** replaced", c["layers replaced (linux)"])
        self.assertTrue(c["base image moved?"].startswith("**yes**"))
        self.assertIn("upstream has moved again", c["base image moved?"])

    def test_definition_changed_few_layers_base_still(self):
        old = {p: layers("old", 23, DEB_C) for p in (AMD, ARM)}
        new = {p: swap(old[p], 6, "new") for p in (AMD, ARM)}
        c = self.run_case(["Dockerfile", "claude-session"], old, new, DEB_C)
        self.assertTrue(c["image definition changed?"].startswith("**yes** — 2 file(s)"))
        self.assertIn("linux/arm64: **6/23** replaced", c["layers replaced (linux)"])
        self.assertTrue(c["base image moved?"].startswith("**no**"))
        self.assertIn("(= new)", c["base image moved?"])

    def test_nothing_defined_changed_yet_one_platform_rebuilt(self):
        old = {p: layers("old", 23, DEB_C) for p in (AMD, ARM)}
        new = {AMD: swap(old[AMD], 11, "new"), ARM: list(old[ARM])}
        c = self.run_case([], old, new, DEB_C)
        self.assertIn("linux/amd64: **11/23**", c["layers replaced (linux)"])
        self.assertIn("linux/arm64: **0/23**", c["layers replaced (linux)"])

    def test_the_three_cells_move_independently(self):
        # Across the three real scenarios, each cell takes both values somewhere
        # and no two cells agree in every scenario.
        seen = {"image definition changed?": set(), "base image moved?": set()}
        rows = []
        for changed, moved, k in (([], True, 20), (["Dockerfile"], False, 6), ([], False, 11)):
            old = {p: layers("old", 23, DEB_A) for p in (AMD, ARM)}
            new = {p: [DEB_B if moved else DEB_A] + swap(old[p][1:], k - (1 if moved else 0), "n") for p in (AMD, ARM)}
            c = self.run_case(changed, old, new, None)
            d = c["image definition changed?"].startswith("**yes**")
            b = c["base image moved?"].startswith("**yes**")
            seen["image definition changed?"].add(d)
            seen["base image moved?"].add(b)
            rows.append((d, b, k))
        self.assertEqual(seen["image definition changed?"], {True, False})
        self.assertEqual(seen["base image moved?"], {True, False})
        self.assertNotEqual([r[0] for r in rows], [r[1] for r in rows])

    def test_unmeasured_is_never_no(self):
        c = cells(be.render("o", "n", None, "fatal: bad revision 'o'", {}, "GHCR: HTTP 404",
                            None, None, None, "debian:12-slim", "GHCR: HTTP 404"))
        for k, v in c.items():
            with self.subTest(cell=k):
                self.assertTrue(v.startswith("not measured ("), v)
                self.assertNotIn("**no**", v)
        with self.subTest(control="a missing platform is named, not counted as 0"):
            old = {AMD: layers("o", 5, DEB_C)}
            new = {AMD: layers("o", 5, DEB_C), ARM: layers("o", 5, DEB_C)}
            self.assertIsNone(be.layers_replaced(old, new)[ARM])


if __name__ == "__main__":
    unittest.main(verbosity=1)
