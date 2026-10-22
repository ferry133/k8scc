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

echo "apps repo section"
# section <file>: the "## 你的 apps repo" section alone, so a phrase elsewhere
# in the file cannot satisfy a check meant for this section
section() { awk '/^## /{on = ($0 == "## 你的 apps repo")} on' "$1"; }
apps=$(mktemp); noapps=$(mktemp)
trap 'rm -f "$apps" "$noapps"' EXIT
section "$policy" > "$apps"
# source_first <file>: the GitRepository annotate comes before the
# Kustomization one -- the other order re-applies the old artifact (k8scc#39)
source_first() {
  awk '/annotate gitrepository tenant-im-apps/ && !g {g = NR}
       /annotate kustomization tenant-im-apps/ && !k {k = NR}
       END {exit !(g && k && g < k)}' "$1"
}
apps_checks() { # apps_checks <file> -> 0 when every statement is there
  grep -q '只在 `IM_APPS_REPO` 有值' "$1" &&
  grep -q '這一節不適用' "$1" &&
  grep -q '只放 manifest、kustomization 與 README' "$1" &&
  grep -q '以 tenant 公鑰加密' "$1" &&
  grep -q 'sops --encrypt --in-place' "$1" &&
  grep -q '不含 log、IP 位址' "$1" &&
  grep -q 'annotate gitrepository tenant-im-apps reconcile.fluxcd.io/requestedAt=.*--overwrite' "$1" &&
  grep -q 'annotate kustomization tenant-im-apps reconcile.fluxcd.io/requestedAt=.*--overwrite' "$1" &&
  source_first "$1" &&
  grep -q '要留下來的東西走 repo，不走 `kubectl apply`' "$1" &&
  grep -q 'git@github.com-im-apps:\$IM_APPS_REPO.git' "$1"
}
check "section carries every statement"               apps_checks "$apps"
# control: the policy as it stood before this section (the section stripped)
awk '/^## /{on = ($0 != "## 你的 apps repo")} NR==1 || on' "$policy" > "$noapps"
check_not "control: policy without the section fails" apps_checks "$noapps"
check "control: the stripped copy kept the rest"      grep -q '## inbox 慣例' "$noapps"
check_not "no wording that permits a plaintext Secret" grep -qiE '明文.{0,8}(可以|即可|沒關係)|plaintext secrets? (is|are) (fine|ok)' "$policy"
check_not "no wording that asks for logs in commits"   grep -qiE '(貼|附|放).{0,6}log.{0,8}(commit|repo)|commit.{0,10}(附上|貼上).{0,4}log' "$policy"
check_not "control: a statement that is not there"     grep -q '把 age 私鑰放進 repo' "$apps"
# controls for k8scc#39: the Kustomization-only line, and the two steps swapped
kust_only=$(mktemp); swapped=$(mktemp)
trap 'rm -f "$apps" "$noapps" "$kust_only" "$swapped"' EXIT
grep -v 'annotate gitrepository tenant-im-apps' "$apps" > "$kust_only"
awk '/annotate gitrepository tenant-im-apps/ {held = $0; next}
     {print} /annotate kustomization tenant-im-apps/ && held != "" {print held}' "$apps" > "$swapped"
check_not "control: Kustomization-only reconcile fails" apps_checks "$kust_only"
check_not "control: Kustomization before source fails"  apps_checks "$swapped"
check "control: the swapped copy kept both lines"     [ "$(grep -cE 'annotate (gitrepository|kustomization) tenant-im-apps' "$swapped")" -eq 2 ]

echo "old premise absent"
check_not "no customer-approval premise"              grep -qiE 'approval from the customer|person at the terminal|客戶.{0,6}(按|批准)' "$policy"
check_not "no permissions.ask wording"                grep -q 'permissions.ask' "$policy"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
