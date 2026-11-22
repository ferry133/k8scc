## 1. 前置條件（不在本 repo；三項都完成之前不合併）

- [ ] 1.1 `ferry133/jg-extras#26`：生產用 chamber 伺服器
- [ ] 1.2 `ferry133/jg-cluster-template#263`：`cluster.yaml` 能開 chamber
- [ ] 1.3 jcom、jg-jiahd 先跑 observe，再切到 deliver（ferry133 點頭）
- [ ] 1.4 `im-chamber-client` 先歸檔（它的 `operator-inbox` delta 會 MODIFY 本 change 要 REMOVE 的那條 requirement）

## 2. k8scc（PR，k8scc#103）

- [x] 2.1 刪 `inbox-watch`、entrypoint 的啟動、Dockerfile 的 COPY／chmod、它的 controls 和 CI 步驟
- [x] 2.2 刪 `claude-session` 的 `📥 inbox` 提示與停頓；controls 改成「有未讀檔也什麼都不印、不停頓」
- [x] 2.3 pane fixture 搬到 `test/pane-fixtures.sh`；打字閘門的 cell 移到 chamber-watch controls（inbox-watch 那時的六個閘門 mutation，在 chamber-watch controls 上各紅一次）
- [x] 2.4 `im-policy.md` 的 inbox 段落與 chamber 段落；`test/im-policy-controls.sh`
- [x] 2.5 `CLAUDE.md`
- [ ] 2.6 驗收（fleet-ops-4c [50fff3]）：在一座 deliver 的 im 上，inbox 放一份新檔不會被宣告；房間裡的交辦照常被宣告；im 動了路由器之後，`-report.md` 照樣寫得出來
