#!/usr/bin/env bash
#
# Controls for the pane gate and chamber-watch against a REAL tmux (k8scc#95).
# The other watcher controls use a fake tmux, and a fake cannot show the one
# thing this issue is about: what a real tmux does with `send-keys -l` while
# the pane is in copy-mode. Measured on tmux 3.7b (2026-11-21): keys bound to a
# tmux prompt (emacs f g t, vi 1 : f t) make send-keys wait for that prompt,
# with a client attached, and the wait outlives copy-mode and the client.
#
# Its own server: TMUX_TMPDIR points into a temp directory, so the default
# socket is there, and nothing here can reach a tmux the person is running.
# The pane runs a copy of `cat` named `claude`, so tmux reports the pane's
# process as claude; it prints the idle prompt first.
#
# No tmux, or no `script` to attach a client with => FAIL (cannot measure),
# never a pass.
#
# Run: test/pane-gate-real-tmux.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."
repo=$(pwd)

pass=0 fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local d="$1"; shift; if "$@"; then ok; else bad "$d"; fi; }

for tool in tmux script timeout; do
    command -v "$tool" >/dev/null 2>&1 || { echo "FAIL CANNOT MEASURE: no $tool on this machine"; exit 1; }
done

work=$(mktemp -d)
export TMUX_TMPDIR="$work/sock"; mkdir -p "$TMUX_TMPDIR"
unset TMUX
cpid=""
cleanup() { [ -n "$cpid" ] && kill "$cpid" 2>/dev/null; tmux kill-server 2>/dev/null; rm -rf "$work"; }
trap cleanup EXIT
mkdir -p "$work/bin"
# The pane's process must be one tmux names `claude`, because chamber-watch
# checks #{pane_current_command}. Two candidates, and the one tmux really names
# claude is used (tmux reads the name differently per OS):
#  (a) a copy of cat called claude. macOS kills a copied system binary until it
#      is re-signed (measured 2026-11-21: rc 137); on a runner it did not run
#      at all (run 44909733610, tmux 3.6; a multi-call coreutils is a guess).
#  (b) bash reading stdin, started as `exec -a claude`; on macOS tmux names it
#      bash (measured), on Linux it reads argv[0] (read, not measured).
cp "$(command -v cat)" "$work/bin/claude-a"
if [ "$(uname)" = Darwin ]; then codesign --force -s - "$work/bin/claude-a" 2>/dev/null; fi
mv "$work/bin/claude-a" "$work/bin/claude"
printf '%s\n' '#!/usr/bin/env bash' "exec -a claude bash -c 'while IFS= read -r _; do :; done'" > "$work/bin/claude-b"
chmod +x "$work/bin/claude-b"
standin=""; readings=""
for c in claude claude-b; do
    tmux -f /dev/null new-session -d -s probe "exec '$work/bin/$c'" 2>/dev/null; sleep 0.5
    r=$(tmux display -p -t =probe: '#{pane_current_command}' 2>&1); tmux kill-server 2>/dev/null; sleep 0.3
    readings+=" $c:'${r}'"
    [ "$r" = claude ] && { standin="$work/bin/$c"; break; }
done
[ -n "$standin" ] || { echo "FAIL CANNOT MEASURE: no stand-in that tmux names claude (read:${readings}; cat run: $(echo | "$work/bin/claude" 2>&1 | head -c 200))"; exit 1; }
echo "stand-in: ${standin##*/} (tmux read:${readings})"

# the rule-drawn idle prompt, as in test/pane-fixtures.sh (IDLE_RULE)
printf '%s\n' '● Restart applied.' '────────────────────────────────────────────' '❯ ' \
    '────────────────────────────────────────────' '  ⏵⏵ auto mode on (shift+tab to cycle)' > "$work/idle"

start_server() {
    tmux kill-server 2>/dev/null; sleep 0.3
    tmux -f /dev/null new-session -d -s claude -x 200 -y 50 "cat '$work/idle'; exec '$standin'"
    tmux set -g mode-keys "$1"
    sleep 0.5
}
# attach a client on a pty, as ttyd does; it types nothing
attach() {
    # TERM is set here, not inherited: a CI runner has none (or dumb), and tmux
    # then refuses the client ("open terminal failed: terminal does not support
    # clear"; reproduced with TERM=dumb on macOS, 2026-11-21; run 44910120765)
    if script --version 2>/dev/null | grep -q util-linux; then
        { sleep 120 | env TERM=xterm-256color script -q -c "tmux attach -t claude" /dev/null; } >"$work/attach.log" 2>&1 &
    else
        { sleep 120 | env TERM=xterm-256color script -q /dev/null tmux attach -t claude; } >"$work/attach.log" 2>&1 &
    fi
    cpid=$!
    local i; for i in 1 2 3 4 5 6 7 8 9 10; do
        [ "$(tmux display -p -t =claude: '#{session_attached}' 2>/dev/null)" = 1 ] && return 0; sleep 0.3
    done
    # every cell after this one needs the client, so this is "cannot measure",
    # not one FAIL among passes
    echo "FAIL CANNOT MEASURE: no client attached ($(tr -d '\r' < "$work/attach.log" | LC_ALL=C tr -c '[:print:]' ' ' | head -c 300))"
    exit 1
}
detach() { [ -n "$cpid" ] && kill "$cpid" 2>/dev/null; cpid=""; tmux detach-client -s claude 2>/dev/null; sleep 0.3; }

