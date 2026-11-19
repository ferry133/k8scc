#!/usr/bin/env bash
#
# Controls for session-keep (openspec im-chamber-client tasks 4.3, k8scc#70):
# run AS SHIPPED, and it runs the real claude-session, with fake `tmux`,
# `claude` and `sleep` ahead on PATH. The fake tmux keeps "the session exists"
# in a file, so a session can die between two rounds of one process.
#
# What these cannot show: whether a detached `claude` reaches its prompt.
# That is a pod reading (tasks 1.2), and when it does not, it is chamber-watch
# that says so, not this script.
#
# Run: test/session-keep-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."
repo=$PWD

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
ln -s "$repo/claude-session" "$work/bin/claude-session"

cat > "$work/bin/tmux" <<'FAKE'
#!/usr/bin/env bash
# fake tmux: the session exists iff $FAKE_DIR/session exists. `new-session`
# creates it unless $FAKE_DIR/nostart exists (then it fails, like a tmux that
# cannot start its command).
printf 'tmux %s\n' "$*" >> "$FAKE_DIR/tmux.log"
case "$1" in
  has-session) [ -f "$FAKE_DIR/session" ] ;;
  -u) if [ "$2" = new-session ]; then
        [ -f "$FAKE_DIR/nostart" ] && { echo "fake tmux: cannot start" >&2; exit 1; }
        touch "$FAKE_DIR/session"
      fi ;;
