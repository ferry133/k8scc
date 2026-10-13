#!/usr/bin/env bash
#
# Controls for claude-session: the Talos permission profile and the inbox
# notice. The script is run AS SHIPPED, with HOME redirected to a scratch
# directory and fake `claude` / `tmux` binaries ahead of the real ones on
# PATH -- the final `exec tmux` lands on the fake, which prints its argv and
# exits. Nothing here is a copy of claude-session; a copy would keep passing
# after the original changed.
#
# Each case has a negative control: the opposite input must NOT produce the
# same reading, otherwise 0 failures cannot distinguish "works" from "the
# check cannot fail".
#
# Run: test/claude-session-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/bin"
cat > "$work/bin/claude" <<'EOF'
#!/usr/bin/env bash
# fake claude: `mcp get <name>` says "not registered", `mcp add …` succeeds.
printf 'claude %s\n' "$*" >> "${FAKE_LOG:?}"
case "${1:-} ${2:-}" in
  "mcp get") exit 1 ;;
  "mcp add") exit 0 ;;
esac
exit 0
EOF
cat > "$work/bin/tmux" <<'EOF'
#!/usr/bin/env bash
printf 'tmux %s\n' "$*" >> "${FAKE_LOG:?}"
exit 0
EOF
chmod +x "$work/bin/claude" "$work/bin/tmux"

pass=0
fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check() { # check <description> <command…>   -- passes when the command succeeds
  local desc="$1"; shift
  if "$@"; then ok; else bad "$desc"; fi
}
check_not() { # check_not <description> <command…>   -- passes when the command FAILS
  local desc="$1"; shift
  if "$@"; then bad "$desc"; else ok; fi
}

# run_session <home> [ENV=VALUE…] -> stdout+stderr of one claude-session run
run_session() {
  local home="$1"; shift
  mkdir -p "$home"
  : > "$home/fake.log"
  env -i PATH="$work/bin:$PATH" HOME="$home" FAKE_LOG="$home/fake.log" \
      TTYD_USER="" CLAUDE_USER_ID="tester" DATABASE_URL="" TALOS_MCP_URL="" "$@" \
      bash ./claude-session </dev/null 2>&1
}

settings_of() { cat "$1/.claude/settings.json"; }
allow_of() { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("permissions",{}).get("allow")))' "$1/.claude/settings.json"; }
ask_of()   { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("permissions",{}).get("ask")))' "$1/.claude/settings.json"; }
has_perms() { python3 -c 'import json,sys; sys.exit(0 if "permissions" in json.load(open(sys.argv[1])) else 1)' "$1/.claude/settings.json"; }
# mtime via python, not stat(1): GNU `stat -f %m FILE` reads `%m` as a file
# name and prints FILE's *file-system* stats to stdout (rc 1), so a
# `stat -f … || stat -c …` chain captures free-block counts that move between
# calls -- red on Linux, green on macOS (CI run 38475915481, 2026-10-13).
mtime_of() { python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "$1/.claude/settings.json"; }

READS='["mcp__talos__get_node_status", "mcp__talos__get_etcd_members", "mcp__talos__get_link_status", "mcp__talos__get_service_logs"]'

echo "permission profile"
# fresh pod, sidecar present
h="$work/fresh"
out=$(run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse)
check "fresh pod: allow holds the four read tools" [ "$(allow_of "$h")" = "$READS" ]
check "fresh pod: ask holds reboot_node"            [ "$(ask_of "$h")" = '["mcp__talos__reboot_node"]' ]
check "fresh pod: the initial env key survives"    grep -q '"DISABLE_AUTOUPDATER": "1"' "$h/.claude/settings.json"
check "fresh pod: tmux was exec'd with claude"      grep -q '^tmux -u new-session -A -s claude -- claude$' "$h/fake.log"
check "fresh pod: talos registered"                 grep -q '^claude mcp add --transport sse --scope local talos http://127.0.0.1:8765/sse$' "$h/fake.log"
before=$(settings_of "$h"); mtime1=$(mtime_of "$h")
sleep 1
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
mtime2=$(mtime_of "$h")
check "second run: bytes identical"                 [ "$(settings_of "$h")" = "$before" ]
check "second run: file not rewritten (mtime)"      [ "$mtime1" = "$mtime2" ]
# control: the reading must move when the file IS rewritten, or the line
# above cannot fail (the sleep covers filesystems with 1 s mtime granularity)
sleep 1; touch "$h/.claude/settings.json"
check_not "control: mtime reading moves on a rewrite" [ "$(mtime_of "$h")" = "$mtime2" ]

