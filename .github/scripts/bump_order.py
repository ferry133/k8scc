#!/usr/bin/env python3
"""May this build's pin replace the one already on bump/claudecode? (k8scc#31)

Every main build force-pushes its pin onto ONE jg-base branch (k8scc#15). Builds
of consecutive merges run side by side, and nothing ordered their pushes: an
older commit whose build finished last would put the older pin back -- and the
PR would read perfectly normal, just stale. 2026-10-15's three merges finished
in commit order by luck (runs 38813722919/38813767880/38813785669).

So before the push, compare the commit this build is for (NEW) with the commit
whose pin the branch already carries (ON_BRANCH), in k8scc's own history:

  absent   the branch has no pin yet (no branch, or it was merged and deleted)  -> push
  newer    ON_BRANCH is an ancestor of NEW                                      -> push
  same     NEW == ON_BRANCH (a re-run)                                          -> leave it
  older    NEW is an ancestor of ON_BRANCH                                      -> leave it
  unknown  anything else: unreadable pin, a commit this checkout does not have,
           histories that diverged                                              -> leave it, say why

"unknown" never pushes: folding "could not tell" into "newer" is the very race
this exists to stop, one level down.

The branch is not the only thing to beat. jg-base deletes bump/claudecode when a
bump PR merges, so the first build to finish after a merge sees "absent" -- and
if that build is older than what main now pins, pushing it opens a downgrade PR:
the same race, after the merge instead of before it (jg-base-0a [4e5e8d] on
k8scc#32: `"" 867d73e` read absent while main pinned 583a263). So NEW must also
be newer than jg-base main's pin (MAIN), which the job already reads; main
always has a pin, so an empty MAIN is unreadable, not absent. Push only when
main says newer AND the branch says absent or newer.

Run: python3 .github/scripts/bump_order.py ON_BRANCH_SHORT NEW_SHORT MAIN_SHORT
(in a k8scc checkout with full history; ON_BRANCH_SHORT may be "" for absent).
Prints the outcome word, a tab, and a reason. Exit 0 = push, 1 = leave it.
Controls: python3 test/test_bump_order.py
"""
from __future__ import annotations

import re
import subprocess
import sys

PUSH = {"absent", "newer"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _resolve(short: str) -> "str | None":
    r = _git("rev-parse", "--verify", "--quiet", f"{short}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def _is_ancestor(a: str, b: str) -> "bool | None":
    r = _git("merge-base", "--is-ancestor", a, b)
    return {0: True, 1: False}.get(r.returncode)   # anything else: git could not answer


def order(on_branch: str, new: str, where: str = "the branch") -> tuple[str, str]:
    if not on_branch:
        return "absent", "bump/claudecode carries no pin"
    if not re.fullmatch(r"[0-9a-f]{7,40}", on_branch):
        return "unknown", f"pin on {where} is not a commit id: {on_branch!r}"
    b, n = _resolve(on_branch), _resolve(new)
    if b is None or n is None:
        return "unknown", f"not in this checkout: {on_branch if b is None else new}"
    if b == n:
        return "same", f"{where} already carries {new}"
    fwd, back = _is_ancestor(b, n), _is_ancestor(n, b)
    if fwd is None or back is None:
        return "unknown", "git merge-base could not answer"
    if fwd:
        return "newer", f"{new} descends from {on_branch}"
    if back:
        return "older", f"{new} is an ancestor of {on_branch}, which {where} already carries"
    return "unknown", f"{new} and {on_branch} have diverged"


def decide(on_branch: str, new: str, main: str) -> tuple[str, str]:
    """Against jg-base main first, then against the branch. The verdict that
    stops the push is the one reported."""
    m_out, m_why = order(main or "<unreadable: empty>", new, "jg-base main")
    if m_out != "newer":
        return m_out, m_why
    return order(on_branch, new)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: bump_order.py ON_BRANCH_SHORT NEW_SHORT MAIN_SHORT")
    outcome, why = decide(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"{outcome}\t{why}")
    sys.exit(0 if outcome in PUSH else 1)
