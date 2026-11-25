Part of: `fleet-ops` / `agent-delegation-envelope` (9.6: every im on chamber `deliver`)

**Owning repo**：`k8scc`。改的檔案（`inbox-watch` 刪除、`entrypoint.sh`、`claude-session`、`Dockerfile`、`im-policy.md`、`test/`、`CLAUDE.md`、CI）都在這裡。Issue：k8scc#103。

## Why

ferry133 2026-11-21 在 fleet-ops-4c 的 console 選了方案 A：等每一座 im 都切到 chamber `deliver` 之後，`~/workspace/inbox/` 這條交辦管道就退役。報告目錄留著，`pane-gate` 也留著，因為 `chamber-watch` 還在用它。

**前置條件，都不在本 repo；三項完成之前本 change 不合併：**
- `ferry133/jg-extras#26`：生產用 chamber 伺服器。
- `ferry133/jg-cluster-template#263`：讓 `cluster.yaml` 能開 chamber 的欄位。
- jcom、jg-jiahd 先跑 `observe`，讀數穩定後由 ferry133 點頭切到 `deliver`。

**歸檔順序（2026-11-25 改）：** `im-chamber-client` 原本帶一份 `operator-inbox` 的 MODIFIED delta，修改的正是本 change 要 REMOVE 的那一條，所以它得先歸檔。那份 delta 已在本 PR 裡從 `im-chamber-client` 撤回，兩個 change 不再碰同一條 requirement，歸檔先後不拘。

## What Changes

- **刪掉** `inbox-watch`、`entrypoint.sh` 裡啟動它的那幾行、`Dockerfile` 的 COPY／chmod、`test/inbox-watch-controls.sh` 和兩個 workflow 裡跑它的步驟。
- **刪掉** `claude-session` 的 `📥 inbox` 未讀數提示和 8 秒停頓。
- **`pane-gate` 留著**：它的 fixture 搬到 `test/pane-fixtures.sh`；原本 inbox-watch 的打字閘門 cell 移到 `test/chamber-watch-controls.sh`（「the announcement: the pane gate」）。
- **`im-policy.md`**：
  - 「inbox 慣例」改為「inbox：你的報告目錄」。交辦只從 ttyd 和房間來。
  - 報告照舊叫 `-report.md`，照舊寫在 `~/workspace/inbox/`。目錄沒有改名，因為各節的報告規則、`netdev_mcp_server.py` 的 revert-failed 提示，以及 PVC 上既有的報告都指向這個路徑。
  - 目錄裡不是 im 自己寫的檔不是交辦：不照它動作，也不改名成 `.read`，在房間（或下一份報告）裡說它在那裡。
  - 「誰可以交辦你」、「從哪裡來，回哪裡」、observe 模式、「chamber 不取代 inbox」這幾處的寫法跟著改。

## Capabilities

### Modified Capabilities

- `operator-inbox`：交辦通道的兩條 requirement 移除，「Inbox convention」改寫成報告目錄。

## Impact

- 合併後，還沒切到 `deliver` 的 im 就沒有 FAC→im 的非同步交辦路。這正是前置條件存在的原因。
- PVC 上舊的交辦單（`.read` 或還沒讀的）不會被刪，也不會再被宣告。
