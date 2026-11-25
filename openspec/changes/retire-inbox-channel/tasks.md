## 1. 前置條件（不在本 repo；三項都完成之前不合併）

- [x] 1.1 `ferry133/jg-extras#26`：生產用 chamber 伺服器（已關，2026-11-23 讀）
- [x] 1.2 `ferry133/jg-cluster-template#263`：`cluster.yaml` 能開 chamber（已關，2026-11-23 讀）
- [x] 1.3 jcom、jg-jiahd 先跑 observe，再切到 deliver（ferry133 點頭）
  - **切了（2026-11-25 15:26–15:30，`fleet-ops-02 [e3795b]`）**：jcom `47d5460`、jg-jiahd `bb272f7`，依據是 ferry133 15:22 在 fleet-ops-02 的 console 的「5. yes, go.」；兩座 bridge 都印 `mode=deliver server=ok`、`online=no`（沒有人開 session）
- [x] 1.4 ~~`im-chamber-client` 先歸檔~~ 不再需要（2026-11-25）：它的 `operator-inbox` delta 已撤回，兩個 change 不再改同一條 requirement

## 2. k8scc（PR，k8scc#103）

- [x] 2.1 刪 `inbox-watch`、entrypoint 的啟動、Dockerfile 的 COPY／chmod、它的 controls 和 CI 步驟
- [x] 2.2 刪 `claude-session` 的 `📥 inbox` 提示與停頓；controls 改成「有未讀檔也什麼都不印、不停頓」
- [x] 2.3 pane fixture 搬到 `test/pane-fixtures.sh`；打字閘門的 cell 移到 chamber-watch controls（inbox-watch 那時的六個閘門 mutation，在 chamber-watch controls 上各紅一次）
- [x] 2.4 `im-policy.md` 的 inbox 段落與 chamber 段落；`test/im-policy-controls.sh`
- [x] 2.5 `CLAUDE.md`
- [x] 2.6 驗收（fleet-ops-4c [50fff3]）：在一座 deliver 的 im 上，inbox 放一份新檔不會被宣告；房間裡的交辦照常被宣告；im 動了路由器之後，`-report.md` 照樣寫得出來
  - **驗收通過（2026-11-25 15:54，`fleet-ops-02 [e3795b]` 代開單者收，k8scc#103）**：jg-jcc2，映像 `b76a45f`，deliver，`SESSION_KEEP=1`。inbox 放新檔 100 秒後 pane 不變；房間交辦被宣告、im 回 `ack` 與 `report`；`-report.md` 寫得出來。合併為 `5ac023d`。**沒量**：im 動路由器之後的報告、netdev revert-failed 那條路、生產叢集上的任何一格
