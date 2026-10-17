#!/usr/bin/env bash
#
# Controls for claude-session: the Talos permission profile, the apps-repo
# write path, and the inbox notice. The script is run AS SHIPPED, with HOME redirected to a scratch
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
      TTYD_USER="" CLAUDE_USER_ID="tester" DATABASE_URL="" TALOS_MCP_URL="" IM_APPS_REPO="" "$@" \
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

# all five tools under allow since the 2026-10-15 ruling; reboot_node is never under ask
PROFILE='["mcp__talos__get_node_status", "mcp__talos__get_etcd_members", "mcp__talos__get_link_status", "mcp__talos__get_service_logs", "mcp__talos__reboot_node"]'

echo "permission profile"
# fresh pod, sidecar present
h="$work/fresh"
out=$(run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse)
check "fresh pod: allow holds all five tools"      [ "$(allow_of "$h")" = "$PROFILE" ]
check "fresh pod: no ask key is written"            [ "$(ask_of "$h")" = "null" ]
check "fresh pod: the initial env key survives"    grep -q '"DISABLE_AUTOUPDATER": "1"' "$h/.claude/settings.json"
check "fresh pod: tmux was exec'd with claude"      grep -q '^tmux -u new-session -A -s claude -- claude$' "$h/fake.log"
# a stray line in the script runs as a command and the session still starts --
# only its stderr says so (a cut-off comment did exactly that, 2026-10-17)
check_not "fresh pod: no stray command ran" grep -q 'command not found' <<<"$out"
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

# existing rules survive, order kept, no duplicates
h="$work/existing"
mkdir -p "$h/.claude"
cat > "$h/.claude/settings.json" <<'EOF'
{"env":{"DISABLE_AUTOUPDATER":"1"},"permissions":{"allow":["Bash(ls:*)","mcp__talos__get_link_status"],"deny":["Bash(rm:*)"]},"theme":"dark"}
EOF
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "existing: own allow entry stays first"       [ "$(allow_of "$h")" = '["Bash(ls:*)", "mcp__talos__get_link_status", "mcp__talos__get_node_status", "mcp__talos__get_etcd_members", "mcp__talos__get_service_logs", "mcp__talos__reboot_node"]' ]
check "existing: deny list untouched"               grep -q '"Bash(rm:\*)"' "$h/.claude/settings.json"
check "existing: unrelated top-level key kept"      grep -q '"theme": "dark"' "$h/.claude/settings.json"
check "existing: no ask key invented"               [ "$(ask_of "$h")" = "null" ]

# migration: a pod that ran the previous image carries reboot_node under ask on
# its PVC; only that entry leaves ask, anything else under ask stays
h="$work/migrate"
mkdir -p "$h/.claude"
cat > "$h/.claude/settings.json" <<'EOF'
{"env":{"DISABLE_AUTOUPDATER":"1"},"permissions":{"allow":["mcp__talos__get_node_status","mcp__talos__get_etcd_members","mcp__talos__get_link_status","mcp__talos__get_service_logs"],"ask":["mcp__talos__reboot_node","Bash(curl:*)"]}}
EOF
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "migrate: reboot_node moved out of ask"       [ "$(ask_of "$h")" = '["Bash(curl:*)"]' ]
check "migrate: reboot_node now under allow"        [ "$(allow_of "$h")" = "$PROFILE" ]
before=$(settings_of "$h")
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "migrate: second run changes nothing"         [ "$(settings_of "$h")" = "$before" ]

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

echo "apps repo"
APPS='["Bash(git *)", "Bash(sops *)"]'
REPO=ferry133/jg-x-apps
mtime() { python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "$1"; }
gitcfg() { HOME="$1" git config --global --get "$2"; }
# ssh -G prints the resolved options; identityfile lines are what a push offers
idfiles() { ssh "$@" -G 2>/dev/null | awk '$1 == "identityfile" {print $2}'; }
put_key() { mkdir -p "$1/.ssh"; printf 'not a real key\n' > "$1/.ssh/im-apps.key"; chmod "$2" "$1/.ssh/im-apps.key"; }

