#!/usr/bin/env bash
#
# Controls for inbox-watch (k8scc#59): run AS SHIPPED, with fake `tmux` and
# `sleep` ahead on PATH. The fake tmux records every call; the fake sleep runs
# the next hook script, which is how the inbox changes between two ticks of
# one watcher (its "already announced" memory lives in that one process).
#
# Each case carries a negative control: the opposite input must not give the
# same reading.
#
# Run: test/inbox-watch-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

cat > "$work/bin/tmux" <<'EOF'
#!/usr/bin/env bash
# fake tmux: one log line per call. Session exists iff $FAKE_DIR/session holds
# its creation epoch. send-keys fails once if $FAKE_DIR/sendfail exists.
# capture-pane prints $FAKE_DIR/pane (fails if $FAKE_DIR/capfail exists).
printf '%s\n' "$*" >> "$FAKE_DIR/tmux.log"
case "$1" in
  has-session) [ -f "$FAKE_DIR/session" ] ;;
  display-message) cat "$FAKE_DIR/session" ;;
  capture-pane) [ ! -f "$FAKE_DIR/capfail" ] && cat "$FAKE_DIR/pane" ;;
  send-keys) if [ -f "$FAKE_DIR/sendfail" ]; then rm -f "$FAKE_DIR/sendfail"; exit 1; fi ;;
