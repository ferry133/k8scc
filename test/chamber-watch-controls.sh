#!/usr/bin/env bash
#
# Controls for chamber-watch (openspec im-chamber-client tasks 4.1 and 5.2,
# k8scc#70): run AS SHIPPED, with fake `tmux`, `sleep` and `curl` ahead on
# PATH. The fake curl stands for the bridge: it records every call and answers
# the three local paths from files. The bridge's own half of this (what it
# does with the signal) is in test/test_chamber_bridge.py, and one cell there
# runs this script against the real bridge.
#
# The pane fixtures are the ones test/inbox-watch-controls.sh uses, extracted
# from that file, not copied: the two watchers must be judged on the same
# screens.
#
# Each case carries a control: the opposite input must not give the same
# reading.
#
# Run: test/chamber-watch-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

cat > "$work/bin/tmux" <<'EOF'
#!/usr/bin/env bash
# fake tmux. Session exists iff $FAKE_DIR/session holds its creation epoch.
# The pane's process is $FAKE_DIR/cmd (default claude). send-keys fails once
# if $FAKE_DIR/sendfail exists. capture-pane prints $FAKE_DIR/pane.
printf '%s\n' "$*" >> "$FAKE_DIR/tmux.log"
case "$1" in
  has-session) [ -f "$FAKE_DIR/session" ] ;;
  display-message)
    # like tmux 3.3a: a bare `=claude` target prints an empty line with rc 0
    if [ "$4" != "=claude:" ]; then echo; exit 0; fi
    case "$5" in
      *pane_current_command*) cat "$FAKE_DIR/cmd" 2>/dev/null || echo claude ;;
      *session_created*) cat "$FAKE_DIR/session" ;;
    esac ;;
  capture-pane) [ ! -f "$FAKE_DIR/capfail" ] && cat "$FAKE_DIR/pane" ;;
  send-keys)
    if [ -f "$FAKE_DIR/sendfail" ]; then rm -f "$FAKE_DIR/sendfail"; exit 1; fi
    # a typed line followed by Enter starts a turn: the next capture is busy
    [ "${*: -1}" = Enter ] && [ -f "$FAKE_DIR/busy-after-enter" ] && cp "$FAKE_DIR/busy-after-enter" "$FAKE_DIR/pane"
    exit 0 ;;
esac
EOF
cat > "$work/bin/sleep" <<'EOF'
#!/usr/bin/env bash
n=$(( $(cat "$FAKE_DIR/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_DIR/sleeps"
[ -f "$FAKE_DIR/hook.$n" ] && bash "$FAKE_DIR/hook.$n"
exit 0
EOF
cat > "$work/bin/curl" <<'EOF'
#!/usr/bin/env bash
# fake curl = the bridge. One log line per call: METHOD URL. A body given with
# --data-binary is appended to $FAKE_DIR/announced. $FAKE_DIR/bridgedown makes
# every call fail the way an unreachable socket does.
method=GET; url=; body=; sock=
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift ;;
    --data-binary) body="$2"; shift ;;
    --unix-socket) sock="$2"; shift ;;
    --max-time) shift ;;
    http://*) url="$1" ;;
  esac
  shift
done
printf '%s %s sock=%s\n' "$method" "$url" "$sock" >> "$FAKE_DIR/curl.log"
[ -f "$FAKE_DIR/bridgedown" ] && exit 7
case "$method $url" in
  "POST http://bridge/bridge/beat") echo deliver ;;
  "GET http://bridge/bridge/pending") cat "$FAKE_DIR/pending" 2>/dev/null ;;
  "POST http://bridge/bridge/announced")
    [ -f "$FAKE_DIR/announcefail" ] && exit 22
    printf '%s' "$body" >> "$FAKE_DIR/announced" ;;
  *) exit 22 ;;
esac
exit 0
EOF
chmod +x "$work/bin/tmux" "$work/bin/sleep" "$work/bin/curl"

pass=0 fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local d="$1"; shift; if "$@"; then ok; else bad "$d"; fi; }
check_not() { local d="$1"; shift; if "$@"; then bad "$d"; else ok; fi; }

# the pane fixtures of the inbox-watch controls, extracted
fixtures=$(sed -n '/^IDLE_BOX=/,/^# setup <case>/p' test/inbox-watch-controls.sh | sed '$d')
eval "$fixtures"
check "control: the fixtures could be extracted" [ -n "${IDLE_BOX:-}" ]
check "control: … including the real-capture ones" [ -n "${IDLE_REAL:-}" ]

