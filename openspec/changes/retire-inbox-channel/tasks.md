## 1. 前置條件（不在本 repo；三項都完成之前不合併）

- [x] 1.1 `ferry133/jg-extras#26`：生產用 chamber 伺服器（已關，2026-11-23 讀）
- [x] 1.2 `ferry133/jg-cluster-template#263`：`cluster.yaml` 能開 chamber（已關，2026-11-23 讀）
- [ ] 1.3 jcom、jg-jiahd 先跑 observe，再切到 deliver（ferry133 點頭）
  - 兩座都已是 observe（jcom `c2241e7` 11-23、jg-jiahd `1bee0b3` 11-24）。**切 deliver：ferry133 2026-11-25 在 k8scc-bf 的 console 以選項回答「兩座都切」**；執行交給 fleet-ops（它的 repo），讀數回來才勾
- [x] 1.4 ~~`im-chamber-client` 先歸檔~~ 不再需要（2026-11-25）：它的 `operator-inbox` delta 已撤回，兩個 change 不再改同一條 requirement

## 2. k8scc（PR，k8scc#103）

- [x] 2.1 刪 `inbox-watch`、entrypoint 的啟動、Dockerfile 的 COPY／chmod、它的 controls 和 CI 步驟
- [x] 2.2 刪 `claude-session` 的 `📥 inbox` 提示與停頓；controls 改成「有未讀檔也什麼都不印、不停頓」
- [x] 2.3 pane fixture 搬到 `test/pane-fixtures.sh`；打字閘門的 cell 移到 chamber-watch controls（inbox-watch 那時的六個閘門 mutation，在 chamber-watch controls 上各紅一次）
- [x] 2.4 `im-policy.md` 的 inbox 段落與 chamber 段落；`test/im-policy-controls.sh`
- [x] 2.5 `CLAUDE.md`
- [ ] 2.6 驗收（fleet-ops-4c [50fff3]）：在一座 deliver 的 im 上，inbox 放一份新檔不會被宣告；房間裡的交辦照常被宣告；im 動了路由器之後，`-report.md` 照樣寫得出來
