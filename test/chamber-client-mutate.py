#!/usr/bin/env python3
"""Mutations for test/test_chamber_client_server.py: break chamber/chamber one
way at a time and require the cells named for it to go red against the real
server. A mutation whose old text is not found exactly once is NOT APPLIED,
never a pass. Not run in CI (each mutation starts a server; about ten minutes
in all); run it by hand after changing the client or the cells:

    SYNAPSE_PYTHON=<python with matrix-synapse> python3 test/chamber-client-mutate.py

Ported from the operator side's chamber-mutate.py (2026-11-15)."""
import os, pathlib, subprocess, sys, tempfile
HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "chamber" / "chamber"
T = HERE / "test_chamber_client_server.py"
M = [
 ("cannot-measure folded into offline", '        return None\n    # \'unavailable\'', '        return "offline"\n    # \'unavailable\'', ["13"]),
 ("addressing filter removed", '    return bool(m.get("room")) or me in (m.get("user_ids") or [])', '    return True', ["5b", "8a"]),
 ("unreachable server reported as offline", 'fail("cannot reach the server (%s) while %s" % (data.get("error"), what), UNMEASURED)', 'fail("cannot reach the server (%s) while %s" % (data.get("error"), what), LEFT_OFFLINE)', ["11"]),
 ("wait trusts the initial sync position again", 'ev = [e for e in ev if e["event_id"] not in seen and e["origin_server_ts"] > newest]', 'ev = list(ev)', ["8a"]),
 ("recipient no longer required", '        if len(a) != 2 or not to:', '        if len(a) != 2:', ["3"]),
 ("offline never reported", '        if any(p == "offline" for p in states.values()):', '        if False:', ["4"]),
]
src = SRC.read_text(); bad = 0
only = os.environ.get('ONLY')
for name, old, new, expect in [m for m in M if not only or only in m[0]]:
    n = src.count(old)
    if n != 1:
        print("NOT APPLIED  %-48s (old text found %d times)" % (name, n), flush=True); bad += 1; continue
    f = os.path.join(tempfile.mkdtemp(), "chamber")
    with open(f, "w") as fh:
        fh.write(src.replace(old, new))
    p = subprocess.run([sys.executable, str(T)], env=dict(os.environ, CHAMBER_BIN=f), capture_output=True, text=True)
    red = [l.split()[1] for l in p.stdout.splitlines() if l.startswith("FAIL")]
    caught = p.returncode == 1 and all(e in red for e in expect)
    print("%s %-48s rc=%d red: %s (expected: %s)" % ("caught" if caught else "MISSED", name, p.returncode,
                                                     ",".join(red) or "none", ",".join(expect)), flush=True)
    bad += 0 if caught else 1
print("mutations not caught or not applied:", bad); sys.exit(1 if bad else 0)
