#!/usr/bin/env bash
#
# Controls for claude-session: the Talos permission profile, the apps-repo
# write path, the Home Assistant MCP registration, the netdev sidecar, and the inbox notice. The script is run AS SHIPPED, with HOME redirected to a scratch
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
env > "${FAKE_LOG%.log}.env"   # the environment claude would inherit
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
      TTYD_USER="" CLAUDE_USER_ID="tester" DATABASE_URL="" TALOS_MCP_URL="" IM_APPS_REPO="" IM_APPS_KEY="" HA_MCP_URL="" HA_MCP_TOKEN_FILE="" NETDEV_MCP_URL="" "$@" \
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

# key present but IM_APPS_REPO empty -- run_session passes IM_APPS_REPO="", the
# form jg-base's ${IM_APPS_REPO:=} substitution delivers: silent, nothing written
h="$work/apps-norepo"
put_key "$h" 0600
out=$(run_session "$h")
check_not "no IM_APPS_REPO: no ssh config" test -e "$h/.ssh/config"
check_not "no IM_APPS_REPO: no gitconfig" test -e "$h/.gitconfig"
check_not "no IM_APPS_REPO: silent" grep -q 'apps repo' <<<"$out"

# IM_APPS_KEY: the key mounted as a Secret directory outside ~/.ssh (jg-base#183)
h="$work/apps-keydir"
mkdir -p "$h/etc-im-apps"; printf 'not a real key\n' > "$h/etc-im-apps/key"; chmod 0400 "$h/etc-im-apps/key"
run_session "$h" IM_APPS_REPO=$REPO IM_APPS_KEY="$h/etc-im-apps/key" >/dev/null
check "IM_APPS_KEY: config names that key"          grep -qxF "  IdentityFile $h/etc-im-apps/key" "$h/.ssh/config"
check "IM_APPS_KEY: -F alias resolves it"           grep -qxF "$h/etc-im-apps/key" <<<"$(idfiles -F "$h/.ssh/config" github.com-im-apps)"
check_not "IM_APPS_KEY: the default path is not used" grep -qF '/.ssh/im-apps.key' "$h/.ssh/config"
# IM_APPS_KEY empty (substituted, not unset): the default path
h="$work/apps-keyempty"
put_key "$h" 0600
run_session "$h" IM_APPS_REPO=$REPO IM_APPS_KEY="" >/dev/null
check "IM_APPS_KEY empty: default path used"        grep -qxF "  IdentityFile $h/.ssh/im-apps.key" "$h/.ssh/config"
# IM_APPS_KEY names a missing file: nothing written, that path named
h="$work/apps-keymissing"
put_key "$h" 0600
out=$(run_session "$h" IM_APPS_REPO=$REPO IM_APPS_KEY="$h/etc-im-apps/key")
check_not "IM_APPS_KEY missing: no ssh config" test -e "$h/.ssh/config"
check "IM_APPS_KEY missing: that path named"        grep -q "寫入金鑰不在 ~/etc-im-apps/key" <<<"$out"

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

echo "home assistant"
HAURL=https://ha.homeauto.example.cc/api/mcp
TOK=HAtok3n-DUMMY-not-real-0001
# the entry claude-session wrote for this project (the script runs from the repo root)
ha_entry() { python3 - "$1" "$(pwd -P)" <<'PY2'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("null"); sys.exit()
print(json.dumps(d.get("projects", {}).get(sys.argv[2], {}).get("mcpServers", {}).get("ha"), sort_keys=True))
PY2
}
want_entry() { python3 -c 'import json,sys; print(json.dumps({"type":"http","url":sys.argv[1],"headers":{"Authorization":"Bearer "+sys.argv[2]}}, sort_keys=True))' "$1" "$2"; }
put_token() { mkdir -p "$1/etc-ha"; printf "$2" > "$1/etc-ha/token"; }
nwarn() { grep -c '^⚠ Home Assistant 沒有設定：' <<<"$1"; }
fmode() { python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "$1"; }