# the line chamber-watch types, as it would type it
line='💬 chamber: 1 message(s) for you in cust-a — run `chamber read cust-a` and handle them per your policy'

# gate ARGS : run one pane-gate function in a fresh shell, print its answer
gate() { WATCH_TMUX_TIMEOUT=2 bash -c 'session=claude; . "$1/pane-gate"; shift; "$@"; echo "rc=$?"' _ "$repo" "$@"; }

for keys in emacs vi; do
    echo "the gate on a real tmux ($keys copy-mode keys)"
    start_server "$keys"
    attach
    check "$keys: positive control: the real capture of the idle prompt passes the gate" [ "$(gate pane_idle)" = "rc=0" ]
    tmux copy-mode -t =claude:
    check "$keys: in copy-mode the gate says so" [ "$(gate pane_idle | tr '\n' ' ')" = "pane in a mode rc=1 " ]
    check "$keys: … while the screen underneath still reads idle (why the mode is checked)" [ "$(gate pane_screen)" = "rc=0" ]
    # the mechanism itself: what the watcher would have done without the check
    t0=$(date +%s); gate tmux_t send-keys -t =claude: -l "$line" > "$work/out"; t1=$(date +%s)
    check "$keys: the line typed into copy-mode does not return; the limit stops it (rc 124, $((t1 - t0))s)" grep -qx 'rc=124' "$work/out"
    tmux send-keys -t =claude: -X cancel
    check "$keys: after leaving copy-mode the gate passes again" [ "$(gate pane_idle)" = "rc=0" ]
    check "$keys: … and a send returns at once (tmux recovered)" [ "$(gate tmux_t send-keys -t =claude: -l x)" = "rc=0" ]
    detach
done

echo "chamber-watch on a real tmux"
# a fake bridge: answers the beat, one waiting message, records what is reported
cat > "$work/bin/curl" <<'EOF'
#!/usr/bin/env bash
method=GET; url=; body=
while [ $# -gt 0 ]; do
  case "$1" in -X) method="$2"; shift ;; --data-binary) body="$2"; shift ;; --unix-socket|--max-time) shift ;; http://*) url="$1" ;; esac
  shift
done
printf '%s %s\n' "$method" "$url" >> "$FAKE_DIR/curl.log"
case "$method $url" in
  "POST http://bridge/bridge/beat") echo deliver ;;
  "GET http://bridge/bridge/pending") [ -s "$FAKE_DIR/announced" ] || printf '1 cust-a\n' ;;
  "POST http://bridge/bridge/announced") printf '%s' "$body" >> "$FAKE_DIR/announced" ;;
  *) exit 22 ;;
esac
EOF
chmod +x "$work/bin/curl"
mkdir -p "$work/fake"; : > "$work/fake/curl.log"; : > "$work/fake/announced"
watch() {
    env PATH="$work/bin:$PATH" FAKE_DIR="$work/fake" CHAMBER_URL="unix:$work/bridge.sock" CHAMBER_BEAT=1 \
        CHAMBER_WATCH_TICKS="$1" CHAMBER_WATCH_SETTLE=0 CHAMBER_SIGNERS_DIR="$work/none" WATCH_TMUX_TIMEOUT=3 \
        bash chamber/chamber-watch 2>>"$work/watch.err"
}
typed() { tmux capture-pane -p -t =claude: -S - | grep -c 'chamber read cust-a'; }
start_server emacs
attach
tmux copy-mode -t =claude:
t0=$(date +%s); watch 3; t1=$(date +%s)
check "copy-mode, a message waiting: three beats in $((t1 - t0))s, none of them stuck" [ $((t1 - t0)) -lt 8 ]
check "… the alive signal on every beat (acceptance b)" [ "$(grep -c '^POST http://bridge/bridge/beat' "$work/fake/curl.log")" = 3 ]
check "… nothing typed (acceptance a)" [ "$(typed)" = 0 ]
check "… nothing reported" [ ! -s "$work/fake/announced" ]
check "… the log says why" grep -q 'messages waiting, skipped (pane not idle: pane in a mode)' "$work/watch.err"
check "… the pane is still in copy-mode, untouched" [ "$(tmux display -p -t =claude: '#{pane_in_mode}')" = 1 ]
tmux send-keys -t =claude: -X cancel
watch 1
check "after leaving copy-mode: the line is in the pane (acceptance c)" [ "$(typed)" -ge 1 ]
check "… and reported" grep -qx '1 cust-a' "$work/fake/announced"

echo "pane-gate on a real tmux: passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
