Part of: `jg-base` / `im-onsite-operator-tools` —— sidecar 層的設計（憑證來源、隔離邊界、四層門檻、撤銷）在 root 的 `design.md`；這裡只決定這個 repo 的檔案怎麼寫。

## Context

`talos_mcp_server.py`（125 行，FastMCP，SSE 綁 `127.0.0.1:8765`）：一個 `_run(node, *args)` 包 `talosctl --talosconfig $TALOSCONFIG --nodes <node>`，四個 `@app.tool()` 唯讀工具，`MISSING_CONFIG` 在啟動時算一次。`claude-session` 在第一次登入時寫一份最小 `settings.json`，MCP 以 `claude mcp add --scope local` 註冊進 `~/.claude/.claude.json`。`app` 與 `talos-mcp` 同 pod、hostNetwork、同一個 ServiceAccount（cluster-admin），sidecar `drop: ALL` 但 `kubectl` 不需要 cap。

2026-10-11 量到的三件事決定了這裡的形狀：(1) im 的 Bash 分類器在 auto mode 下把 `talosctl` 整個擋掉、對 reboot 無提示自動拒絕，但 MCP 工具走 permission 規則；(2) `get_link_status(.23)` 在 SideroLink 斷線時 timeout——代理到不了；(3) im 自己判斷得出「csi-attacher 的 PDB 會擋 drain、graceful reboot 可以接受」，缺的只是一個能按的按鈕。

## Goals / Non-Goals

**Goals：** 第二把憑證缺席時零行為差異；讀取退路；一個 graceful reboot 工具，契約可測；permission 規則合併寫入；inbox 提示。
**Non-Goals：** drain／uncordon（`app` 的 kubectl 本來就能做，而且分類器放行）；shutdown；任何對 `TALOS_NODES` 的依賴；通知系統。

## Decisions

**1. 兩段式 confirm，token 由 server 發。**
`reboot_node(node, confirm="")`：`confirm` 空 → 回傳 `{token, node, expires_in: 120, will_run: "talosctl --endpoints N --nodes N reboot --wait"}` 並**不動作**；帶 token → 比對（同 node、未過期、未用過）→ 執行 → token 作廢。agent 必須把第一段的輸出給人看、人按 `ask` 提示、第二段才跑。
- *替代*：`confirm: bool`——LLM 會直接填 `true`，等於沒有門。
- *替代*：只靠 `permissions.ask`——提示在 manual／auto 兩種模式下行為不同（10-11 量到 auto 會倒數後自動拒絕），server 端的門不能依賴它。

**2. allowlist 在呼叫當下從 `kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type=="InternalIP")].address}'` 推。**
不碰 `TALOS_NODES`（insideman 4.7 裁定它不是邊界、而且會過期）。`kubectl` 失敗 → **fail closed**，拒絕並回傳錯誤原文。
- *替代*：從 operator talosconfig 的 `endpoints` 讀——那是公司鑄憑證時填的，不是叢集的真相。

**3. 自己坐的節點不帶 `--wait`。**
hostNetwork → `POD_IP`（downward API）＝ node IP。`node == POD_IP` 時指令不帶 `--wait`、回傳「已下令；本 pod 將隨節點重啟，重開後用 `get_node_status` 確認」。否則 `--wait`、timeout 600 秒（10-11 實測 22 秒 ＋ 1 分鐘回 Ready；單節點 Longhorn 叢集會更久）。

**4. audit 一行 JSON 到 stdout，含被拒的呼叫。**
`{"ts","tool":"reboot_node","node","phase":"token|execute|refused","reason","rc","user":$CLAUDE_USER_ID}`。sidecar 的 stdout 進 `kubectl logs -c talos-mcp`——這是 10-11 im 自己說它會去讀的地方（`x-sidero-signature` 那段），退路路徑沒有 Omni 稽核，這行是唯一的紀錄。

**5. 讀取退路：主路錯誤分類後只重試一次。**
`_run` 的 Omni 路徑回 timeout、或 stderr 含 `Unavailable`／`no route to host`／`context deadline` → 且 `OPERATOR_CONFIG` 在場 → `talosctl --talosconfig $OPERATOR --endpoints <node> --nodes <node> …` 一次；輸出首行 `[via direct LAN]`／`[via Omni]`。其他錯誤（`PermissionDenied`、參數錯）不重試——那些不是路的問題。

**6. 憑證到期檢查在啟動時做一次，寫成模組級常數，與 `MISSING_CONFIG` 同一個理由。**
`talosctl --talosconfig $OPERATOR config info` 解析 `Certificate expires`；過期 → 視為缺席並記原因；< 30 天 → 前綴警告。檔案 0 byte → 缺席（root spec 的「空字串與缺席同義」）。

**7. `claude-session` 用 `python3 -c` 合併 `permissions`，不用 `jq`（映像裡不一定有），不覆寫既有鍵。**
`allow += ["mcp__talos__get_node_status","mcp__talos__get_etcd_members","mcp__talos__get_link_status","mcp__talos__get_service_logs"]`、`ask += ["mcp__talos__reboot_node"]`，去重。只在 `TALOS_MCP_URL` 設定時做（與註冊同一個 gate）。

**8. inbox：`~/workspace/inbox/` 非空時印一行到 ttyd；不解析內容。**
`ls` 到任何非 `*.read` 的檔就印 `📥 inbox: N file(s) — read ~/workspace/inbox/ before anything else`。agent 讀完自己 `mv x x.read`。

## Risks / Trade-offs

- **[Risk] `kubectl` 在 sidecar 內要有 SA token** → automount 預設開、同 pod 同 SA；啟動時 `kubectl auth can-i list nodes` 一次，失敗記進 `MISSING_CONFIG` 類的啟動讀數（不擋唯讀工具）。
- **[Risk] token 存在記憶體，sidecar 重啟就失效** → 可接受，agent 重拿一次。
- **[Risk] 直連退路被誤用成「平時也直連」** → 只在主路失敗後、只重試一次、首行標記；測試裡斷言主路成功時 `[via Omni]`。
- **[Trade-off] `permissions.ask` 在 auto mode 的實際行為是否仍會倒數自動拒絕** → 本 repo 量不到，要在真 im 上量（root tasks 的 jg-jiahd 驗證那格）；若會，設計的底線仍是 server 端的兩段式 confirm，提示只是第四層。

## Migration Plan

1. 改 server 與測試（`test/` 下用假 `talosctl` shim 跑四種結果：主路成功／主路失敗退路成功／無 operator／token 流程）。
2. 改 `claude-session`；在本機容器跑一次確認 `settings.json` 合併正確且冪等。
3. `CLAUDE.md` env 表加 `TALOS_OPERATOR_CONFIG`、`POD_IP`；sidecar 段落改寫。
4. CI 重建、tag 回報給 root（jg-base／jg-cluster-template 的 HelmRelease 由那邊 bump）。
5. 回滾：env 不設即為舊行為；映像可直接退 tag。