# URL empty (the form jg-base's substitution delivers) -- run_session passes "" -- and truly unset
h="$work/ha-empty"
put_token "$h" "$TOK"
out=$(run_session "$h" HA_MCP_TOKEN_FILE="$h/etc-ha/token")
check "empty URL: no ha entry"                      [ "$(ha_entry "$h/.claude.json")" = "null" ]
check_not "empty URL: no permissions key" has_perms "$h"
check_not "empty URL: silent" grep -q 'Home Assistant' <<<"$out"
h="$work/ha-unset"
mkdir -p "$h"; : > "$h/fake.log"
out=$(env -i PATH="$work/bin:$PATH" HOME="$h" FAKE_LOG="$h/fake.log" CLAUDE_USER_ID=tester bash ./claude-session </dev/null 2>&1)
check "unset URL: no ha entry"                      [ "$(ha_entry "$h/.claude.json")" = "null" ]
check_not "unset URL: no permissions key" has_perms "$h"
check_not "unset URL: silent" grep -q 'Home Assistant' <<<"$out"
check "unset URL: session still starts"             grep -q '^tmux ' "$h/fake.log"

# URL without the token: exactly one line, nothing written
h="$work/ha-notoken"
out=$(run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token")
check "no token: exactly one warning line"          [ "$(nwarn "$out")" = 1 ]
check_not "control: two warning lines are not one" [ "$(nwarn "$out"$'\n'"$out")" = 1 ]
check "no token: the line names the path"           grep -q '^⚠ Home Assistant 沒有設定：token 檔不在 ~/etc-ha/token$' <<<"$out"
check "no token: no ha entry"                       [ "$(ha_entry "$h/.claude.json")" = "null" ]
check_not "no token: no .claude.json written" test -e "$h/.claude.json"
check_not "no token: no permissions key" has_perms "$h"
h="$work/ha-zerobytes"
put_token "$h" ""
out=$(run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token")
check "0 bytes: exactly one warning line"           [ "$(nwarn "$out")" = 1 ]
check "0 bytes: the line says empty"                grep -q '^⚠ Home Assistant 沒有設定：token 檔 ~/etc-ha/token 是空的$' <<<"$out"
check_not "0 bytes: no .claude.json written" test -e "$h/.claude.json"
check_not "0 bytes: no permissions key" has_perms "$h"
# HA_MCP_TOKEN_FILE empty: the default path (absent on this machine)
h="$work/ha-defaultpath"
out=$(run_session "$h" HA_MCP_URL=$HAURL)
check "token file empty: default path named"        grep -q 'token 檔不在 /etc/ha-mcp/token$' <<<"$out"

# both present, token with a trailing newline
h="$work/ha"
put_token "$h" "$TOK\n"
out=$(run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token")
cj="$h/.claude.json"
check "ha: entry is the measured shape, newline stripped" [ "$(ha_entry "$cj")" = "$(want_entry "$HAURL" "$TOK")" ]
check_not "control: the entry check sees a wrong token" [ "$(ha_entry "$cj")" = "$(want_entry "$HAURL" "${TOK}x")" ]
check "ha: .claude.json is 0600"                    [ "$(fmode "$cj")" = 0o600 ]
check "ha: allow gains only mcp__ha"                [ "$(allow_of "$h")" = '["mcp__ha"]' ]
check_not "ha: silent when all is well" grep -q 'Home Assistant' <<<"$out"
check_not "ha: claude never called for ha (no argv)" grep -q 'mcp .*ha' "$h/fake.log"
# no trace of the token: output, environment, argv -- and the same grep finds it where it belongs
check_not "ha: token prefix not in output" grep -qF "${TOK:0:8}" <<<"$out"
check_not "ha: token prefix not in claude's env" grep -qF "${TOK:0:8}" "$h/fake.env"
check_not "ha: token prefix not in any argv" grep -qF "${TOK:0:8}" "$h/fake.log"
check "control: HA_MCP_URL is in claude's env"      grep -qF "HA_MCP_URL=$HAURL" "$h/fake.env"
check "control: the token is in .claude.json"       grep -qF "${TOK:0:8}" "$cj"
# second run: nothing moves
b_cj=$(cat "$cj"); b_set=$(settings_of "$h"); m_cj=$(mtime "$cj"); m_set=$(mtime "$h/.claude/settings.json")
sleep 1
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "ha second run: .claude.json identical"       [ "$(cat "$cj")" = "$b_cj" ]
check "ha second run: settings identical"           [ "$(settings_of "$h")" = "$b_set" ]
check "ha second run: no file rewritten (mtime)"    [ "$(mtime "$cj") $(mtime "$h/.claude/settings.json")" = "$m_cj $m_set" ]
# rotation: rewritten once with the new bearer, then still
put_token "$h" "${TOK}-rotated"
sleep 1
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "rotated: entry carries the new token"        [ "$(ha_entry "$cj")" = "$(want_entry "$HAURL" "${TOK}-rotated")" ]
check_not "rotated: the file did move" [ "$(mtime "$cj")" = "$m_cj" ]
m_rot=$(mtime "$cj"); sleep 1
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "rotated, third run: not rewritten (mtime)"   [ "$(mtime "$cj")" = "$m_rot" ]
# an entry removed by hand comes back on the next run
python3 - "$cj" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
for p in d["projects"].values(): p["mcpServers"].pop("ha", None)
json.dump(d, open(sys.argv[1], "w"))
PY2
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "removed by hand: re-registered"              [ "$(ha_entry "$cj")" = "$(want_entry "$HAURL" "${TOK}-rotated")" ]

# CLAUDE_CONFIG_DIR (jg-base sets it): the file lands there, not in HOME
h="$work/ha-configdir"
put_token "$h" "$TOK"
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" CLAUDE_CONFIG_DIR="$h/.claude" >/dev/null
check "CLAUDE_CONFIG_DIR: entry in that dir"        [ "$(ha_entry "$h/.claude/.claude.json")" = "$(want_entry "$HAURL" "$TOK")" ]
check_not "CLAUDE_CONFIG_DIR: nothing in HOME" test -e "$h/.claude.json"

# existing file: other keys, projects and servers survive
h="$work/ha-existing"
put_token "$h" "$TOK"
printf '{"userID":"u1","projects":{"/elsewhere":{"mcpServers":{"x":{"type":"stdio"}}},"%s":{"allowedTools":[],"mcpServers":{"talos":{"type":"sse","url":"http://127.0.0.1:8765/sse"}}}}}\n' "$(pwd -P)" > "$h/.claude.json"
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "existing: ha added"                          [ "$(ha_entry "$h/.claude.json")" = "$(want_entry "$HAURL" "$TOK")" ]
check "existing: other keys kept"                   python3 - "$h/.claude.json" "$(pwd -P)" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1])); p = d["projects"][sys.argv[2]]
sys.exit(0 if d["userID"] == "u1" and "x" in d["projects"]["/elsewhere"]["mcpServers"]
         and "talos" in p["mcpServers"] and p["allowedTools"] == [] else 1)