old=$(( $(date +%s) - 3600 ))

# setup <case> : an old session, an idle pane, claude in front, nothing pending
setup() {
  c="$work/$1"; mkdir -p "$c/home" "$c/fake" "$c/signers"
  echo "$old" > "$c/fake/session"
  printf '%s' "$IDLE_BOX" > "$c/fake/pane"
  : > "$c/fake/tmux.log"; : > "$c/fake/curl.log"; : > "$c/fake/announced"
}
# run <case> [ENV=VALUE…]
run() {
  local c="$work/$1"; shift
  env -i PATH="$work/bin:$PATH" HOME="$c/home" FAKE_DIR="$c/fake" \
      CHAMBER_URL="unix:/run/chamber/bridge.sock" CHAMBER_BEAT=1 CHAMBER_WATCH_TICKS=1 \
      CHAMBER_SIGNERS_DIR="$c/signers" "$@" \
      bash chamber/chamber-watch 2>"$c/stderr"
}
beats()  { grep -c '^POST http://bridge/bridge/beat ' "$work/$1/fake/curl.log"; }
sends()  { grep -c '^send-keys -t =claude: -l ' "$work/$1/fake/tmux.log"; }
enters() { grep -c '^send-keys -t =claude: Enter$' "$work/$1/fake/tmux.log"; }
calls()  { wc -l < "$work/$1/fake/curl.log" | tr -d ' '; }

echo "the alive signal: a session that can act"
setup idle; run idle
check "idle prompt: one signal"                     [ "$(beats idle)" = 1 ]
check "idle prompt: over the socket in CHAMBER_URL" grep -q 'sock=/run/chamber/bridge.sock$' "$work/idle/fake/curl.log"
check "idle prompt: the log says so"                grep -q '^chamber-watch: alive signal sent (idle)' "$work/idle/stderr"
setup real; printf '%s' "$IDLE_REAL" > "$work/real/fake/pane"; run real
check "the idle prompt as captured on the image: one signal" [ "$(beats real)" = 1 ]
setup busy; printf '%s' "$BUSY" > "$work/busy/fake/pane"; run busy
check "a turn in progress: one signal"              [ "$(beats busy)" = 1 ]
check "a turn in progress: the log says which"      grep -q 'alive signal sent (turn running)' "$work/busy/stderr"

