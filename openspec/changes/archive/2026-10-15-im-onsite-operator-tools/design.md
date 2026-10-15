Part of: `jg-base` / `im-onsite-operator-tools` —— sidecar 層的設計（憑證來源、隔離邊界、四層門檻、撤銷）在 root 的 `design.md`；這裡只決定這個 repo 的檔案怎麼寫。

## Context

`talos_mcp_server.py`（125 行，FastMCP，SSE 綁 `127.0.0.1:8765`）：一個 `_run(node, *args)` 包 `talosctl --talosconfig $TALOSCONFIG --nodes <node>`，四個 `@app.tool()` 唯讀工具，`MISSING_CONFIG` 在啟動時算一次。`claude-session` 在第一次登入時寫一份最小 `settings.json`，MCP 以 `claude mcp add --scope local` 註冊進 `~/.claude/.claude.json`。`app` 與 `talos-mcp` 同 pod、hostNetwork、同一個 ServiceAccount（cluster-admin），sidecar `drop: ALL` 但 `kubectl` 不需要 cap。

2026-10-11 量到的三件事決定了這裡的形狀：(1) im 的 Bash 分類器在 auto mode 下把 `talosctl` 整個擋掉、對 reboot 無提示自動拒絕，但 MCP 工具走 permission 規則；(2) `get_link_status(.23)` 在 SideroLink 斷線時 timeout——代理到不了；(3) im 自己判斷得出「csi-attacher 的 PDB 會擋 drain、graceful reboot 可以接受」，缺的只是一個能按的按鈕。

## Goals / Non-Goals

**Goals：** 第二把憑證缺席時零行為差異；讀取退路；一個 graceful reboot 工具，契約可測；permission 規則合併寫入；inbox 提示。
**Non-Goals：** drain／uncordon（`app` 的 kubectl 本來就能做，而且分類器放行）；shutdown；任何對 `TALOS_NODES` 的依賴；通知系統。

## Decisions

**1. 兩段式 confirm，token 由 server 發。**
`reboot_node(node, reason, confirm="")`：`confirm` 空 → 回傳 `{token, node, expires_in: 120, cooldown_after: 600, will_run: "talosctl --endpoints N --nodes N reboot --wait=true --timeout=600s"}` 並**不動作**；帶 token → 比對（同 node、未過期、未用過）→ 執行 → token 作廢。第一段是 agent 自己重讀 `will_run` 與 reason 的第二眼——2026-10-15 裁定後沒有人按提示（原設計「給人看、人按 `ask`」作廢，見第 9 條）。
- *替代*：`confirm: bool`——LLM 會直接填 `true`，等於沒有門。
- *替代*：只靠 `permissions.ask`——提示在 manual／auto 兩種模式下行為不同（10-11 量到 auto 會倒數後自動拒絕），server 端的門不能依賴它。（2026-10-15 之後反過來也成立：門在伺服端，所以 `ask` 不再需要。）

**2. allowlist 在呼叫當下從 `kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type=="InternalIP")].address}'` 推。**
不碰 `TALOS_NODES`（insideman 4.7 裁定它不是邊界、而且會過期）。`kubectl` 失敗 → **fail closed**，拒絕並回傳錯誤原文。
- *替代*：從 operator talosconfig 的 `endpoints` 讀——那是公司鑄憑證時填的，不是叢集的真相。

**3. 自己坐的節點 `--wait=false`。**
hostNetwork → `POD_IP`（downward API）＝ node IP。`node == POD_IP` 時指令帶 `--wait=false`、回傳「已下令；本 pod 將隨節點重啟，重開後用 `get_node_status` 確認」。否則 `--wait=true --timeout=600s`（10-11 實測 22 秒 ＋ 1 分鐘回 Ready；單節點 Longhorn 叢集會更久）。
- ⚠️ **2026-10-13 實作時量到：`talosctl reboot --wait` 預設是 true**（talosctl 1.12 `--help`），所以 proposal 原本寫的「不帶 `--wait`」照字面做會等自己死掉——必須明寫 `--wait=false`。spec 已同步改。

**4. audit 一行 JSON 到 stdout，含被拒的呼叫。**
`{"ts","tool":"reboot_node","node","phase":"token|execute|refused","reason","rc","user":$CLAUDE_USER_ID}`。sidecar 的 stdout 進 `kubectl logs -c talos-mcp`——這是 10-11 im 自己說它會去讀的地方（`x-sidero-signature` 那段），退路路徑沒有 Omni 稽核，這行是唯一的紀錄。

**5. 讀取退路：主路錯誤分類後只重試一次。**
`_run` 的 Omni 路徑回 timeout、或 stderr 含 `Unavailable`／`no route to host`／`context deadline` → 且 `OPERATOR_CONFIG` 在場 → `talosctl --talosconfig $OPERATOR --endpoints <node> --nodes <node> …` 一次；輸出首行 `[via direct LAN]`／`[via Omni]`。其他錯誤（`PermissionDenied`、參數錯）不重試——那些不是路的問題。

**6. 憑證到期檢查在啟動時做一次，寫成模組級常數，與 `MISSING_CONFIG` 同一個理由。**
`talosctl --talosconfig $OPERATOR config info` 解析 `Certificate expires: … (YYYY-MM-DD)`（格式以合成 talosconfig 量到）；到期日 ≤ 今天 → 視為缺席並記日期；< 30 天 → 前綴警告。檔案 0 byte → 缺席（root spec 的「空字串與缺席同義」）。**讀不到到期日（`config info` 失敗或沒有那一行）→ 也視為缺席，理由帶 talosctl 的錯誤**——fail closed；一把「到期日不明」的憑證若照用，會一直好用到某天無聲失效。