PY2

# unparseable .claude.json: left alone, one line, no permission
h="$work/ha-broken"
put_token "$h" "$TOK"
printf '{"projects": {' > "$h/.claude.json"
out=$(run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token")
check "broken .claude.json: byte-identical"         [ "$(cat "$h/.claude.json")" = '{"projects": {' ]
check "broken .claude.json: exactly one line"       [ "$(nwarn "$out")" = 1 ]
check "broken .claude.json: says why"               grep -q '不是合法 JSON，沒有改它' <<<"$out"
check_not "broken .claude.json: no permissions key" has_perms "$h"
check "broken .claude.json: session still starts"   grep -q '^tmux ' "$h/fake.log"

# independent of the other two profiles
h="$work/ha-talos"
put_token "$h" "$TOK"
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "talos+ha: talos profile then mcp__ha"        [ "$(allow_of "$h")" = "${PROFILE%]}, \"mcp__ha\"]" ]
h="$work/ha-apps"
put_token "$h" "$TOK"; put_key "$h" 0600
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" IM_APPS_REPO=$REPO >/dev/null
check "ha+apps: mcp__ha then git and sops"          [ "$(allow_of "$h")" = "[\"mcp__ha\", ${APPS#[}" ]
h="$work/ha-off-talos"
run_session "$h" TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check_not "talos without HA: no mcp__ha" grep -q 'mcp__ha' "$h/.claude/settings.json"

# transport: streamable HTTP (HA's /api/mcp). A pod that ran the sse image is rewritten once.
h="$work/ha-transport"
put_token "$h" "$TOK"
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "transport: entry type is literally http"     python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["type"] == "http" else 1)' "$(ha_entry "$h/.claude.json")"
python3 - "$h/.claude.json" "$(pwd -P)" <<'PY3'
import json, sys
d = json.load(open(sys.argv[1])); d["projects"][sys.argv[2]]["mcpServers"]["ha"]["type"] = "sse"
open(sys.argv[1], "w").write(json.dumps(d))
PY3
check_not "control: the sse entry reads as sse" [ "$(ha_entry "$h/.claude.json")" = "$(want_entry "$HAURL" "$TOK")" ]
run_session "$h" HA_MCP_URL=$HAURL HA_MCP_TOKEN_FILE="$h/etc-ha/token" >/dev/null
check "transport: an sse entry from the old image is rewritten to http" [ "$(ha_entry "$h/.claude.json")" = "$(want_entry "$HAURL" "$TOK")" ]

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

