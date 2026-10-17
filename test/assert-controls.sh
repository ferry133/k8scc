#!/usr/bin/env bash
#
# Controls for the Dockerfile's assert() version check.
#
# assert() is what makes the factory build claim "the requested version is
# present" rather than "the install did not error". A check that cannot fail
# in the direction you care about reads exactly like one that passed, so this
# drives it against outputs recorded from real builds AND against pins that
# should be rejected.
#
# The function is extracted from the Dockerfile rather than copied here: a
# copy would keep passing after the original changed, which is the same
# failure this file exists to prevent.
#
# Run: test/assert-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail

cd "$(dirname "$0")/.."

fn=$(mktemp)
trap 'rm -f "$fn"' EXIT

python3 - "$fn" <<'PY'
import pathlib, re, sys

joined = re.sub(r"\\\n", "\n", pathlib.Path("Dockerfile").read_text())

match = re.search(r"(    assert\(\) \{.*?\n    \}; )", joined, re.S)
if not match:
    sys.exit("could not find assert() in the Dockerfile -- did the verify layer change shape?")

pathlib.Path(sys.argv[1]).write_text(match.group(1).rstrip().removesuffix(";") + "\n")
PY

records=$(mktemp)
trap 'rm -f "$fn" "$records"' EXIT

# shellcheck disable=SC1090
. "$fn"

emit() { printf '%s\n' "$1"; }

pass=0
fail=0

ok() {
  if assert "$1" "$2" emit "$3" >/dev/null 2>&1; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "  NOT ACCEPTED (should pass): $1 want=$2 out=$3"
  fi
}

no() {
  if assert "$1" "$2" emit "$3" >/dev/null 2>&1; then
    fail=$((fail + 1))
    echo "  NOT REJECTED (should fail): $1 want=$2 out=$3"
  else
    pass=$((pass + 1))
  fi
}

# Real output, copied from the build that produced ghcr.io/ferry133/claude-code
# factory-170830f. Update these when a tool changes how it reports itself --
# not when a pin moves.
echo "positive controls -- recorded tool output must be accepted"
ok omnictl     v1.8.1    'omnictl version v1.8.1 (API Version: 2)'
ok gh          2.87.3    'gh version 2.87.3 (2026-02-23)'
ok cloudflared 2026.2.0  'cloudflared version 2026.2.0 (built 2026-02-06-14:47 UTC)'
ok age         v1.3.1    'v1.3.1'
ok sops        v3.12.1   'sops 3.12.1'
ok cue         v0.15.4   'cue version v0.15.4'
ok task        v3.48.0   '3.48.0'
ok helm        v4.1.1    'v4.1.1+g5caf004'
ok helmfile    1.3.2     '  Version              1.3.2'
ok talhelper   v3.1.16   'talhelper version 3.1.16'
ok flux        2.8.1     'flux version 2.8.1'
ok kustomize   v5.7.1    'v5.7.1'
ok kubeconform v0.7.0    'v0.7.0'
ok yq          v4.52.4   'yq (https://github.com/mikefarah/yq/) version v4.52.4'
ok jq          1.8.1     'jq-1.8.1'
ok uv          0.10.7    'uv 0.10.7'
ok kubectl     v1.35.2   'Client Version: v1.35.2'
ok talosctl    v1.13.8   'Client: 	Tag:         v1.13.8 	SHA: 3de49322 	OS/Arch: linux/arm64'
ok omni-client v1.10.3   'omni-machine-watch (github.com/siderolabs/omni/client v1.10.3, go1.26.6)'
ok makejinja   2.8.2     '2.8.2'

echo "negative controls -- the pin moved but the binary did not"
no omnictl     v1.8.2    'omnictl version v1.8.1 (API Version: 2)'
no age         v1.3.11   'v1.3.1'
no age         v1.3.1    'v11.3.14'
no helm        v4.1.11   'v4.1.1+g5caf004'
no jq          1.8.11    'jq-1.8.1'
no talosctl    v1.13.2   'Client: 	Tag: v1.13.8'
no makejinja   2.8.2     '1.9.8'