esac
FAKE
cat > "$work/bin/claude" <<'FAKE'
#!/usr/bin/env bash
printf 'claude %s\n' "$*" >> "$FAKE_DIR/claude.log"
case "${1:-} ${2:-}" in "mcp get") exit 1 ;; esac
exit 0
FAKE
cat > "$work/bin/sleep" <<'FAKE'
#!/usr/bin/env bash
n=$(( $(cat "$FAKE_DIR/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_DIR/sleeps"
[ -f "$FAKE_DIR/hook.$n" ] && bash "$FAKE_DIR/hook.$n"
exit 0
FAKE
chmod +x "$work/bin/tmux" "$work/bin/claude" "$work/bin/sleep"

pass=0 fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local d="$1"; shift; if "$@"; then ok; else bad "$d"; fi; }
check_not() { local d="$1"; shift; if "$@"; then bad "$d"; else ok; fi; }

setup() {
  c="$work/$1"; mkdir -p "$c/home" "$c/fake"
  : > "$c/fake/tmux.log"; : > "$c/fake/claude.log"
}
# run <case> [ENV=VALUE…]
run() {
  local c="$work/$1"; shift
  env -i PATH="$work/bin:$PATH" HOME="$c/home" FAKE_DIR="$c/fake" CLAUDE_USER_ID=tester \
      SESSION_KEEP=1 SESSION_KEEP_INTERVAL=1 SESSION_KEEP_TICKS=1 "$@" \
      bash ./session-keep 2>"$c/stderr"
}
starts() { grep -c '^tmux -u new-session ' "$work/$1/fake/tmux.log"; }

echo "off unless SESSION_KEEP=1"
for v in "" 0 true yes; do
  setup "off-$v"; run "off-$v" SESSION_KEEP="$v"
  check "SESSION_KEEP='$v': says it is off"            grep -q '^session-keep: SESSION_KEEP is not 1, off' "$work/off-$v/stderr"
  check "SESSION_KEEP='$v': tmux and claude were asked nothing" [ ! -s "$work/off-$v/fake/tmux.log" -a ! -s "$work/off-$v/fake/claude.log" ]
done

echo "no session: one is started, detached, the way the terminal starts it"
setup none; run none TALOS_MCP_URL=http://127.0.0.1:8765/sse
check "one session started"                             [ "$(starts none)" = 1 ]
check "detached, with a size, running claude"           grep -qx 'tmux -u new-session -d -s claude -x 200 -y 50 -- claude' "$work/none/fake/tmux.log"
check_not "not the attaching form (there is no terminal to attach)" grep -q 'new-session -A' "$work/none/fake/tmux.log"
check "claude-session's set-up ran first (the MCP server is registered)" grep -q '^claude mcp add --transport sse --scope local talos ' "$work/none/fake/claude.log"
check "… and the permission profile was written"        grep -q 'mcp__talos__reboot_node' "$work/none/home/.claude/settings.json"
check "the log says a session was started"              grep -q '^session-keep: no session, started one (detached)' "$work/none/stderr"

echo "a session exists: it is left alone"
# TALOS_MCP_URL is set so that a run of claude-session would leave a trace
# (`claude mcp get talos`); without it this cell could not tell "not run" from
# "ran and had nothing to register" -- a mutant survived on exactly that
setup exists; touch "$work/exists/fake/session"; run exists SESSION_KEEP_TICKS=3 TALOS_MCP_URL=http://127.0.0.1:8765/sse
check "nothing started over three rounds"               [ "$(starts exists)" = 0 ]
check "claude-session was not run at all"               [ ! -s "$work/exists/fake/claude.log" ]
check_not "the log does not claim a start"              grep -q 'started one' "$work/exists/stderr"
check "tmux was only asked whether it exists"           [ "$(grep -vc '^tmux has-session ' "$work/exists/fake/tmux.log")" = 0 ]

echo "the session goes away: it is started again"
setup dies; echo 'rm -f "$FAKE_DIR/session"' > "$work/dies/fake/hook.1"; run dies SESSION_KEEP_TICKS=3
check "started, gone after the first round, started again: two starts" [ "$(starts dies)" = 2 ]
setup stays; run stays SESSION_KEEP_TICKS=3
check "control: a session that stays is started once"   [ "$(starts stays)" = 1 ]

echo "a session that cannot be started is said, once"
setup cannot; touch "$work/cannot/fake/nostart"; run cannot SESSION_KEEP_TICKS=3
check "three rounds, three attempts"                    [ "$(starts cannot)" = 3 ]
check "one line in the log, not three"                  [ "$(grep -c '^session-keep: could not start a session' "$work/cannot/stderr")" = 1 ]
check "… with what tmux said"                           grep -q 'session-keep:   | fake tmux: cannot start' "$work/cannot/stderr"
check_not "… and never 'started one'"                   grep -q 'started one' "$work/cannot/stderr"
setup recovers; touch "$work/recovers/fake/nostart"; echo 'rm -f "$FAKE_DIR/nostart"' > "$work/recovers/fake/hook.1"; run recovers SESSION_KEEP_TICKS=2
check "control: once it can start, it does, and says so" grep -q 'started one (detached)' "$work/recovers/stderr"

echo "claude-session, detached"
setup det; mkdir -p "$work/det/home/workspace/inbox"; touch "$work/det/home/workspace/inbox/a.md"
t0=$(date +%s)
out=$(env -i PATH="$work/bin:$PATH" HOME="$work/det/home" FAKE_DIR="$work/det/fake" CLAUDE_USER_ID=tester CLAUDE_SESSION_DETACHED=1 bash ./claude-session </dev/null 2>&1)
t1=$(date +%s)
check_not "no greeting"                                 grep -q '歡迎' <<<"$out"
check_not "no inbox line (nobody is there to read it; inbox-watch tells the session)" grep -q 'inbox' <<<"$out"
check "no 8-second pause"                               [ $((t1 - t0)) -lt 6 ]
out=$(env -i PATH="$work/bin:$PATH" HOME="$work/det/home" FAKE_DIR="$work/det/fake" CLAUDE_USER_ID=tester bash ./claude-session </dev/null 2>&1)
check "control: at a terminal the greeting and the inbox line are there" bash -c 'grep -q "歡迎" <<<"$1" && grep -q "inbox" <<<"$1"' _ "$out"
check "control: … and the terminal attaches (-A), as before" grep -qx 'tmux -u new-session -A -s claude -- claude' "$work/det/fake/tmux.log"
setup det2; touch "$work/det2/fake/session"
env -i PATH="$work/bin:$PATH" HOME="$work/det2/home" FAKE_DIR="$work/det2/fake" CLAUDE_USER_ID=tester CLAUDE_SESSION_DETACHED=1 bash ./claude-session </dev/null >/dev/null 2>&1
check "detached, and a session appeared meanwhile: none is started on top" [ "$(starts det2)" = 0 ]

echo "bad settings"
setup badint; run badint SESSION_KEEP_INTERVAL=soon
check "an interval that is not a number: not started, said" grep -q "SESSION_KEEP_INTERVAL 'soon' is not a positive whole number" "$work/badint/stderr"
check "… nothing started"                               [ "$(starts badint)" = 0 ]

echo "shipped"
check "Dockerfile copies session-keep"                  grep -qE '^COPY session-keep /usr/local/bin/session-keep$' Dockerfile
check "entrypoint starts it"                            grep -qE '^    session-keep &$' entrypoint.sh
check "it reads no message: no bridge, no room, no inbox file is opened" [ "$(grep -vE '^\s*#' session-keep | grep -cE 'curl|chamber|inbox')" = 0 ]

# a red run must say why: the first run of this file went red on ten cells and
# the cause could not be read afterwards, because the scratch directory was gone
if [ "$fail" -gt 0 ]; then
  for d in "$work"/*/; do
    [ -s "$d/stderr" ] && { echo "--- $(basename "$d") stderr"; head -n 12 "$d/stderr"; }
  done
fi

echo "session-keep controls: passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