echo "netdev sidecar"
NDURL=http://127.0.0.1:8766/sse
h="$work/netdev"
run_session "$h" NETDEV_MCP_URL=$NDURL >/dev/null
check "netdev: registered as sse, no header"        grep -qx "claude mcp add --transport sse --scope local netdev $NDURL" "$h/fake.log"
check "netdev: allow is exactly mcp__netdev"        [ "$(allow_of "$h")" = '["mcp__netdev"]' ]
check_not "netdev: no talos entry"                  grep -q 'mcp__talos' "$h/.claude/settings.json"
check_not "netdev: no header on the add line"       grep -q 'netdev.*--header' "$h/fake.log"
before=$(mtime_of "$h"); before_body=$(settings_of "$h")
run_session "$h" NETDEV_MCP_URL=$NDURL >/dev/null
check "netdev: second run leaves settings untouched" [ "$(mtime_of "$h")" = "$before" ]
check "netdev: second run byte-identical"           [ "$(settings_of "$h")" = "$before_body" ]
for v in unset empty; do
  h="$work/netdev-$v"
  if [ "$v" = unset ]; then run_session "$h" >/dev/null; else run_session "$h" NETDEV_MCP_URL= >/dev/null; fi
  check_not "netdev $v: not registered"             grep -q 'mcp add.*netdev' "$h/fake.log"
  check_not "netdev $v: no mcp__netdev"             grep -q 'mcp__netdev' "$h/.claude/settings.json"
  check "netdev $v: session still starts"           grep -q '^tmux ' "$h/fake.log"
done
h="$work/netdev-talos"
run_session "$h" NETDEV_MCP_URL=$NDURL TALOS_MCP_URL=http://127.0.0.1:8765/sse >/dev/null
check "talos+netdev: talos profile then mcp__netdev" [ "$(allow_of "$h")" = "${PROFILE%]}, \"mcp__netdev\"]" ]
check "talos+netdev: both registered"               [ "$(grep -c 'mcp add --transport sse' "$h/fake.log")" -eq 2 ]

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