# The shape a plain `grep -w` lets through: "." is not a word character, so
# an abbreviated pin matches inside the longer real version and the build
# reports success for a version nobody asked for.
echo "truncated-pin controls -- an abbreviated pin must not match a longer version"
no kubectl     v1.35     'Client Version: v1.35.2'
no makejinja   2.8       '2.8.2'
no task        v3.4      '3.48.0'

echo "failure controls -- a tool that cannot run is not a pass"
if assert broken 1.0.0 false >/dev/null 2>&1; then
  fail=$((fail + 1))
  echo "  NOT REJECTED: nonzero exit accepted"
else
  pass=$((pass + 1))
fi

if assert missing 1.0.0 /nonexistent-binary >/dev/null 2>&1; then
  fail=$((fail + 1))
  echo "  NOT REJECTED: missing binary accepted"
else
  pass=$((pass + 1))
fi

# known_hosts_match (base stage, im-apps-repo): the committed
# github-known-hosts must carry exactly the fingerprints pinned in the
# Dockerfile. Function AND pin are extracted, not copied; the committed file is
# the positive control, and each negative one is that file with one thing wrong.
echo "known_hosts controls -- the committed file passes, any altered copy fails"
kh_fn=$(mktemp); kh_dir=$(mktemp -d)
trap 'rm -rf "$fn" "$records" "$kh_fn" "$kh_dir"' EXIT
python3 - "$kh_fn" <<'PY'
import pathlib, re, sys
joined = re.sub(r"\\\n", "\n", pathlib.Path("Dockerfile").read_text())
fn = re.search(r"(    known_hosts_match\(\) \{.*?\n    \}; )", joined, re.S)
pin = re.search(r'^ARG GITHUB_HOSTKEY_FINGERPRINTS="([^"]+)"$', joined, re.M)
if not fn or not pin:
    sys.exit("could not find known_hosts_match() or its pin in the Dockerfile")
pathlib.Path(sys.argv[1]).write_text(
    fn.group(1).rstrip().removesuffix(";") + "\n" + f"KH_PIN='{pin.group(1)}'\n")
PY
# shellcheck disable=SC1090
. "$kh_fn"

kh_ok() {
  if known_hosts_match "$2" "$KH_PIN" >/dev/null 2>&1; then pass=$((pass + 1));
  else fail=$((fail + 1)); echo "  NOT ACCEPTED (should pass): $1"; fi
}
kh_no() {
  if known_hosts_match "$2" "$KH_PIN" >/dev/null 2>&1; then
    fail=$((fail + 1)); echo "  NOT REJECTED (should fail): $1"
  else pass=$((pass + 1)); fi
}

kh_ok "committed github-known-hosts" github-known-hosts
# One base64 character of the ed25519 key changed: still parses, other fingerprint.
sed '1s/AAAAIOMqqnkVzrm0S/AAAAIOMqqnkVzrm0T/' github-known-hosts > "$kh_dir/altered"
kh_no "one character altered" "$kh_dir/altered"
head -n 2 github-known-hosts > "$kh_dir/short"
kh_no "one key missing" "$kh_dir/short"
sed 's/^github\.com /gitlab.com /' github-known-hosts > "$kh_dir/otherhost"
kh_no "right keys, wrong host" "$kh_dir/otherhost"
: > "$kh_dir/empty"
kh_no "empty file" "$kh_dir/empty"
kh_no "missing file" "$kh_dir/nonexistent"
# The altered copy must really differ, or the control above proves nothing.
if cmp -s github-known-hosts "$kh_dir/altered"; then
  fail=$((fail + 1)); echo "  CONTROL BROKEN: altered copy is identical to the original"
else
  pass=$((pass + 1))
fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
