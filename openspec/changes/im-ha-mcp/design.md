# design — im-ha-mcp

Root 的設計在 `fleet-ops/openspec/changes/im-home-automation/design.md`（D1 並存、D2 extra 形狀、D3 身分與 token 進 `app`、D4 權限邊界）。這裡只記 k8scc 自己要決定的事。

## Decisions

### D-a ─ 註冊不走 `claude mcp add --header`，直接寫 `.claude.json`

`claude mcp add --transport sse ha <url> --header "Authorization: Bearer <token>"` 把 token 放進 argv，容器裡任何人 `ps` 都看得到——`~/.claude/CLAUDE.md` 的 Secrets 節點名的四種洩漏路徑之一。`claude-session` 已經用 python 直接寫 `settings.json`（`merge_permissions`），同一招寫 `.claude.json`：先 **spike 1.1** 在乾淨 `HOME` 用假 token 跑一次 `claude mcp add --transport sse ha http://x --header 'Authorization: Bearer dummy'`，把它寫出來的結構（`projects["<cwd>"].mcpServers.ha = {type, url, headers}`）抄成 python 的 dict 寫法；`claude mcp get ha` 讀得到才算等價。token 從 `HA_MCP_TOKEN_FILE` 讀進 python 變數，從不經過 shell 展開、從不印。

**spike 1.1 讀數**（2026-10-26 07:30，Claude Code `2.1.261` 的 darwin-x64 建置——與映像同版號、不同平台；映像內的 linux 建置沒有量）：
- 寫到 `$CLAUDE_CONFIG_DIR/.claude.json`（有設時；jg-base 設 `/home/claude/.claude`），否則 `$HOME/.claude.json`；檔案 mode `0600`。
- 結構：`projects[<cwd 的 realpath>].mcpServers.ha = {"type": "sse", "url": <url>, "headers": {"Authorization": "Bearer <token>"}}`（`--transport http` 寫出同形、`type` 是 `"http"`，`claude mcp get ha` 回 rc 0、`Type: http`：2.1.310 先量，2026-10-26 在釘版 2.1.261 的 darwin build 上又量了一次；映像裡的 linux build 沒量）；從 symlink 進去的 cwd 一樣找得到（鍵是 realpath）。
- python 只寫 `{"projects": {<cwd>: {"mcpServers": {"ha": …}}}}` 的新檔，`claude mcp get ha` 讀得到、印出 type／url／headers；claude 自己隨後補上其他頂層鍵。
- ⚠️ **`claude mcp get ha` 把 header 全文印到 stdout**——`claude-session` 從不顯示它的輸出（本實作根本不為 ha 呼叫 `claude`）。它對連不上的已註冊 server 回 0、對未註冊回 1，所以不是健康讀數。
- `mcp__ha` 規則：`claude doctor` 對故意寫壞的 `Bash(unclosed` 報「Invalid permission rule … was skipped」，對 `mcp__ha` 不報——有正對照的「語法被接受」。「真的免問」要 HA 在，量不到，留 §4.2。

### D-b ─ `permissions.allow` 用 server 層級的 `mcp__ha`

HA 的 MCP server 暴露的工具名取決於 Assist 的 expose 設定與 HA 版本（`HassTurnOn`／`HassLightSet`／`GetLiveContext`…），逐支列舉會在公司多 expose 一類實體時過期，而過期的樣子是「im 每次都被問」——在 auto mode 等於工具消失。Claude Code 的規則語法支援 `mcp__<server>` 整個 server 放行（spike 1.1 在 `2.1.261` 上確認）；root spec 寫的 `mcp__ha__*` 是這個意思，歸檔時把 root 的字對齊。

### D-c ─ 輪替：比對現有的項，不同才重寫（實作時取代了 sha256 檔）

