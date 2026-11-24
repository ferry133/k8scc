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

echo "session set-up at pod start (k8scc#122)"
sfn="$tmp/setup-fn.sh"
sed -n '/^# --- session set-up at pod start/,/^# --- end session set-up at pod start/p' entrypoint.sh > "$sfn"
if ! grep -q '^session_setup()' "$sfn"; then
  bad "could not extract session_setup from entrypoint.sh"
elif ! command -v timeout >/dev/null 2>&1; then
  bad "CANNOT MEASURE: no timeout(1) on this machine; the session set-up cells did not run"
else
  tbin=$(dirname "$(command -v timeout)")
  mkdir -p "$tmp/sbin"
  cat > "$tmp/sbin/claude-session" <<'SH'
#!/bin/sh
echo "SETUP_ONLY=${CLAUDE_SESSION_SETUP_ONLY:-<unset>}" > "$FAKE_SEEN"
case "${FAKE_SETUP:-ok}" in
  ok)   exit 0 ;;
  warn) echo "⚠ settings.json 的 netdev permission 設定沒有寫入"; exit 0 ;;
  fail) echo "boom: cannot write"; exit 3 ;;
  hang) sleep 30 ;;
  deaf) trap '' TERM; sleep 30 & wait; sleep 30 ;;
  stdin) if read -r -t 1 line; then echo "read a line from stdin"; exit 4; fi; exit 0 ;;
esac
SH
  chmod +x "$tmp/sbin/claude-session"
  # srun MODE [PATH] [TIMEOUT]
  srun() {
    rm -f "$tmp/seen"
    env -i HOME="$tmp" PATH="${2:-$tmp/sbin:$tbin:/usr/bin:/bin}" FAKE_SETUP="$1" FAKE_SEEN="$tmp/seen" SESSION_SETUP_TIMEOUT="${3:-20}" \
      bash -c 'set -e; . "$0"; session_setup; echo survived' "$sfn" 2>&1
  }
  o=$(srun ok)
  has "$o" "session set-up: done at pod start" "ok"
  has "$o" "survived" "ok"
  if [ "$(cat "$tmp/seen" 2>/dev/null)" = "SETUP_ONLY=1" ]; then ok; else bad "claude-session was not run with CLAUDE_SESSION_SETUP_ONLY=1: $(cat "$tmp/seen" 2>/dev/null)"; fi
  o=$(srun warn)
  has "$o" "entrypoint:   | ⚠ settings.json 的 netdev permission 設定沒有寫入" "warn: its own words reach the pod log"
  o=$(srun fail)
  has "$o" "session set-up: FAILED (exit 3)" "fail"
  has "$o" "entrypoint:   | boom: cannot write" "fail: its own words"
  has "$o" "survived" "fail: the container goes on"
  hasnt "$o" "done at pod start" "fail is not read as done"
  o=$(srun hang "" 1)
  has "$o" "NOT finished within 1s" "hang"
  has "$o" "survived" "hang: the container goes on"
  hasnt "$o" "done at pod start" "a timeout is not read as done"
  # a set-up that ignores TERM is killed, not waited for (fleet-ops-1e on #125)
  t0=$(date +%s); o=$(srun deaf "" 1); took=$(( $(date +%s) - t0 ))
  has "$o" "NOT finished within 1s" "deaf to TERM"
  has "$o" "survived" "deaf to TERM: the container goes on"
  if [ "$took" -le 15 ]; then ok; else bad "deaf to TERM: took ${took}s, the kill did not come"; fi
  # its stdin is not the entrypoint's: with a line waiting there, it reads none
  o=$(echo "a line on the entrypoint's stdin" | srun stdin)
  has "$o" "session set-up: done at pod start" "stdin is /dev/null"
  hasnt "$o" "read a line from stdin" "stdin is /dev/null"
  o=$(srun ok "$tbin:/usr/bin:/bin")
  has "$o" "claude-session is not on PATH" "absent"
  has "$o" "survived" "absent"
  hasnt "$o" "done at pod start" "absent is not read as done"
  if [ ! -e "$tmp/seen" ]; then ok; else bad "absent: something still ran"; fi

  # the default limit stays well under the liveness probe's ~75 s (see entrypoint.sh)
  dflt=$(sed -n 's/.*SESSION_SETUP_TIMEOUT:-\([0-9]*\)}.*/\1/p' "$sfn" | head -n 1)
  if [ -n "$dflt" ] && [ "$dflt" -ge 10 ] && [ "$dflt" -le 45 ]; then ok; else bad "the default SESSION_SETUP_TIMEOUT is '${dflt}', not between 10 and 45"; fi

  # where it is called: once, before ttyd and before every watcher
  at()  { grep -n -m1 -E "$1" entrypoint.sh | cut -d: -f1; }
  call=$(at '^session_setup$')
  if [ -n "$call" ]; then ok; else bad "entrypoint.sh never calls session_setup"; call=0; fi
  for later in '^[[:space:]]*chamber-watch &' '^[[:space:]]*session-keep &' '^exec ttyd '; do
    l=$(at "$later")
    if [ -n "$l" ] && [ "$call" -gt 0 ] && [ "$call" -lt "$l" ]; then ok; else bad "session_setup (line $call) is not before '$later' (line ${l:-none})"; fi
  done

  # the real claude-session behind it: the profile is on disk with no session
  mkdir -p "$tmp/rbin" "$tmp/rhome"
  ln -s "$PWD/claude-session" "$tmp/rbin/claude-session"
  printf '#!/bin/sh\necho "claude $*" >> "$FAKE_LOG"\n[ "$1 $2" = "mcp get" ] && exit 1\nexit 0\n' > "$tmp/rbin/claude"
  printf '#!/bin/sh\necho "tmux $*" >> "$FAKE_LOG"\nexit 0\n' > "$tmp/rbin/tmux"
  chmod +x "$tmp/rbin/claude" "$tmp/rbin/tmux"
  : > "$tmp/real.log"
  rrun() { env -i HOME="$tmp/rhome" PATH="$tmp/rbin:$tbin:$PATH" FAKE_LOG="$tmp/real.log" "$@" bash -c 'set -e; . "$0"; session_setup; echo survived' "$sfn" 2>&1; }
  o=$(rrun NETDEV_MCP_URL=http://127.0.0.1:8766/sse)
  has "$o" "session set-up: done at pod start" "real claude-session"
  if grep -qF '"Bash(lan-inventory-check)"' "$tmp/rhome/.claude/settings.json" 2>/dev/null; then ok; else bad "real claude-session: the rule is not in settings.json after pod start"; fi
  if grep -q '^tmux ' "$tmp/real.log"; then bad "real claude-session: a session was started at pod start"; else ok; fi
  rm -rf "$tmp/rhome"; mkdir -p "$tmp/rhome"
  o=$(rrun NETDEV_MCP_URL=)
  if grep -q 'lan-inventory-check' "$tmp/rhome/.claude/settings.json" 2>/dev/null; then bad "control: the rule is written without the sidecar env"; else ok; fi
fi

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
