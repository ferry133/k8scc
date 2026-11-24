#!/usr/bin/env bash
#
# Controls for .github/scripts/check_written_pin.sh (k8scc#130): the step of
# build.yaml that compares the pin jg-base's script wrote with the digest this
# build pushed. It had no control, and it failed every bump once another image
# in the same file got a digest.
#
# The fixture has the shape of jg-base's im/enabled/helmrelease.yaml as read
# on 2026-11-24: three claude-code sites and one oauth2-proxy site, all
# `tag: <something>@sha256:<64 hex>`.
#
# Run: test/bump-pin-check-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

pass=0; fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
S=.github/scripts/check_written_pin.sh

A=$(printf 'a%.0s' $(seq 64)); B=$(printf 'b%.0s' $(seq 64)); O=$(printf '0%.0s' $(seq 64))
SHA=abc1234
# file DIGEST1 DIGEST2 DIGEST3 [extra line] -> path
file() {
  local f="$tmp/hr-$RANDOM.yaml"
  {
    printf '          app:\n            image:\n              repository: ghcr.io/ferry133/claude-code\n              tag: %s@sha256:%s\n' "$SHA" "$1"
    printf '          init:\n            image:\n              repository: ghcr.io/ferry133/claude-code\n'
    [ -n "$2" ] && printf '              tag: %s@sha256:%s\n' "$SHA" "$2"
    printf '          talos-mcp:\n            image:\n              repository: ghcr.io/ferry133/claude-code\n              tag: %s@sha256:%s\n' "$SHA" "$3"
    printf '          oauth2-proxy:\n            image:\n              repository: quay.io/oauth2-proxy/oauth2-proxy\n              tag: v7.15.3@sha256:%s\n' "$O"
    [ -n "${4:-}" ] && printf '%s\n' "$4"
  } > "$f"
  echo "$f"
}
# expect RC NEEDLE DESC ARGS...
expect() {
  local want="$1" needle="$2" desc="$3" out rc; shift 3
  out=$(bash "$S" "$@" 2>&1); rc=$?
  if [ "$rc" -ne "$want" ]; then bad "$desc: expected rc=$want, got rc=$rc: $out"; return; fi
  if [ -n "$needle" ] && ! grep -qF -- "$needle" <<<"$out"; then bad "$desc: expected '$needle' in: $out"; return; fi
  ok
}

echo "the written pin against the built digest"
good=$(file "$A" "$A" "$A")
expect 0 "sites   : 3 pinned to $SHA" "three sites, one digest, the built one, beside another image's digest" "$good" "$SHA" "sha256:$A"
# the harness: the old way of reading this same file sees two digests
old=$(grep -oE 'sha256:[0-9a-f]{64}' "$good" | sort -u | wc -l | tr -d ' ')
if [ "$old" -eq 2 ]; then ok; else bad "control: the fixture should hold two distinct digests for the old reading, found $old"; fi
expect 1 "but this build pushed" "the sites agree with each other but not with the build" "$good" "$SHA" "sha256:$B"
expect 1 "disagree on the digest" "one site carries another digest" "$(file "$A" "$B" "$A")" "$SHA" "sha256:$A"
expect 1 "expected 3 sites pinned to $SHA, found 2" "one site was not rewritten" "$(file "$A" "" "$A")" "$SHA" "sha256:$A"
expect 1 "expected 3 sites pinned to $SHA, found 4" "a fourth site pinned to this commit" "$(file "$A" "$A" "$A" "              tag: $SHA@sha256:$A")" "$SHA" "sha256:$A"
expect 1 "expected 3 sites pinned to fff9999, found 0" "the file is pinned to another commit" "$good" fff9999 "sha256:$A"
expect 1 "no base digest" "an empty built digest is not compared" "$good" "$SHA" ""
expect 1 "is not a digest" "a built digest that is not one" "$good" "$SHA" "latest"
expect 1 "not a short sha" "a short sha that is not one" "$good" "abc" "sha256:$A"
expect 1 "no file to check" "a missing file" "$tmp/absent.yaml" "$SHA" "sha256:$A"
# a digest of the right commit inside a comment is not a site
expect 0 "sites   : 3" "a comment that mentions the pin is not a site" "$(file "$A" "$A" "$A" "              # tag: $SHA@sha256:$B")" "$SHA" "sha256:$A"

echo "build.yaml runs this file, and the controls run before the build"
if grep -qF '.github/scripts/check_written_pin.sh' .github/workflows/build.yaml; then ok; else bad "build.yaml does not call check_written_pin.sh"; fi
if grep -qE "grep -oE 'sha256:\[0-9a-f\]\{64\}' \"\\\$F\" \| sort -u" .github/workflows/build.yaml; then bad "build.yaml still reads every digest in the file"; else ok; fi
for w in controls.yaml build.yaml; do
  if grep -qF 'test/bump-pin-check-controls.sh' ".github/workflows/$w"; then ok; else bad "$w does not run these controls"; fi
done

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