`claude mcp get ha` 只回答「有沒有註冊」，token 換了它照樣說有——與 talos 的「註冊一次」不同，這裡 token 會被 `ha-im-token` skill 輪替。原本的做法是 `~/.claude/ha-mcp.token.sha256` 存上一次寫進去的 token 的 sha256。**實作時改成**：python 讀 `.claude.json` 裡現有的 `ha` 項，與要寫的項相比，相等就**不開檔寫**，不等才整份以 temp＋`os.replace` 寫回（mode 0600，與 claude 自己的檔相同）。理由：sha 檔與 `.claude.json` 是兩份會分岔的紀錄——有人 `claude mcp remove ha` 之後 sha 仍相同，sha 版本永遠不會補回；直接比對同時涵蓋輪替與手動移除，少一個檔。比對只在記憶體裡，不印。

### D-d ─ 空字串＝未設；URL 有而 token 缺 ⇒ 一行 warning、零寫入

jg-base 的 `${HA_MCP_URL}` 在未設 HA 的叢集代換成空字串（jg-base-ed 2026-10-25 22:50 量到的代換語意；jg-base#221 已照此寫）。`claude-session` 的判斷是 `[ -n "${HA_MCP_URL}" ]`，與 `IM_APPS_REPO` 相同。URL 有、token 檔不存在或 0 bytes（Secret key 由未設的值渲染出來就是 0 bytes）⇒ 印一行 `⚠ Home Assistant 沒有設定：<原因>`，`.claude.json` 與 `settings.json` 都不動——與 apps repo 的 fail-closed 同形，controls 用 `check_not` 驗「沒有第二行」。

### D-e ─ policy 寫「技術上做不到的也寫」

非 admin 的 HA 使用者本來就改不了整合、進不了開發者工具，但 policy 是 im 讀的、HA 的權限表不是——所以「不碰整合與使用者」還是寫進去，和「不碰 HomeKit／Google 配對」（技術上做得到、影響客戶家人）並列，再加 homebridge（它仍在服務家人）。五項不碰各一條 `check`。

### D-f ─ 不做反向清理

HA 拿掉（URL 變空）之後，`.claude.json` 的 `ha` 項與 `allow` 的 `mcp__ha` 還在；那時 server 不存在，前者讓 `claude` 啟動時報一個連不上的 MCP、後者什麼都開不了。與 talos／git／sops 既有的決定相同；差別是 `ha` 的連不上會每次 session 印一行——接受，寫進 CLAUDE.md；要清理是 `claude mcp remove ha` 一行，由人做。

## Risks

- [`claude mcp add` 的 `.claude.json` 結構在版本間變] → spike 1.1 釘在 `2.1.261`；controls 以假 `claude` 跑、直接讀 `.claude.json` 的項比對 spike 1.1 量到的結構；真 `claude` 讀回只在 spike 量過一次（darwin-x64），結構若在版本間變，controls 不會紅——升 Claude Code 版本時重跑 spike 1.1 那三行。
- [`mcp__ha` 語法不被 `2.1.261` 接受] → spike 1.1 第二格：寫進 `allow` 後以 `claude --print` 呼叫一個 HA 工具不被問（要 root spike 1.1 的 HA 在才量得到；量不到就退回逐支列舉＋每次 session 從 server 讀工具清單重寫——多一個機制，所以先量）。
- [token 檔可讀但內容多一個換行] → `read -r`／python `.strip()`；controls 一格「尾隨換行的 token 檔」。
- [`.claude.json` 被 `claude` 自己同時改] → 只在 session 起來、`claude` 還沒跑時寫，與 `claude mcp add` 的時點相同。

### D-g ─ 傳輸是 streamable HTTP（2026-10-26 改）

第一版寫 `type: sse`，那時 HA 的端點還沒量。fleet-ops-65 在 jg-jcc2 的 HA 2026.10.4 上量（先設好 `mcp_server` 整合；設之前四個探測全是 404）：`POST /api/mcp` initialize ⇒ 200 `application/json`、`GET /api/mcp` ⇒ 405、不帶 token ⇒ 401；舊的 `GET /mcp_server/sse` ⇒ 200 `text/event-stream`。im 拿到的 URL 是 tenant 公開 HTTPRoute 上的 `/api/mcp`（jgct#245 root 裁定 (A)），所以 `type` 是 `http`。已用 `sse` 寫過的 pod：那一項與要寫的不同，下一次 session 覆寫一次（與換 token 同一條路），不需要遷移步驟。
