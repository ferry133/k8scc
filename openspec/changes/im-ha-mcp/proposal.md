Part of: `fleet-ops` / `im-home-automation`

**Owning repo**：`k8scc` —— 本 sub-change 改的檔案（`claude-session`、`im-policy.md`、`CLAUDE.md`、`test/*-controls.sh`）全在這裡；root change 住在 `fleet-ops`，持有 HA 怎麼部署（jg-extras#13）、怎麼渲染（jgct#237）、怎麼掛進 pod（jg-base#221）與公司側程序的 spec。這個 repo 持有 `im-permission-profile`，本 sub-change 對它出 delta。

## Why

Root 的 Why 不在這裡複述（`fleet-ops/openspec/changes/im-home-automation/proposal.md`）。k8scc 的份額是讓 im **接得到** Home Assistant 的 MCP server、**只以那個身分**接、而且知道自己在家庭裝置上**能做什麼、不能做什麼**。量到的現況（2026-10-26 01:02，`origin/main 59a5d24`）：`claude-session` 註冊兩個 MCP server——`memory`（stdio，`DATABASE_URL` 門控）與 `talos`（sse，`TALOS_MCP_URL` 門控、無 header）；`merge_permissions` 已有合併語意；`im-policy.md` 44 行，五節，沒有任何一句講裝置或家庭自動化；映像裡的 Claude Code 是 `2.1.261`（`Dockerfile:287`）。

## What Changes

- **`claude-session` 多一個門**：`HA_MCP_URL` **非空**（jg-base 以 Flux `${HA_MCP_URL}` 代換，未設 HA 的叢集是空字串——與 `IM_APPS_REPO` 同一規則：空＝未設、靜默）**且** `HA_MCP_TOKEN_FILE`（預設 `/etc/ha-mcp/token`）存在、非空 ⇒ 以 bearer header 註冊 remote SSE server `ha`，並把 **server 層級**的 `mcp__ha` 合進 `permissions.allow`（HA 的工具名隨「哪些實體被 expose」而變，逐支列舉會過期——design D2）。URL 有、token 檔缺或 0 bytes ⇒ 什麼都不寫、**一行** `⚠ Home Assistant 沒有設定：…`（與 apps repo 的 fail-closed 同形）。
- **token 不進 argv、不進 env、不進任何啟動訊息**：`claude mcp add … --header` 會把 token 放進 `ps` 看得到的 argv，所以不用它；`claude-session` 直接寫 `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json` 裡 `claude mcp add` 會寫的那個結構（spike 1.1 先量結構），從檔案讀 token。輪替：與 `.claude.json` 裡現有的 `ha` 項比對，不同才重寫；相同則第二次執行**零變化**（檔案不重寫）——實作時取代了原本的 sha256 檔（design D-c）。
- **`im-policy.md` 新節「你的家庭自動化」**：只在 `HA_MCP_URL` 有值、而且 session 開頭沒有印出「⚠ Home Assistant 沒有設定」時適用。**自主**：操作被 expose 的實體、建立／修改／刪除 HA 的自動化與場景、讀歷史——自己判斷、事後在 `~/workspace/inbox/` 留報告。**不碰**：HomeKit Bridge 的啟用／配對、Google Assistant 的帳號綁定、HA 使用者與整合的增刪、HA 本身的升級、homebridge（它仍在服務家人）。**自動化只寫在 HA**；同一顆裝置兩邊都看得到是正常的（MQTT 是單一真相來源）。
- **`CLAUDE.md`**：Runtime Configuration 表加 `HA_MCP_URL`、`HA_MCP_TOKEN_FILE` 兩列；新節「Home Assistant MCP」說明為什麼這把 token 進 `app`（root D3：非 admin、秒級可撤、`app` 本來就是 cluster-admin 讀得到任何 Secret——隔離防的是不經意外洩，不是決意的操作者）。
- **controls**：`test/claude-session-controls.sh`——未設／空字串 ⇒ 不註冊、`permissions` 不動；設了＋token ⇒ `.claude.json` 有 `ha`、`allow` 有 `mcp__ha`、程序 env 與啟動輸出**0 次**命中 token（正對照：`HA_MCP_URL` 1 次）；設了＋token 缺或 0 bytes ⇒ 恰一行 warning、零寫入；第二次零 diff；token 換了 ⇒ 重寫一次、第三次又零 diff。`test/im-policy-controls.sh`——新節的 `check`（自主句、五項不碰、報告句）與 `check_not`（「替家人重新配對」「關掉 homebridge」類措辭）；對舊版 policy 跑要紅。
- 已知且接受：`mcp__ha` 進了 PVC 上的 `settings.json`，HA 拿掉之後它還在——沒有 server 時那條什麼都開不了，與 talos／git 的既有決定相同；token 落在 `.claude.json`（PVC，`claudecode-state-backup` 以 age 加密異地）——root D3 已接受。

## Capabilities

### New Capabilities

- `im-ha-mcp`：im 接 Home Assistant 的那一段——註冊的觸發條件與內容、token 的處理方式（檔案進、不進 argv／env／輸出、輪替）、policy 的權限邊界。

### Modified Capabilities

- `im-permission-profile`：「Permission rules follow the tool surface」——tool surface 多了 HA 的 server 層級規則 `mcp__ha`，條件是 URL 與 token 都在。

## Impact

- `claude-session`：新函式 `configure_ha_mcp`，在 talos 那段之後、apps repo 之前；`HA_ALLOW=(mcp__ha)`
- `im-policy.md`：新節
- `CLAUDE.md`：兩列＋一節
- `test/claude-session-controls.sh`、`test/im-policy-controls.sh`
- **不在本 repo**：jg-base#221（env 與 Secret 掛載）、jgct#237（`ha_im_token`、`HA_MCP_URL` 推導）、jg-extras#13（HA extra ＋ ClusterIP Service）、fleet-ops skill `ha-im-token`（建使用者、發 token）。本 repo 的契約是「env 與檔案在就配置」。
