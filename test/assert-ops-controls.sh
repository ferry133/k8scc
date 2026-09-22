#!/usr/bin/env bash
#
# Controls for Dockerfile.ops's probe() and present() checks.
#
# probe() is what makes the ops image claim "this command runs on this
# platform" rather than "apk exited 0". present() is the deliberately weaker
# claim used where a build sandbox cannot make the stronger one. A check that
# cannot fail in the direction you care about reads exactly like one that
# passed, so both are driven here against inputs that must be accepted AND
# inputs that must be rejected.
#
# The functions are extracted from Dockerfile.ops rather than copied here: a
# copy would keep passing after the original changed, which is the same
# failure this file exists to prevent.
#
# Run: test/assert-ops-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail

cd "$(dirname "$0")/.."

fn=$(mktemp)
records=$(mktemp)
weak=$(mktemp)
trap 'rm -f "$fn" "$records" "$weak"' EXIT

python3 - "$fn" <<'PY'
import pathlib, re, sys

joined = re.sub(r"\\\n", "\n", pathlib.Path("Dockerfile.ops").read_text())

out = []
for name in ("probe", "present"):
    m = re.search(r"(    %s\(\) \{.*?\n    \}; )" % name, joined, re.S)
    if not m:
        sys.exit(f"could not find {name}() in Dockerfile.ops -- did the verify layer change shape?")
    out.append(m.group(1).rstrip().removesuffix(";"))

pathlib.Path(sys.argv[1]).write_text("\n".join(out) + "\n")
PY

# shellcheck disable=SC1090
. "$fn"

pass=0
fail=0

ok() {   # ok <label> <cmd...>
  label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "  NOT ACCEPTED (should pass): $label"
  fi
}

no() {   # no <label> <cmd...>
  label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    fail=$((fail + 1))
    echo "  NOT REJECTED (should fail): $label"
  else
    pass=$((pass + 1))
  fi
}

echo "probe positive controls -- a command that runs and reports must be accepted"
ok "prints a version-shaped line" probe v sh -c 'echo "tool 1.2.3"'
ok "prints on stderr only"        probe e sh -c 'echo "tool 1.2.3" >&2'
ok "multi-line output"            probe m sh -c 'printf "first\nsecond\n"'
ok "leading blank line"           probe b sh -c 'printf "\nreal line\n"'

echo "probe negative controls"
no "nonzero exit"                 probe x false
no "binary is not there"          probe x /nonexistent-binary-ops
no "exit 0, no output at all"     probe x true
no "exit 0, only an empty line"   probe x sh -c 'echo ""'
no "exit 0, only whitespace"      probe x sh -c 'printf "   \n\t\n"'
no "prints then fails"            probe x sh -c 'echo "tool 1.2.3"; exit 3'

# The distinction that makes present() safe to use at all: it is allowed to be
# weaker than probe(), but it is NOT allowed to accept something that is not
# there. If it did, the three applets it covers would read as verified.
echo "present controls -- weaker than probe, but still able to fail"
ok "a real executable on PATH"    present sh
no "a name that does not exist"   present definitely-not-a-command-xyz-ops
no "empty name"                   present ""

echo
echo "records written by probe():  $(sort -u "$records" | tr '\n' ' ')"
echo "names written by present():  $(sort -u "$weak" | tr '\n' ' ')"

# The manifest is built from these files, so a probe that passes without
# recording its name would ship a contract missing the command it verified.
if [ ! -s "$records" ] || [ ! -s "$weak" ]; then
  fail=$((fail + 1))
  echo "  NEITHER RECORDED: a passing check left no record for the manifest"
else
  pass=$((pass + 1))
fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
