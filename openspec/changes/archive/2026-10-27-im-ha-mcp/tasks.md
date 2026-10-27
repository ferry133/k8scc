> 全部在 `k8scc`。**驗收由 root 擁有者——fleet-ops 目錄的 session——在 merge 之前收**；作者（k8scc-c7 [9b9eab]）也是 root 的開單者，**不自收**（自己修自己收，與沒有驗收在紀錄上長得一樣）。
> 歸檔時由作者回頭更新 root（`fleet-ops/openspec/changes/im-home-automation` 的 Sub-changes 表與 tasks §5）。
> 測試照既有 controls 的形狀：每一格有 `check` 與 `check_not`；三種結果（通過／失敗／量不到）分開寫。

## 1. Spikes

- [x] 1.1 （2026-10-26 量，讀數在 design D-a；darwin-x64 建置，linux 映像內未量）在乾淨 `HOME` 用映像裡的 Claude Code `2.1.261` 跑 `claude mcp add --transport sse ha http://127.0.0.1:1 --header 'Authorization: Bearer dummy'`，記下 `.claude.json` 寫出的結構（鍵名、`type`、`headers` 形狀）；再把 `mcp__ha` 寫進 `permissions.allow`，確認語法被接受（`claude` 啟動不報 invalid rule）。結構抄進 design D-a。（量不到「server 層級規則真的免問」——那要 root spike 1.1 的 HA 在——就標量不到，留到 §4.2。）
- [x] 1.2 （2026-10-26 18:12 fleet-ops-65 在 jg-jcc2 量到：HA 2026.10.4 的 MCP server 存在（`mcp_server` 整合要先設），端點 `/api/mcp`、streamable HTTP ⇒ `type` 改 `http`，見 design D-g。）（2026-10-26 07:35：root spike 1.1 未量，fleet-ops `1390aa7`。本實作不依賴它；量到「沒有 MCP server」或「只有 streamable HTTP」時回來改 proposal／`type` 常數）root spike 1.1 的結果（HA 的 MCP server 存在與否、端點路徑、非 admin 的限制）——消費，不重量；沒有 MCP server ⇒ 本 sub-change 改成包一個薄的 `ha_mcp_server.py`（形狀照 `talos_mcp_server.py`），proposal 要改。

## 2. claude-session

- [x] 2.1 `configure_ha_mcp`：`[ -n "$HA_MCP_URL" ]` ＋ token 檔存在且非空 ⇒ python 直接寫 `.claude.json` 的 `ha` 項（D-a），token 由 python 讀檔、`.strip()`、不經 shell；與現有的項比對決定要不要重寫（D-c，實作時取代了 sha256 檔）；缺 token ⇒ 恰一行 `⚠ Home Assistant 沒有設定：…`、零寫入（D-d）。
- [x] 2.2 `HA_ALLOW=(mcp__ha)` 合進 `permissions.allow`（`merge_permissions` 既有語意）；與 talos／apps 那兩組互相獨立（八種組合裡抽四種驗）。
- [x] 2.3 `test/claude-session-controls.sh`：未設／空字串 ⇒ 靜默、不動；URL＋token ⇒ `.claude.json` 的 `ha` 項等於 spike 量到的結構、`allow` 有 `mcp__ha`、輸出與 env 對 token 前 8 字元 0 命中（正對照 `HA_MCP_URL` ≥1）、尾隨換行被去掉；URL＋token 缺／0 bytes ⇒ 恰一行 warning（`check_not` 第二行）、零寫入；第二次零 diff；換 token ⇒ 重寫一次、第三次零 diff。

## 3. policy 與文件

- [x] 3.1 `im-policy.md`「你的家庭自動化」一節（內容見 proposal；四項不碰＋homebridge 不碰、自動化只在 HA、報告去 inbox）。
- [x] 3.2 `test/im-policy-controls.sh`：適用條件句、自主句、報告句、五項不碰、單一自動化句各一 `check`；「替家人重新配對」「關掉 homebridge」「停用 homebridge」`check_not`；對去掉任一句的副本要紅。
- [x] 3.3 `CLAUDE.md`：Runtime Configuration 加 `HA_MCP_URL`、`HA_MCP_TOKEN_FILE`；新節「Home Assistant MCP」（為什麼 token 進 `app`、D-a 為什麼不用 `--header`、D-f 不做反向清理）。

## 4. 收尾

- [x] 4.1 （2026-10-27 歸檔時由 `openspec archive` 合進；root 那一行字樣對齊交給 fleet-ops-65）`im-permission-profile` 的 MODIFIED 在歸檔時合進 `openspec/specs/`；`im-ha-mcp` 成為新 spec；root spec 的 `mcp__ha__*` 字樣對齊成 `mcp__ha`（在 root 那邊改，一行）。
- [x] 4.2 （2026-10-27，ferry133 在 jg-jiahd 的 im 裡從 CLI 讀到 HA，時間在 15:10Z 之前、走當時的 tenant 網址；fleet-ops-65 量 jg-jcc2：`ha` 註冊為 http、`mcp__ha` 在 allow、token 200／無 token 401、`GetLiveContext` isError=false。**未量**：呼叫一個服務並復原；URL 換成 `https://ha.jiahd.cc/api/mcp` 後下一個 session 改寫 entry（15:27Z 讀到 entry 仍是舊網址，pod 重啟後尚無 session）；#52 的新政策節要等 bump 進 pod。ferry133 裁定不等這些、直接歸檔）活 pod 的格（等 jg-extras#13、jgct#237、jg-base#221 都進、拋棄式 jg-jcc2 有 HA 之後，由驗收者在 root 7.3／7.5 一併量）：`mcp__ha__*` 呼叫不被問、能讀實體、能呼叫一個服務並復原；`/etc/claude-code/CLAUDE.md` 裡有新節。本 sub-change 不等這格歸檔，但 PR 內文要寫明「活 pod 未量」。
- [x] 4.3 （#42、#44、#52 各由 fleet-ops-65 驗收後合併，PR 上有署名行）PR 內文署名；驗收者（fleet-ops 目錄的 session）在 merge 之前收；merge 後 PR 留一行 `peer name [ref]`。映像 bump 走既有流程（`bump/claudecode`）。
- [x] 4.4 （本 PR；root 的 Sub-changes 表與 tasks §5 交給 fleet-ops-65）歸檔本 sub-change，回更新 root 的 Sub-changes 表與 tasks §5。