# both inputs present, key 0600, no sidecar
h="$work/apps"
put_key "$h" 0600
out=$(run_session "$h" IM_APPS_REPO=$REPO)
cfg="$h/.ssh/config"
check "apps: ssh config written"                    test -f "$cfg"
check "apps: alias present"                         grep -q '^Host github.com-im-apps$' "$cfg"
check "apps: StrictHostKeyChecking yes"             grep -q '^  StrictHostKeyChecking yes$' "$cfg"
check "apps: pinned known_hosts"                    grep -q '^  UserKnownHostsFile /etc/ssh/github-known-hosts$' "$cfg"
check "apps: user.name im"                          [ "$(gitcfg "$h" user.name)" = "im" ]
check "apps: user.email from the repo half"         [ "$(gitcfg "$h" user.email)" = "im@jg-x-apps" ]
check "apps: core.sshCommand names the config"      [ "$(gitcfg "$h" core.sshCommand)" = "ssh -F $cfg" ]
check "apps: allow gains only git and sops"         [ "$(allow_of "$h")" = "$APPS" ]
check "apps: no ask key"                            [ "$(ask_of "$h")" = "null" ]
check_not "apps: nothing said when all is well" grep -q 'apps repo' <<<"$out"
# what a push would offer: only the alias resolves the mounted key ...
check "-F alias: resolves the key"                  grep -qxF "$h/.ssh/im-apps.key" <<<"$(idfiles -F "$cfg" github.com-im-apps)"
check_not "-F github.com: does not" grep -qxF "$h/.ssh/im-apps.key" <<<"$(idfiles -F "$cfg" github.com)"
# ... and only through -F. HOME is redirected here, so it differs from this
# user's passwd home: a bare ssh never reads $HOME/.ssh/config. The
# core.sshCommand git will run is exercised as written, not re-typed.
check_not "HOME != pw_dir: bare ssh misses the alias" grep -qxF "$h/.ssh/im-apps.key" <<<"$(HOME="$h" idfiles github.com-im-apps)"
read -r -a sshcmd <<<"$(gitcfg "$h" core.sshCommand)"
check "HOME != pw_dir: git's sshCommand finds it"   grep -qxF "$h/.ssh/im-apps.key" <<<"$("${sshcmd[@]}" -G github.com-im-apps 2>/dev/null | awk '$1 == "identityfile" {print $2}')"
# second run: nothing moves
b_cfg=$(cat "$cfg"); b_git=$(cat "$h/.gitconfig"); b_set=$(settings_of "$h")
m_cfg=$(mtime "$cfg"); m_git=$(mtime "$h/.gitconfig"); m_set=$(mtime "$h/.claude/settings.json")
sleep 1
run_session "$h" IM_APPS_REPO=$REPO >/dev/null
check "apps second run: ssh config identical"       [ "$(cat "$cfg")" = "$b_cfg" ]
check "apps second run: gitconfig identical"        [ "$(cat "$h/.gitconfig")" = "$b_git" ]
check "apps second run: settings identical"         [ "$(settings_of "$h")" = "$b_set" ]
check "apps second run: no file rewritten (mtime)"  [ "$(mtime "$cfg") $(mtime "$h/.gitconfig") $(mtime "$h/.claude/settings.json")" = "$m_cfg $m_git $m_set" ]

# sidecar and apps repo together: both profiles, talos first
h="$work/apps-talos"
put_key "$h" 0400
run_session "$h" IM_APPS_REPO=$REPO TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "talos+apps: both profiles"                   [ "$(allow_of "$h")" = "${PROFILE%]}, ${APPS#[}" ]

# sidecar without apps repo: no git/sops
h="$work/talos-only"
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check_not "talos only: no git entry" grep -q 'Bash(git' "$h/.claude/settings.json"

# key absent: nothing written, the key path named
h="$work/apps-nokey"
out=$(run_session "$h" IM_APPS_REPO=$REPO)
check_not "no key: no ssh config" test -e "$h/.ssh/config"
check_not "no key: no gitconfig" test -e "$h/.gitconfig"
check_not "no key: no permissions key" has_perms "$h"
check "no key: the line names the key path"         grep -q '寫入金鑰不在 ~/.ssh/im-apps.key' <<<"$out"

# key mode too open (owned by this user): nothing written, the mode named
h="$work/apps-0644"
put_key "$h" 0644
out=$(run_session "$h" IM_APPS_REPO=$REPO)
check_not "0644: no ssh config" test -e "$h/.ssh/config"
check_not "0644: no gitconfig" test -e "$h/.gitconfig"
check_not "0644: no permissions key" has_perms "$h"
check "0644: the line names the mode"               grep -q 'mode 0644' <<<"$out"

# key present but IM_APPS_REPO unset: silent, nothing written
h="$work/apps-norepo"
put_key "$h" 0600
out=$(run_session "$h")
check_not "no IM_APPS_REPO: no ssh config" test -e "$h/.ssh/config"
check_not "no IM_APPS_REPO: no gitconfig" test -e "$h/.gitconfig"
check_not "no IM_APPS_REPO: silent" grep -q 'apps repo' <<<"$out"

# not owner/repo: refused and said
h="$work/apps-badrepo"
put_key "$h" 0600
out=$(run_session "$h" IM_APPS_REPO='jg-x-apps')
check_not "bad IM_APPS_REPO: no ssh config" test -e "$h/.ssh/config"
check "bad IM_APPS_REPO: said"                      grep -q '不是 owner/repo' <<<"$out"

# an ssh config this script did not write is left alone
h="$work/apps-foreign"
put_key "$h" 0600
printf 'Host mine\n  HostName example.invalid\n' > "$h/.ssh/config"
out=$(run_session "$h" IM_APPS_REPO=$REPO)
check "foreign ssh config: untouched"               [ "$(cat "$h/.ssh/config")" = "$(printf 'Host mine\n  HostName example.invalid')" ]
check "foreign ssh config: said"                    grep -q '不是 claude-session 寫的' <<<"$out"
check_not "foreign ssh config: no gitconfig" test -e "$h/.gitconfig"

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