esac
EOF
cat > "$work/bin/sleep" <<'EOF'
#!/usr/bin/env bash
# fake sleep: run the next hook (hook.1, hook.2, ...) instead of sleeping
n=$(( $(cat "$FAKE_DIR/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_DIR/sleeps"
[ -f "$FAKE_DIR/hook.$n" ] && bash "$FAKE_DIR/hook.$n"
exit 0
EOF
chmod +x "$work/bin/tmux" "$work/bin/sleep"

pass=0 fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local d="$1"; shift; if "$@"; then ok; else bad "$d"; fi; }
check_not() { local d="$1"; shift; if "$@"; then bad "$d"; else ok; fi; }

old=$(( $(date +%s) - 3600 ))

# Pane fixtures. HAND-WRITTEN from how Claude Code draws these screens, not
# captured from the image's claude; the pod cell (#60) is where they meet the
# real one. Trailing blank lines stand for the unused rows of a tall pane.
IDLE_BOX='  ⎿  Done. Next steps:
     1. rotate the key
     2. tell the company

╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
  ? for shortcuts



'
IDLE_RULE='● Restart applied.
────────────────────────────────────────────
❯ 
────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
'
PERMISSION='● Bash(talosctl -n 10.9.9.23 reboot)
╭──────────────────────────────────────────╮
│ Bash command                             │
│                                          │
│   talosctl -n 10.9.9.23 reboot           │
│                                          │
│ Do you want to proceed?                  │
│ ❯ 1. Yes                                 │
│   2. No, and tell Claude what to do      │
╰──────────────────────────────────────────╯
'
MENU_NOPHRASE='╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
 ❯ 1. Yes
   2. No
'
HALF_TYPED='╭──────────────────────────────────────────╮
│ > please check the hom                   │
╰──────────────────────────────────────────╯
  ? for shortcuts
'
SLASH='╭──────────────────────────────────────────╮
│ > /                                      │
╰──────────────────────────────────────────╯
  /help   Show help
'
BUSY='✻ Pondering… (12s · ↑ 1.2k tokens · esc to interrupt)
╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
'
PROMPT_HIGH='╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
line
line
line
line
line
line
'

# setup <case> : fresh home with an inbox, an old session, an idle pane, empty log
setup() {
  c="$work/$1"; mkdir -p "$c/home/workspace/inbox" "$c/fake"
  echo "$old" > "$c/fake/session"
  printf '%s' "$IDLE_BOX" > "$c/fake/pane"
  : > "$c/fake/tmux.log"
}
# run <case> [ENV=VALUE…]
run() {
  local c="$work/$1"; shift
  env -i PATH="$work/bin:$PATH" HOME="$c/home" FAKE_DIR="$c/fake" \
      INBOX_WATCH_INTERVAL=1 INBOX_WATCH_SETTLE=30 INBOX_WATCH_TICKS=1 "$@" \
      bash ./inbox-watch 2>"$c/stderr"
}
sends() { grep -c '^send-keys -t =claude: -l ' "$work/$1/fake/tmux.log"; }
enters() { grep -c '^send-keys -t =claude: Enter$' "$work/$1/fake/tmux.log"; }
msgs() { grep '^send-keys -t =claude: -l ' "$work/$1/fake/tmux.log"; }

echo "announce"
setup one
printf 'SECRETCONTENT\n' > "$work/one/home/workspace/inbox/2026-11-10-ha-homekit.md"
touch "$work/one/home/workspace/inbox/old.read"
run one
check "one unread: one line typed"              [ "$(sends one)" = 1 ]
check "one unread: then Enter"                  [ "$(enters one)" = 1 ]
check "one unread: names it"                    grep -q '2026-11-10-ha-homekit.md' "$work/one/fake/tmux.log"
check "one unread: says 1 new file"             grep -q '📥 inbox: 1 new file(s)' "$work/one/fake/tmux.log"
check_not "one unread: .read file not named"    grep -q 'old.read' "$work/one/fake/tmux.log"
check_not "one unread: contents never typed"    grep -q 'SECRETCONTENT' "$work/one/fake/tmux.log"
check "one unread: pod log line, count only"    grep -q '^inbox-watch: announced 1 file(s)' "$work/one/stderr"
check_not "one unread: pod log has no name"     grep -q 'homekit' "$work/one/stderr"

echo "no session / young session"
setup nosess; rm "$work/nosess/fake/session"
touch "$work/nosess/home/workspace/inbox/a.md"
run nosess
check "no session: nothing typed"               [ "$(sends nosess)" = 0 ]
check "control: has-session was asked"          grep -q '^has-session' "$work/nosess/fake/tmux.log"
setup young; date +%s > "$work/young/fake/session"
touch "$work/young/home/workspace/inbox/a.md"
run young
check "session younger than settle: nothing"    [ "$(sends young)" = 0 ]
run young INBOX_WATCH_SETTLE=0
check "control: settle 0, same session: typed"  [ "$(sends young)" = 1 ]

echo "once per file"
setup twice
touch "$work/twice/home/workspace/inbox/a.md"
run twice INBOX_WATCH_TICKS=3
check "unchanged inbox over 3 ticks: typed once" [ "$(sends twice)" = 1 ]
setup grow
touch "$work/grow/home/workspace/inbox/a.md"
echo 'touch "$FAKE_DIR/../home/workspace/inbox/b.md"' > "$work/grow/fake/hook.1"
run grow INBOX_WATCH_TICKS=2
check "control: a file arriving between ticks is typed" [ "$(sends grow)" = 2 ]
check "second line names only the new file"     grep -q '(b.md)' <<<"$(msgs grow | sed -n 2p)"
setup again
touch "$work/again/home/workspace/inbox/a.md"
echo 'mv "$FAKE_DIR/../home/workspace/inbox/a.md" "$FAKE_DIR/../home/workspace/inbox/a.md.read"' > "$work/again/fake/hook.1"
echo 'touch "$FAKE_DIR/../home/workspace/inbox/a.md"' > "$work/again/fake/hook.2"
run again INBOX_WATCH_TICKS=3
check "read, then the same name again: typed again" [ "$(sends again)" = 2 ]

echo "retry"
setup retry
touch "$work/retry/home/workspace/inbox/a.md" "$work/retry/fake/sendfail"
run retry INBOX_WATCH_TICKS=2
check "failed send is retried on the next tick" [ "$(sends retry)" = 2 ]
check "retry: Enter only after the good send"   [ "$(enters retry)" = 1 ]
check "retry: said so in the pod log"           grep -q 'send-keys failed, will retry' "$work/retry/stderr"
setup noretry
touch "$work/noretry/home/workspace/inbox/a.md"
run noretry INBOX_WATCH_TICKS=2
check "control: a good send is not repeated"    [ "$(sends noretry)" = 1 ]

echo "file names"
setup nl
touch "$work/nl/home/workspace/inbox/$(printf 'evil\nrm -rf x.md')"
run nl
check "newline in a name: still one line typed" [ "$(wc -l < "$work/nl/fake/tmux.log" | tr -d ' ')" = "$(grep -c . "$work/nl/fake/tmux.log")" ]
check "newline in a name: shown as ?"           grep -q 'evil?rm -rf x.md' "$work/nl/fake/tmux.log"
check "newline in a name: one Enter, ours"      [ "$(enters nl)" = 1 ]
check_not "control: no log line starts with the name's tail" grep -q '^rm -rf' "$work/nl/fake/tmux.log"

echo "pane gate"
# gate <case> <pane-text> : one unread file, that pane, one tick
gate() { setup "$1"; touch "$work/$1/home/workspace/inbox/a.md"; printf '%s' "$2" > "$work/$1/fake/pane"; run "$1"; }
gate g-idle "$IDLE_BOX";            check "idle prompt (boxed, numbered reply above): typed" [ "$(sends g-idle)" = 1 ]
gate g-rule "$IDLE_RULE";           check "idle prompt (rule-drawn, ❯): typed"            [ "$(sends g-rule)" = 1 ]
gate g-perm "$PERMISSION";          check "permission dialog: nothing typed"             [ "$(sends g-perm)" = 0 ]
check "permission dialog: no Enter either"   [ "$(enters g-perm)" = 0 ]
check "permission dialog: pod log says why"  grep -q 'skipped (pane not idle: dialog)' "$work/g-perm/stderr"
gate g-menu "$MENU_NOPHRASE";       check "menu below an empty prompt: nothing typed"     [ "$(sends g-menu)" = 0 ]
gate g-half "$HALF_TYPED";          check "half-typed prompt: nothing typed"             [ "$(sends g-half)" = 0 ]
gate g-slash "$SLASH";              check "slash menu open: nothing typed"               [ "$(sends g-slash)" = 0 ]
gate g-busy "$BUSY";                check "turn running: nothing typed"                  [ "$(sends g-busy)" = 0 ]
check "turn running: pod log says why"       grep -q 'pane not idle: turn running' "$work/g-busy/stderr"
gate g-high "$PROMPT_HIGH";         check "empty prompt not at the bottom: nothing typed" [ "$(sends g-high)" = 0 ]
gate g-empty "";                    check "empty pane: nothing typed"                    [ "$(sends g-empty)" = 0 ]
setup g-cap; touch "$work/g-cap/home/workspace/inbox/a.md" "$work/g-cap/fake/capfail"; run g-cap
check "capture-pane fails: nothing typed"   [ "$(sends g-cap)" = 0 ]
# a skipped tick marks nothing: the dialog closes, the next tick types
setup g-later; touch "$work/g-later/home/workspace/inbox/a.md"; printf '%s' "$PERMISSION" > "$work/g-later/fake/pane"
printf 'printf %%s "%s" > "$FAKE_DIR/pane"\n' "$IDLE_RULE" > "$work/g-later/fake/hook.1"
run g-later INBOX_WATCH_TICKS=2
check "dialog then idle: typed once, after it closed" [ "$(sends g-later)" = 1 ]
check "dialog then idle: the capture came first" bash -c 'grep -n "" "$1" | grep -m1 -E "capture-pane|send-keys" | grep -q capture-pane' _ "$work/g-later/fake/tmux.log"

echo "off / absent"
setup off
touch "$work/off/home/workspace/inbox/a.md"
run off INBOX_WATCH_INTERVAL=0 INBOX_WATCH_TICKS=
check "interval 0: no tmux call at all"         [ ! -s "$work/off/fake/tmux.log" ]
check "interval 0: says off"                    grep -q 'INBOX_WATCH_INTERVAL=0, off' "$work/off/stderr"
setup absent; rm -rf "$work/absent/home/workspace/inbox"
run absent
check "absent inbox: nothing typed"             [ "$(sends absent)" = 0 ]
setup empty
run empty
check "empty inbox: nothing typed"              [ "$(sends empty)" = 0 ]
setup readonly; touch "$work/readonly/home/workspace/inbox/x.read"
run readonly
check "only .read files: nothing typed"         [ "$(sends readonly)" = 0 ]

echo "inbox-watch controls: passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