echo "the alive signal: everything else sends nothing"
for pair in "PERMISSION:dialog" "MENU_NOPHRASE:menu below the prompt" "TRUST_REAL:no empty prompt" "HALF_TYPED:no empty prompt" "SLASH:" "PROMPT_HIGH:"; do
  name=${pair%%:*}; why=${pair#*:}
  setup "p-$name"; printf '%s' "${!name}" > "$work/p-$name/fake/pane"; run "p-$name"
  check "$name: no signal"                          [ "$(beats "p-$name")" = 0 ]
  check "$name: the log names a reason"             grep -q '^chamber-watch: no alive signal: ' "$work/p-$name/stderr"
  [ -n "$why" ] && check "$name: the reason is the gate's ($why)" grep -q "no alive signal: $why" "$work/p-$name/stderr"
done
setup nosession; rm "$work/nosession/fake/session"; run nosession
check "no session: no signal"                       [ "$(beats nosession)" = 0 ]
check "no session: says so"                         grep -q 'no alive signal: no session' "$work/nosession/stderr"
check "no session: nothing is started (tmux was only asked whether it exists)" [ "$(grep -vc '^has-session ' "$work/nosession/fake/tmux.log")" = 0 ]
setup shell; echo bash > "$work/shell/fake/cmd"; run shell
check "the pane's process is a shell, the screen still shows an idle prompt: no signal" [ "$(beats shell)" = 0 ]
check "… and the log says which process"            grep -q "the pane's process is 'bash', not claude" "$work/shell/stderr"
setup capfail; touch "$work/capfail/fake/capfail"; run capfail
check "the capture fails: no signal"                [ "$(beats capfail)" = 0 ]
setup emptypane; : > "$work/emptypane/fake/pane"; run emptypane
check "an empty pane: no signal"                    [ "$(beats emptypane)" = 0 ]

echo "the alive signal over time"
setup stops
printf '%s' "$PERMISSION" > "$work/stops/fake/perm"; echo 'cp "$FAKE_DIR/perm" "$FAKE_DIR/pane"' > "$work/stops/fake/hook.1"
run stops CHAMBER_WATCH_TICKS=3
check "idle, then a permission prompt for two beats: one signal, not three" [ "$(beats stops)" = 1 ]
check "… the change is logged once"                 [ "$(grep -c 'no alive signal: dialog' "$work/stops/stderr")" = 1 ]
setup steady; printf '%s' "$BUSY" > "$work/steady/fake/busy"
echo 'cp "$FAKE_DIR/busy" "$FAKE_DIR/pane"' > "$work/steady/fake/hook.1"
run steady CHAMBER_WATCH_TICKS=3
check "idle, then busy, busy: three signals"        [ "$(beats steady)" = 3 ]
check "… and one log line, not one per beat"        [ "$(grep -c 'alive signal sent' "$work/steady/stderr")" = 1 ]
setup down; touch "$work/down/fake/bridgedown"; run down
check "the bridge cannot be reached: said, with the socket path" grep -q 'the bridge cannot be reached at /run/chamber/bridge.sock' "$work/down/stderr"

echo "the announcement"
setup ann; printf '2 cust-a\n' > "$work/ann/fake/pending"; run ann
check "two waiting in one room, idle: one line typed"   [ "$(sends ann)" = 1 ]
check "… then Enter"                                    [ "$(enters ann)" = 1 ]
check "… the line names the room and the count"         grep -qF '💬 chamber: 2 message(s) for you in cust-a — run `chamber read cust-a` and handle them per your policy' "$work/ann/fake/tmux.log"
check "… the bridge is told what was announced"         grep -qx '2 cust-a' "$work/ann/fake/announced"
check "… pod log: a count, no room"                     grep -q '^chamber-watch: announced 2 message(s)' "$work/ann/stderr"
check_not "… pod log has no room name"                  grep -q 'cust-a' "$work/ann/stderr"
setup none; run none
check "control: nothing waiting, idle: nothing typed"   [ "$(sends none)" = 0 ]
setup annbusy; printf '2 cust-a\n' > "$work/annbusy/fake/pending"; printf '%s' "$BUSY" > "$work/annbusy/fake/pane"; run annbusy
check "waiting, a turn is running: nothing typed"       [ "$(sends annbusy)" = 0 ]
check "… and the bridge is not told"                    [ ! -s "$work/annbusy/fake/announced" ]
check "… but the signal is still sent"                  [ "$(beats annbusy)" = 1 ]
# the line-by-line gate inside announce would also stop the typing; this cell
# is the outer rule's own: while a turn runs the list is not even asked for
check "… and the bridge is not asked for the list"      [ "$(grep -c "^GET http://bridge/bridge/pending " "$work/annbusy/fake/curl.log")" = 0 ]
check "control: when idle the list is asked for"        [ "$(grep -c "^GET http://bridge/bridge/pending " "$work/ann/fake/curl.log")" = 1 ]
setup annperm; printf '2 cust-a\n' > "$work/annperm/fake/pending"; printf '%s' "$PERMISSION" > "$work/annperm/fake/pane"; run annperm
check "waiting, a permission prompt is open: no key at all" [ "$(grep -c '^send-keys' "$work/annperm/fake/tmux.log")" = 0 ]
setup later; printf '1 cust-a\n' > "$work/later/fake/pending"; printf '%s' "$PERMISSION" > "$work/later/fake/pane"
printf '%s' "$IDLE_BOX" > "$work/later/fake/idle"; echo 'cp "$FAKE_DIR/idle" "$FAKE_DIR/pane"' > "$work/later/fake/hook.1"
run later CHAMBER_WATCH_TICKS=2
check "… and it is typed on the first beat after the pane is idle" [ "$(sends later)" = 1 ]
setup young; printf '1 cust-a\n' > "$work/young/fake/pending"; date +%s > "$work/young/fake/session"; run young
check "a session younger than the settle time: nothing typed" [ "$(sends young)" = 0 ]
check "… and said"                                      grep -q 'skipped (the session is younger than 30s' "$work/young/stderr"
check "… but the signal is sent (the pane is judged, not the age)" [ "$(beats young)" = 1 ]
setup sendfail; printf '1 cust-a\n' > "$work/sendfail/fake/pending"; touch "$work/sendfail/fake/sendfail"; run sendfail CHAMBER_WATCH_TICKS=2
check "a failed send marks nothing the first time"      [ "$(grep -c . "$work/sendfail/fake/announced")" = 1 ]
check "… and is tried again on the next beat"           grep -q 'send-keys failed, will retry' "$work/sendfail/stderr"
setup two; printf '1 cust-a\n3 company\n' > "$work/two/fake/pending"
printf '%s' "$BUSY" > "$work/two/fake/busy-after-enter"; run two
check "two rooms: the first line starts a turn, so the second is not typed into it" [ "$(sends two)" = 1 ]
check "… and only the first is reported as announced"   [ "$(cat "$work/two/fake/announced")" = "1 cust-a" ]
setup two2; printf '1 cust-a\n3 company\n' > "$work/two2/fake/pending"; run two2
check "control: with the pane staying idle both lines are typed" [ "$(sends two2)" = 2 ]
setup annfail; printf '1 cust-a\n' > "$work/annfail/fake/pending"; touch "$work/annfail/fake/announcefail"; run annfail
check "the bridge cannot be told: said, so a repeat is expected" grep -q 'the bridge could not be told; they will be announced again' "$work/annfail/stderr"
setup junk; printf 'x cust-a\n0 cust-b\n\n2\n' > "$work/junk/fake/pending"; run junk
check "lines that are not '<count> <room>' type nothing" [ "$(sends junk)" = 0 ]

echo "the verifier line"
setup vs; printf '# comment\n\nperson namespaces="chamber-directive" ssh-ed25519 AAAA\nother ssh-ed25519 BBBB\n' > "$work/vs/signers/allowed_signers"
printf 'ssh-ed25519 CCCC\n' > "$work/vs/signers/revoked_signers"; run vs
check "counts keys, not lines"   grep -qE '^chamber-verify-status ts=[0-9]+ interval=1 signers=2 revoked=1 sshkeygen=(ok|missing)$' "$work/vs/stderr"
setup vs0; run vs0
check "no lists: unmeasured, every field still there" grep -qE '^chamber-verify-status ts=[0-9]+ interval=1 signers=unmeasured revoked=unmeasured sshkeygen=(ok|missing)$' "$work/vs0/stderr"

echo "off / refuses to start"
setup off; run off CHAMBER_URL=
check "CHAMBER_URL empty: off, one line"            grep -q '^chamber-watch: not configured (CHAMBER_URL is empty), off$' "$work/off/stderr"
check "… the bridge and tmux were asked nothing"    [ "$(calls off)$(wc -l < "$work/off/fake/tmux.log" | tr -d ' ')" = 00 ]
setup https; run https CHAMBER_URL=https://example.invalid
check "an https address in app is refused"          grep -q 'CHAMBER_URL must be unix:<path> in app' "$work/https/stderr"
check "… nothing asked"                             [ "$(calls https)" = 0 ]
setup nogate; mkdir -p "$work/nogate/bin/sub"; cp chamber/chamber-watch "$work/nogate/bin/sub/chamber-watch"
env -i PATH="$work/bin:$PATH" HOME="$work/nogate/home" FAKE_DIR="$work/nogate/fake" CHAMBER_URL="unix:/run/chamber/bridge.sock" \
    CHAMBER_BEAT=1 CHAMBER_WATCH_TICKS=1 bash "$work/nogate/bin/sub/chamber-watch" 2>"$work/nogate/stderr"
check "no pane gate next to it: does not start"     grep -q 'the pane gate is missing' "$work/nogate/stderr"
check "… no signal"                                 [ "$(beats nogate)" = 0 ]
cp pane-gate "$work/nogate/bin/sub/pane-gate"
env -i PATH="$work/bin:$PATH" HOME="$work/nogate/home" FAKE_DIR="$work/nogate/fake" CHAMBER_URL="unix:/run/chamber/bridge.sock" \
    CHAMBER_BEAT=1 CHAMBER_WATCH_TICKS=1 bash "$work/nogate/bin/sub/chamber-watch" 2>"$work/nogate/stderr"
check "control: the same copy with the gate beside it (the image's layout) sends the signal" [ "$(beats nogate)" = 1 ]

echo "shipped"
check "Dockerfile copies chamber-watch to /usr/local/bin" grep -qE '^COPY chamber/chamber-watch /usr/local/bin/chamber-watch$' Dockerfile
check "the announcement line holds no message text or sender field" [ "$(grep -c 'local msg="💬 chamber: \${count} message(s) for you in \${room} ' chamber/chamber-watch)" = 1 ]

echo "chamber-watch controls: passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
