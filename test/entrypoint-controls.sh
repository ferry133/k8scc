#!/usr/bin/env bash
#
# Controls for entrypoint.sh's secret-service startup (k8scc#19): the D-Bus
# session bus and GNOME Keyring each leave one `entrypoint:` line whatever
# happens, the lines for "came up" and "did not" differ, and no outcome stops
# the container. The function is EXTRACTED from entrypoint.sh between its
# markers, not copied, so these cannot keep passing after the original changes.
# It runs under `set -e`, as in the entrypoint, with fake daemons first on PATH.
#
# Run: test/entrypoint-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

pass=0
fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fn="$tmp/fn.sh"
sed -n '/^# --- secret-service startup/,/^# --- end secret-service startup/p' entrypoint.sh > "$fn"
grep -q '^start_secret_service()' "$fn" || { echo "FAIL: could not extract start_secret_service from entrypoint.sh"; exit 1; }

# Fake daemons, steered by env: FAKE_DBUS / FAKE_KEYRING = ok | fail | silent.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/dbus-daemon" <<'SH'
#!/bin/sh
case "${FAKE_DBUS:-ok}" in
  ok)     echo "unix:path=/tmp/fake-bus,guid=0123" ;;
  fail)   echo "Failed to start message bus: no such file" >&2; exit 1 ;;
  silent) exit 0 ;;
esac
SH
cat > "$tmp/bin/gnome-keyring-daemon" <<'SH'
#!/bin/sh
cat >/dev/null
case "${FAKE_KEYRING:-ok}" in
  ok)     echo "GNOME_KEYRING_CONTROL=/run/user/1000/keyring"; echo "SSH_AUTH_SOCK=/run/user/1000/keyring/ssh" ;;
  fail)   echo "gnome-keyring-daemon: couldn't unlock: no D-Bus" >&2; exit 1 ;;
  silent) exit 0 ;;
esac
SH
chmod +x "$tmp/bin/"*

# run DBUS KEYRING [PATH] -> stdout+stderr of a set -e shell that sources the
# function, calls it, prints the exported env, then `survived`.
run() {
  env -i HOME="$tmp" PATH="${3:-$tmp/bin:/usr/bin:/bin}" FAKE_DBUS="$1" FAKE_KEYRING="$2" \
    bash -c 'set -e; . "$0"; start_secret_service; echo "env: DBUS=${DBUS_SESSION_BUS_ADDRESS:-<unset>} CONTROL=${GNOME_KEYRING_CONTROL:-<unset>}"; echo survived' "$fn" 2>&1
}
has()    { if grep -qF -- "$2" <<<"$1"; then ok; else bad "$3: expected '$2' in: $1"; fi; }
hasnt()  { if grep -qF -- "$2" <<<"$1"; then bad "$3: did not expect '$2' in: $1"; else ok; fi; }

echo "both up"
up=$(run ok ok)
has   "$up" "entrypoint: dbus: session bus up at unix:path=/tmp/fake-bus" "dbus up line"
has   "$up" "entrypoint: keyring: secrets component up (GNOME_KEYRING_CONTROL=/run/user/1000/keyring)" "keyring up line"
has   "$up" "env: DBUS=unix:path=/tmp/fake-bus,guid=0123 CONTROL=/run/user/1000/keyring" "both exported"
hasnt "$up" "NOT started" "control: nothing reported as failed"
has   "$up" "survived" "container continues"

echo "keyring fails"
kf=$(run ok fail)
has   "$kf" "entrypoint: keyring: NOT started (exit 1, printed nothing; stderr: gnome-keyring-daemon: couldn't unlock: no D-Bus" "keyring failure carries rc and stderr"
hasnt "$kf" "secrets component up" "control: no up line on failure"
has   "$kf" "CONTROL=<unset>" "nothing exported on failure"
has   "$kf" "survived" "keyring failure does not stop the container"

echo "keyring exits 0 but prints nothing (the old eval-of-nothing case)"
ks=$(run ok silent)
has   "$ks" "entrypoint: keyring: NOT started (exit 0, printed nothing" "silent success is not reported as up"
has   "$ks" "survived" "container continues"

echo "dbus fails"
df=$(run fail ok)
has   "$df" "entrypoint: dbus: session bus NOT started (exit 1, printed no address; stderr: Failed to start message bus" "dbus failure carries rc and stderr"
has   "$df" "keyring: up, but there is no session bus" "keyring says secret-service is unreachable without a bus"
hasnt "$up" "no session bus" "control: that warning is absent when the bus is up"
has   "$df" "survived" "dbus failure does not stop the container"

echo "dbus exits 0 with no address"
ds=$(run silent ok)
has   "$ds" "entrypoint: dbus: session bus NOT started (exit 0, printed no address" "silent dbus is not reported as up"

echo "neither installed"
none=$(run ok ok "/usr/bin:/bin")
if command -v dbus-daemon >/dev/null 2>&1 || command -v gnome-keyring-daemon >/dev/null 2>&1; then
  echo "  (skipped: a real daemon is on /usr/bin:/bin here)"
else
  has "$none" "entrypoint: dbus: dbus-daemon not installed" "missing dbus named"
  has "$none" "entrypoint: keyring: gnome-keyring-daemon not installed" "missing keyring named"
  has "$none" "survived" "container continues"
fi

echo "the five outcomes leave five different logs"
# Two outcomes reading the same is exactly what this issue was about.
logs=$(for o in "$up" "$kf" "$ks" "$df" "$ds"; do grep '^entrypoint:' <<<"$o" | tr '\n' '|'; echo; done | sort -u | wc -l | tr -d ' ')
if [ "$logs" -eq 5 ]; then ok; else bad "expected 5 distinct logs, got $logs"; fi

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
