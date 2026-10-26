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
# source_first <file>: annotate source -> wait source -> annotate
# Kustomization -> wait Kustomization, the order measured on jg-jcc2
# (k8scc#39), so the reconcile im asks for is one that already sees the new
# commit. Whether another order converges later on its own (the Kustomization
# watching its source) is not measured; this only pins the order the policy
# asks for.
source_first() {
  awk '/annotate gitrepository tenant-im-apps/ && !a {a = NR}
       /wait gitrepository\/tenant-im-apps/      && !b {b = NR}
       /annotate kustomization tenant-im-apps/ && !c {c = NR}
       /wait kustomization\/tenant-im-apps/      && !d {d = NR}
       END {exit !(a && b && c && d && a < b && b < c && c < d)}' "$1"
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
  grep -q "wait kustomization/tenant-im-apps --for=jsonpath='{.status.lastAppliedRevision}'" "$1" &&
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
no_wait=$(mktemp)
trap 'rm -f "$apps" "$noapps" "$kust_only" "$swapped" "$no_wait"' EXIT
grep -v 'wait kustomization/tenant-im-apps' "$apps" > "$no_wait"
check_not "control: annotate without waiting fails"    apps_checks "$no_wait"
# the source wait moved after the Kustomization annotate (the order at 0cdb820)
late_wait=$(mktemp)
trap 'rm -f "$apps" "$noapps" "$kust_only" "$swapped" "$no_wait" "$late_wait"' EXIT
awk '/wait gitrepository\/tenant-im-apps/ {held = $0; next}
     {print} /annotate kustomization tenant-im-apps/ && held != "" {print held}' "$apps" > "$late_wait"
check_not "control: source wait after the ks annotate fails" apps_checks "$late_wait"
check "control: the late-wait copy kept the source wait" grep -q 'wait gitrepository/tenant-im-apps' "$late_wait"
check "control: the swapped copy kept both lines"     [ "$(grep -cE 'annotate (gitrepository|kustomization) tenant-im-apps' "$swapped")" -eq 2 ]

echo "home automation section"
ha_section() { awk '/^## /{on = ($0 == "## 你的家庭自動化")} on' "$1"; }
# one pattern per statement; each is on its own line, so dropping that line
# drops exactly that statement
HA_STATEMENTS=(
  '只在 `HA_MCP_URL` 有值'
  '沒有 Home Assistant 的叢集，這一節不適用'
  '操作被 expose 的實體、建立／修改／刪除 HA 的自動化與場景'
  '依你的判斷做，不需要任何人在終端機按確認'
  '在 `~/workspace/inbox/` 留一份有日期的報告'
  '不要跑 `claude mcp get ha`，也不要讀或印出 `.claude.json` 裡的 `ha` 項'
  '`claude mcp list` 會不會印沒量到，當作會印、一樣不跑'
  '要確認 HA 連不連得上，呼叫一支 `mcp__ha__*` 工具'
  '自動化只寫在 HA'
  '不啟用、也不配對 HA 的 HomeKit Bridge'
  '不綁定或解除 Google Assistant 的帳號連結'
  '不新增或刪除 HA 的使用者與整合'
  '不升級 Home Assistant 本身'
  '不碰 homebridge'
)
ha_checks() { # ha_checks <file> -> 0 when every statement is there
  local pat
  for pat in "${HA_STATEMENTS[@]}"; do grep -qF -- "$pat" "$1" || return 1; done
}
ha=$(mktemp); noha=$(mktemp); drop=$(mktemp)
trap 'rm -f "$apps" "$noapps" "$kust_only" "$swapped" "$no_wait" "$late_wait" "$ha" "$noha" "$drop"' EXIT
ha_section "$policy" > "$ha"
check "section carries every statement"               ha_checks "$ha"
awk '/^## /{on = ($0 != "## 你的家庭自動化")} NR==1 || on' "$policy" > "$noha"
check_not "control: policy without the section fails" ha_checks "$noha"
check "control: the stripped copy kept the rest"      grep -q '## 你的 apps repo' "$noha"
# each statement removed on its own must turn the check red
for pat in "${HA_STATEMENTS[@]}"; do
  grep -vF -- "$pat" "$ha" > "$drop"
  check_not "control: without '$pat' fails" ha_checks "$drop"
  check "control: dropping '$pat' removed one line"   [ "$(( $(wc -l < "$ha") - $(wc -l < "$drop") ))" -eq 1 ]
done
# the token sentence: no wording elsewhere that sends im to `mcp get`/`mcp list` to check HA
check_not "no wording that checks HA with mcp get/list" grep -qE '(用|跑|執行) ?`?claude mcp (get|list)`?.{0,12}(確認|檢查|看)' "$policy"
check "control: the mcp get/list grep can match"   grep -qE '(用|跑|執行) ?`?claude mcp (get|list)`?.{0,12}(確認|檢查|看)' <<<'先跑 claude mcp get ha 確認狀態'
check_not "no wording that re-pairs for the family"   grep -qE '替家人重新配對' "$policy"
check_not "no wording that turns homebridge off"      grep -qE '(關掉|停用|關閉|停止) ?homebridge' "$policy"
check "control: the homebridge grep can match"        grep -qE '(關掉|停用|關閉|停止) ?homebridge' <<<'請停用 homebridge'

echo "router section"
nd_section() { awk '/^## /{on = ($0 == "## 你的路由器")} on' "$1"; }
ND_STATEMENTS=(
  '只在 `mcp__netdev__*` 工具存在時適用'
  '沒有 netdev sidecar 的叢集，這一節不適用'
  '憑證只在 sidecar 裡，你這邊沒有'
  '能寫的只有八支工具'
  '不要自己找路去改'
  '寫入由你自己判斷，事後報告'
  '不論結果是套用、還原還是中止'
  '先讀再寫'
  '`will_change` 是你自己的第二眼'
  '不要跳過 `will_change` 直接帶 confirm'
  '被拒就照訊息處理，不要繞'
  '收到 `result: revert-failed`，第一件事是照回應裡的 `inbox` 指令寫報告'
  '重開設備和 PoE 斷電沒有還原'
  '不要用 `curl` 或任何別的方式直接打 controller'
  '不要想辦法還原它'
  '清冊寫在 `~/workspace/lan-inventory.yaml`，不進任何 repo'
  '每一列註明來源'
  '讀到 `error:` 就照實回報，不要拿舊資料代替'
)
nd_checks() { # nd_checks <file> -> 0 when every statement is there
  local pat
  for pat in "${ND_STATEMENTS[@]}"; do grep -qF -- "$pat" "$1" || return 1; done
}
nd=$(mktemp); nond=$(mktemp); nddrop=$(mktemp)
trap 'rm -f "$apps" "$noapps" "$kust_only" "$swapped" "$no_wait" "$late_wait" "$ha" "$noha" "$drop" "$nd" "$nond" "$nddrop"' EXIT
nd_section "$policy" > "$nd"
check "section carries every statement"               nd_checks "$nd"
awk '/^## /{on = ($0 != "## 你的路由器")} NR==1 || on' "$policy" > "$nond"
check_not "control: policy without the section fails" nd_checks "$nond"
check "control: the stripped copy kept the rest"      grep -q '## 你的家庭自動化' "$nond"
for pat in "${ND_STATEMENTS[@]}"; do
  grep -vF -- "$pat" "$nd" > "$nddrop"
  check_not "control: without '$pat' fails" nd_checks "$nddrop"
  check "control: dropping '$pat' removed one line"   [ "$(( $(wc -l < "$nd") - $(wc -l < "$nddrop") ))" -eq 1 ]
done
# the prohibition itself names curl and the controller, so lines saying 不要 are left out
direct_curl() { grep -vF '不要' | grep -qE 'curl.{0,20}(controller|/proxy/network).{0,20}(確認|讀|看)'; }
check_not "no wording that sends im to the controller directly" direct_curl < "$policy"
check "control: the direct-controller grep can match" direct_curl <<<'用 curl 打 controller 確認'
# the prohibition names will_change and confirm itself, so lines saying 不要 are left out
skip_look() { grep -vF '不要' | grep -qE '(跳過|略過|不必看|不用看).{0,12}will_change'; }
check_not "no wording that skips will_change"         skip_look < "$policy"
check "control: the skip-will_change grep can match"  skip_look <<<'可以略過 will_change 直接 confirm'
check_not "no wording that commits the inventory"     grep -qE 'lan-inventory.{0,30}(commit|推上|進 repo)' "$policy"

echo "old premise absent"
check_not "no customer-approval premise"              grep -qiE 'approval from the customer|person at the terminal|客戶.{0,6}(按|批准)' "$policy"
check_not "no permissions.ask wording"                grep -q 'permissions.ask' "$policy"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
