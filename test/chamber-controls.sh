#!/usr/bin/env bash
#
# Controls for the two chamber tools the image ships (openspec im-chamber-client
# tasks 2.1, k8scc#70): `chamber/chamber` and `chamber/directive-verify`. This
# repo holds the originals (ferry133, 2026-11-19); the operator's Mac carries
# installed copies.
#
# What this file checks is small on purpose: the files are shipped, they start,
# and the verifier tells its three results apart on one fixed vector. The full
# sets (20 client cells, 24 + 13 verifier cells, the mutations) are tasks 2.2
# and need a throwaway server.
#
# Three results, not two: a missing ssh-keygen is "cannot measure" and fails
# the run. It is never counted as a pass.
#
# Run: test/chamber-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

pass=0
fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local desc="$1"; shift; if "$@"; then ok; else bad "$desc"; fi; }
check_not() { local desc="$1"; shift; if "$@"; then bad "$desc"; else ok; fi; }
# rc EXPECTED DESC CMD...: the exit code must be exactly EXPECTED
rc() {
    local want="$1" desc="$2" got; shift 2
    "$@" >/dev/null 2>&1; got=$?
    if [ "$got" -eq "$want" ]; then ok; else bad "$desc (expected rc=$want, got rc=$got)"; fi
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
vec=test/chamber-vectors/sk-ecdsa
# -I: the tools must not pick up a module from the cwd. HOME is a temp dir so
# neither a real config nor a real ledger is read or written.
client()   { HOME="$tmp" python3 -I chamber/chamber "$@"; }
verifier() { HOME="$tmp" python3 -I chamber/directive-verify "$@"; }

echo "shipped"
for f in chamber directive-verify; do
    check "chamber/$f exists and is not empty"    test -s "chamber/$f"
    check "chamber/$f is executable in the tree"  test -x "chamber/$f"
    check "Dockerfile copies $f to /usr/local/bin" \
        grep -qE "^COPY chamber/$f /usr/local/bin/$f\$" Dockerfile
    check "Dockerfile makes $f executable" \
        grep -qE "^ +/usr/local/bin/$f( \\\\)?\$" Dockerfile
done
check_not "control: a file nobody ships" \
    grep -qE '^COPY chamber/nothing /usr/local/bin/nothing$' Dockerfile

echo "start"
rc 0 "chamber --help"            client --help
rc 0 "directive-verify --help"   verifier --help
printf 'import nothing_here\n' > "$tmp/broken"
rc 1 "control: a file that cannot start is seen" env HOME="$tmp" python3 -I "$tmp/broken"

echo "client: failed and cannot-measure are different answers"
echo x > "$tmp/tok"
rc 1 "no server configured ⇒ 1" env -u CHAMBER_URL HOME="$tmp" python3 -I chamber/chamber whoami
rc 2 "server unreachable ⇒ 2" \
    env HOME="$tmp" CHAMBER_URL=http://127.0.0.1:1 CHAMBER_TOKEN_FILE="$tmp/tok" \
        CHAMBER_ACCOUNT=t python3 -I chamber/chamber whoami

echo "verifier: one fixed sk-ecdsa vector, three results"
if ! command -v ssh-keygen >/dev/null 2>&1; then
    bad "CANNOT MEASURE: no ssh-keygen on this machine; the vector cells did not run"
else
    rc 0 "the vector as recorded ⇒ 0" \
        verifier --signers "$vec/allowed_signers" "$vec/directive.txt"
    # one byte of the person's words changed; the header and the signature
    # block are untouched, so the failure can only be the signature
    sed 's/^vector: a Secure/vector: a secure/' "$vec/directive.txt" > "$tmp/flipped"
    check_not "the flipped copy differs from the vector" cmp -s "$vec/directive.txt" "$tmp/flipped"
    rc 1 "one byte of the words changed ⇒ 1" \
        verifier --signers "$vec/allowed_signers" "$tmp/flipped"
    : > "$tmp/empty"
    rc 2 "an empty signer list ⇒ 2" verifier --signers "$tmp/empty" "$vec/directive.txt"
    rc 2 "no signer list ⇒ 2"       verifier --signers "$tmp/absent" "$vec/directive.txt"
    check_not "nothing was recorded as used (no --use was given)" \
        test -e "$tmp/.claude/chamber/used_directives"
fi

echo
echo "chamber controls: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
