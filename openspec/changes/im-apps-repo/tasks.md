> 全部在 `k8scc`。驗收由 root 的開單者（fleet-ops `fleet-ops-77 [af719f]`，或同目錄的繼承者）在 merge 之前收；
> 歸檔時由作者回頭更新 root（`fleet-ops/openspec/changes/standalone-app-repos`）。
> 測試照既有 controls 的形狀：每一格有 `check` 與 `check_not`；Python 測試用 `python3 test/<file>.py` 跑，不用 `-m unittest <path>`。

## 1. 映像

- [ ] 1.1 `Dockerfile`（base）：安裝釘版的 `sops`；`:475-493` 的版本斷言加一行；不加 `age`。
- [ ] 1.2 bump 前後映像大小寫進 PR；`sops --version` 在容器內成功。

## 2. claude-session

- [ ] 2.1 `configure_apps_repo`：`IM_APPS_REPO` ＋ 金鑰檔同時存在 → 寫 `~/.ssh/config`（host alias、`IdentitiesOnly yes`、釘住的 known_hosts）與 `~/.gitconfig`；任一缺 → 不寫、印一行。冪等。
- [ ] 2.2 `permissions.allow` 合入 `Bash(git *)`、`Bash(sops *)`（同 `merge_talos_permissions` 的合併語意）；寫入路缺時不加。
- [ ] 2.3 `test/claude-session-controls.sh`：有→寫、無→不寫、第二次零 diff、user-repo remote 無身分（負對照）。

## 3. policy

- [ ] 3.1 `im-policy.md`「你的 apps repo」一節（內容見 proposal）。
- [ ] 3.2 `test/im-policy-controls.sh`：新節的 `check`；對「明文 Secret」「貼 log」類措辭的 `check_not`；對舊版 policy 跑要紅。

## 4. spec 與收尾

- [ ] 4.1 `im-permission-profile` 的 MODIFIED 在歸檔時合進 `openspec/specs/`；`im-apps-repo` 成為新 spec。
- [ ] 4.2 PR 內文署名；開單者驗收在 merge 之前；merge 後 PR 留一行署名。
- [ ] 4.3 歸檔本 sub-change，回更新 root 的 Sub-changes 表與 tasks §4。