**7. `claude-session` 用 `python3 -` 合併 `permissions`，不用 `jq`（映像裡不一定有），不覆寫既有鍵。**
`allow += ["mcp__talos__get_node_status","mcp__talos__get_etcd_members","mcp__talos__get_link_status","mcp__talos__get_service_logs","mcp__talos__reboot_node"]`，去重、保序；`ask` 裡若有 `mcp__talos__reboot_node`（前一個映像寫的，PVC 上留著）就拿掉，其他 `ask` 項不動——同一個名字同時在兩張表上，權限層怎麼解是它的事，不留這個問題。2026-10-15 之前這裡是 `ask += ["mcp__talos__reboot_node"]`。只在 `TALOS_MCP_URL` 設定時做（與註冊同一個 gate）。結果與原檔逐位元相同就不寫（第二次跑連 mtime 都不動）；解析不了的檔**原樣留著**並在終端機說一句——靜默蓋掉客戶的設定與靜默失敗在紀錄上長得一樣。

**8. inbox：`~/workspace/inbox/` 非空時印一行到 ttyd；不解析內容。**
`find` 到任何非 `*.read` 的檔就印 `📥 inbox: N file(s) — read ~/workspace/inbox/ before anything else`。agent 讀完自己 `mv x x.read`。
- 那一行後面接一個 8 秒、按 Enter 可跳過的停頓：`tmux` 一 attach 就清螢幕，印了只亮一格的字與沒印在人眼裡是同一件事。只在有未讀檔時停。

**9. 守衛在伺服端、兩段都查（2026-10-15 裁定後，root 7.2）。**
同一次 `kubectl get nodes -o json` 同時供 allowlist 與 quorum：其他 control-plane（label `node-role.kubernetes.io/control-plane`）任一 `Ready≠True` 就拒、點名那台；目標自己不 Ready 不算（那通常正是要重啟的理由）；worker 不算。冷卻 600 秒＝一個 `--wait` 視窗，從下令那刻起算，存記憶體（sidecar 重啟歸零）；**自身節點改從它上次開機起算**（`/proc/uptime`，不受 namespace 隔離、讀到的是 host 的開機秒數）——自身節點重啟一定連 sidecar 一起重啟，記憶體裡的紀錄活不過它要記的那件事（fleet-ops-77 2026-10-15 在拋棄式三 CP 量到）；任何一次開機都算，讀不到就拒（fail closed）；數字寫進 token 回應（`cooldown_after`）與拒絕訊息。`reason` 必填，在 MCP schema 層就是 required；空字串在任何查詢之前拒；寫進每一行 audit 的 `stated_reason`——與 refused 行既有的 `reason`（拒絕原因）是兩個欄位，不混用。自身節點時兩段回應都明說 im 隨之停擺、單節點叢集等於整個服務、回來前無人能看結果。
- *替代*：quorum 只在目標是 control-plane 時查——root 7.2 的字面是「另一台控制平面不 Ready 就拒」，不分目標；叢集已降級時多一次重啟是風險不是修復。
- *替代*：冷卻存到 PVC／emptyDir 或 node annotation——前者要 jg-base 加掛載、而 emptyDir 能否活過節點重啟沒量過；後者要新增 `patch nodes` RBAC。開機時鐘不用新權限、不用新掛載，而且量的正是要防的那件事（節點剛起來）。

## Risks / Trade-offs

- **[Risk] `kubectl` 在 sidecar 內要有 SA token** → automount 預設開、同 pod 同 SA；啟動時 `kubectl auth can-i list nodes` 一次，失敗記進 `MISSING_CONFIG` 類的啟動讀數（不擋唯讀工具）。
- **[Risk] token 存在記憶體，sidecar 重啟就失效** → 可接受，agent 重拿一次。
- **[Risk] 其他節點的冷卻只在記憶體** → sidecar 因別的原因重啟（換映像、被重排）會歸零；audit 看得見（兩行 `execute` 相隔 < 600 s），quorum 守衛仍在。自身節點已改用開機時鐘，不在此列。
- **[Risk] 自身節點開機後 600 秒內一律拒** → 包括不是 im 下令的開機（停電回來）。這是刻意的：剛起來的節點再被重啟，正是冷卻要擋的迴圈。
- **[Risk] 直連退路被誤用成「平時也直連」** → 只在主路失敗後、只重試一次、首行標記；測試裡斷言主路成功時 `[via Omni]`。
- **[Trade-off] `permissions.ask` 在 auto mode 的實際行為** → 2026-10-15 之後與本設計無關（reboot 在 `allow`）；root 5.6 的讀數只剩歷史價值。設計的底線一直是 server 端：兩段式 token 加第 9 條的守衛。

## Migration Plan

1. 改 server 與測試（`test/` 下用假 `talosctl`／`kubectl` shim；每個案例配一個負對照；2026-10-13 另以三個故意的突變——永不退路／token 不作廢／自己的節點照等——確認測試會轉紅）。
2. 改 `claude-session`；在本機容器跑一次確認 `settings.json` 合併正確且冪等。
3. `CLAUDE.md` env 表加 `TALOS_OPERATOR_CONFIG`、`POD_IP`；sidecar 段落改寫。
4. CI 重建、tag 回報給 root（jg-base／jg-cluster-template 的 HelmRelease 由那邊 bump）。
5. 回滾：env 不設即為舊行為；映像可直接退 tag。