# customer rules survive, order kept, no duplicates
h="$work/customer"
mkdir -p "$h/.claude"
cat > "$h/.claude/settings.json" <<'EOF'
{"env":{"DISABLE_AUTOUPDATER":"1"},"permissions":{"allow":["Bash(ls:*)","mcp__talos__get_link_status"],"deny":["Bash(rm:*)"]},"theme":"dark"}
EOF
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "customer: own allow entry stays first"       [ "$(allow_of "$h")" = '["Bash(ls:*)", "mcp__talos__get_link_status", "mcp__talos__get_node_status", "mcp__talos__get_etcd_members", "mcp__talos__get_service_logs"]' ]
check "customer: deny list untouched"               grep -q '"Bash(rm:\*)"' "$h/.claude/settings.json"
check "customer: unrelated top-level key kept"      grep -q '"theme": "dark"' "$h/.claude/settings.json"
check "customer: ask added"                         [ "$(ask_of "$h")" = '["mcp__talos__reboot_node"]' ]

# negative control: sidecar absent -> permissions never appears
h="$work/nosidecar"
run_session "$h" >/dev/null
check_not "no TALOS_MCP_URL: no permissions key" has_perms "$h"
check_not "no TALOS_MCP_URL: talos not registered" grep -q 'mcp add.*talos' "$h/fake.log"

# negative control: unparseable file is left alone and the failure is said
h="$work/broken"
mkdir -p "$h/.claude"
printf '{"env": {' > "$h/.claude/settings.json"
out=$(run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse)
check "broken JSON: file byte-identical"            [ "$(cat "$h/.claude/settings.json")" = '{"env": {' ]
check "broken JSON: terminal told"                  grep -q 'permission 設定沒有寫入' <<<"$out"
check "broken JSON: session still starts"           grep -q '^tmux ' "$h/fake.log"

echo "inbox notice"
h="$work/inbox-unread"
mkdir -p "$h/workspace/inbox"
touch "$h/workspace/inbox/2026-10-11-break-glass.md" "$h/workspace/inbox/old.read"
out=$(run_session "$h")
check "one unread: the line"                        grep -q '^📥 inbox: 1 file(s) — read ~/workspace/inbox/ before anything else$' <<<"$out"
check_not "one unread: contents never printed" grep -q 'break-glass' <<<"$out"
check "one unread: before tmux"                     bash -c 'grep -q "inbox" <<<"$1" && grep -q "^tmux " "$2"' _ "$out" "$h/fake.log"
touch "$h/workspace/inbox/second.txt"
out=$(run_session "$h")
check "two unread: count is 2"                      grep -q '^📥 inbox: 2 file(s)' <<<"$out"

h="$work/inbox-read"
mkdir -p "$h/workspace/inbox"
touch "$h/workspace/inbox/done.read"
out=$(run_session "$h")
check_not "only .read files: nothing" grep -q 'inbox' <<<"$out"

h="$work/inbox-empty"
mkdir -p "$h/workspace/inbox"
out=$(run_session "$h")
check_not "empty dir: nothing" grep -q 'inbox' <<<"$out"

h="$work/inbox-absent"
out=$(run_session "$h")
check_not "absent dir: nothing" grep -q 'inbox' <<<"$out"
check "absent dir: session still starts"            grep -q '^tmux ' "$h/fake.log"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
