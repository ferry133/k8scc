#!/usr/bin/env bash
#
# Controls for the managed policy im reads inside the pod (root 7.3b, ferry133
# ruling 2026-10-15): the file is shipped to the path Claude Code loads first
# for every user, and it says the three things the ruling requires. Each
# reading has a control that can fail, otherwise 0 failures cannot distinguish
# "it says so" from "grep cannot see".
#
# Run: test/im-policy-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

pass=0
fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local desc="$1"; shift; if "$@"; then ok; else bad "$desc"; fi; }
check_not() { local desc="$1"; shift; if "$@"; then bad "$desc"; else ok; fi; }

policy=im-policy.md

echo "shipped"
check "policy file exists and is not empty"           test -s "$policy"
check "Dockerfile copies it to the managed path"      grep -qE '^COPY im-policy\.md /etc/claude-code/CLAUDE\.md$' Dockerfile
check_not "control: a path nobody ships"              grep -qE '^COPY im-policy\.md /etc/claude-code/nothing$' Dockerfile
check_not "repo copy is not itself named CLAUDE.md"   test -e managed-policy/CLAUDE.md

echo "says the three things"
check "reboot is within im's authority"               grep -q '在你的權限內' "$policy"
check "no human confirmation is needed"               grep -q '不需要任何人在終端機按確認' "$policy"
check "names the server-side gates: reason"           grep -q '必填 `reason`' "$policy"
check "names the server-side gates: quorum"           grep -q 'quorum' "$policy"
check "names the server-side gates: cooldown"         grep -q '600 秒冷卻' "$policy"
check "names the server-side gates: two-phase token"  grep -q '兩段式 token' "$policy"
check "reports afterwards via the inbox"              grep -q 'workspace/inbox/' "$policy"
check "reports afterwards via the health mail"        grep -q 'health mail' "$policy"
check "only company staff sit at the ttyd"            grep -q '只會是公司員工' "$policy"
check_not "control: a sentence that is not there"     grep -q 'reboot is forbidden' "$policy"

echo "old premise absent"
check_not "no customer-approval premise"              grep -qiE 'approval from the customer|person at the terminal|客戶.{0,6}(按|批准)' "$policy"
check_not "no permissions.ask wording"                grep -q 'permissions.ask' "$policy"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
