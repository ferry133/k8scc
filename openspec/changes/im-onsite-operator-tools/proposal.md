Part of: `jg-base` / `im-onsite-operator-tools`

**Owning repo**：`k8scc` —— 本 sub-change 改的檔案（`talos_mcp_server.py`、`claude-session`、`Dockerfile`、`CLAUDE.md`）全在這裡；root change 住在 `jg-base`，持有 sidecar 層的 spec 與 Secret／HelmRelease 的變動。

## Why

Root change 的 Why 不在這裡複述（見 `jg-base/openspec/changes/im-onsite-operator-tools/proposal.md`）。這個 repo 的份額是三件可以獨立驗證的事：**MCP server 要長出受門檻的 mutating 工具與直連 LAN 的讀取退路**、**`claude-session` 要讓 agent 的權限層跟著工具面走**（唯讀自動放行、reboot 一律提示、絕不無提示自動拒絕）、以及**一個公司可以寫、im 會讀的交辦信箱**。2026-10-11 這三件各缺一塊，im 診斷對了卻動不了。

## What Changes

- `talos_mcp_server.py`：
  - 讀第二個環境變數 `TALOS_OPERATOR_CONFIG`（檔案路徑，直連 LAN 的 `os:operator` talosconfig）。不在場時行為與今天完全相同。
  - 四個 `get_*` 工具：Omni 代理路徑失敗（timeout／`Unavailable`／`no route`）且 operator 憑證在場時，以 `--endpoints <node>` 直連重試一次；回傳開頭標 `[via direct LAN]` 或 `[via Omni]`。
  - 新工具 `reboot_node(node, confirm="")`：`confirm` 留空 → 回傳一次性 token（含 node、到期 120 秒）與將執行的指令，**不動作**；帶正確 token → `talosctl --endpoints <node> --nodes <node> reboot --wait`；allowlist 由 `kubectl get nodes -o jsonpath` 的 InternalIP 在呼叫當下取得（不依賴 `TALOS_NODES`）；每次呼叫（含被拒）寫一行結構化 audit 到 stdout。只在 operator 憑證在場時註冊。
- `claude-session`：初始化 `settings.json` 時加 `permissions.allow: ["mcp__talos__get_node_status", …]`、`permissions.ask: ["mcp__talos__reboot_node"]`；合併而非覆寫既有檔。`inbox/` 非空時在 session 開頭印一行。
- `Dockerfile`／CI：重建映像；`CLAUDE.md` 的 sidecar 段落與 env 表更新。

## Capabilities

### New Capabilities
- `talos-operator-escalation`：`reboot_node` 的契約——兩段式 confirm、呼叫時推導的 allowlist、graceful only、audit 行格式、operator 憑證缺席時工具不存在；以及讀取工具的直連退路與路徑標記。
- `im-permission-profile`：agent 在 ttyd 內對 MCP 工具的權限面——哪些不提示、哪些必提示、提示由誰按、不得出現「無提示自動拒絕」。
- `operator-inbox`：`~/workspace/inbox/` 的交辦慣例——誰寫、im 何時讀、讀完怎麼標記。

### Modified Capabilities
（無——本 repo 沒有既有 spec。sidecar 層 `talos-mcp-sidecar` 的 delta 在 root。）

## Impact

- `talos_mcp_server.py`、`claude-session`、`Dockerfile`、`CLAUDE.md`、`test/`。
- 映像 tag 變動會由 root 的 tasks 帶進 `jg-base` 與 `jg-cluster-template` 的 HelmRelease。
- `kubectl` 在 sidecar 內可用的前提：sidecar 與 `app` 同一個 pod、同一個 ServiceAccount（`claude-code`，cluster-admin）——allowlist 推導用的是 `get nodes`，不新增 RBAC。
