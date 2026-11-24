#!/usr/bin/env bash
#
# check_written_pin.sh FILE SHORT_SHA BUILT_DIGEST
#
# After jg-base's bump script rewrote FILE: the three sites pinned to this
# commit must all carry one digest, and it must be the one this build pushed.
# The script resolves the digest by paging GHCR; the build knows it first-hand.
# Two sources agreeing is a measurement.
#
# Only the lines `tag: SHORT_SHA@sha256:…` are read. The first version took
# every `sha256:…` in the file, which was the same thing while claude-code was
# the only image pinned by digest there, and failed every bump from the day
# jg-base pinned oauth2-proxy in the same file (k8scc#130; five builds without
# a bump PR, behind jg-base#269, which had the same shape).
#
# 0 = three sites, one digest, equal to BUILT_DIGEST. 1 = anything else, said
# on stderr as a GitHub `::error::` line. test/bump-pin-check-controls.sh runs
# this file.
set -euo pipefail

F="${1:-}"; SHA7="${2:-}"; BUILT="${3:-}"
fail() { echo "::error::$*" >&2; exit 1; }

[ -n "$F" ] && [ -f "$F" ] || fail "no file to check: '${F}'"
[[ "$SHA7" =~ ^[0-9a-f]{7}$ ]] || fail "not a short sha: '${SHA7}'"
# Two empty values compare equal and prove nothing. The gate's skip path is
# exactly where this one could arrive empty.
[ -n "$BUILT" ] || fail "no base digest: neither a fresh push nor the gate's already-published lookup produced one"
[[ "$BUILT" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "the built digest is not a digest: '${BUILT}'"

SITES=$(grep -E "^[[:space:]]*tag: ${SHA7}@sha256:[0-9a-f]{64}[[:space:]]*$" "$F" || true)
N=$(printf '%s' "$SITES" | grep -c . || true)
WRITTEN=$(printf '%s\n' "$SITES" | grep -oE 'sha256:[0-9a-f]{64}' | sort -u || true)
echo "sites   : ${N} pinned to ${SHA7}"
echo "written : ${WRITTEN}"
echo "built   : ${BUILT}"
[ "$N" -eq 3 ] || fail "expected 3 sites pinned to ${SHA7}, found ${N}"
[ "$(printf '%s\n' "$WRITTEN" | wc -l | tr -d ' ')" -eq 1 ] || fail "the three sites disagree on the digest"
[ "$WRITTEN" = "$BUILT" ] || fail "GHCR resolved ${WRITTEN} but this build pushed ${BUILT}"
