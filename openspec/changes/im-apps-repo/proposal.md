Part of: `fleet-ops` / `standalone-app-repos`

**Owning repo**：`k8scc` —— 本 sub-change 改的檔案（`Dockerfile`、`claude-session`、`im-policy.md`、`test/*-controls.sh`）全在這裡；root change 住在 `fleet-ops`，持有叢集側與公司側程序的 spec。這個 repo 持有 `im-permission-profile`，本 sub-change 對它出 delta。

## Why

Root 的 Why 不在這裡複述（`fleet-ops/openspec/changes/standalone-app-repos/proposal.md`）。k8scc 的份額是讓 im 這個作者**走得到**自己的 apps repo、而且**只走得到那裡**。量到的現況（2026-10-17，`origin/main 99c3568`）：base 映像有 `git`、沒有 `sops`／`gh`／`flux`；`entrypoint.sh` 與 `claude-session` 不設 git 身分、沒有 credential helper（grep 0）；`im-policy.md` 22 行裡沒有一句講 git 或 repo；`settings.json` 由 `claude-session` 在執行期合成，只放五支 talos 工具。

## What Changes

- base 映像加 **`sops`**（釘版本、加進 `Dockerfile` 的版本斷言）。不加 `age`：加密只要公鑰，im 從不持有任何私鑰。不加 `flux`／`kustomize`：`kubectl annotate` 與 `kubectl kustomize` 已夠。
- `claude-session`：當 `IM_APPS_REPO`（`owner/repo`）與掛載的寫入金鑰（root D5 渲染的 Secret，掛在 `~/.ssh/im-apps.key`）**都在**時，寫 `~/.ssh/config`（`Host github.com-im-apps`、`IdentityFile`、`IdentitiesOnly yes`、`UserKnownHostsFile` 用映像內的 GitHub known_hosts）與 `~/.gitconfig`（`user.name im`、`user.email im@<cluster>`），並確保 `permissions.allow` 含 `Bash(git *)` 與 `Bash(sops *)`；任一不在時**什麼都不寫**。冪等，第二次執行零變化。
- `im-policy.md` 新節「你的 apps repo」：只放 manifest／kustomization／README；Secret 必以 tenant 公鑰加密（repo 的 `.sops.yaml` 已有）；commit 不含 log、IP、hostname 以外的環境細節；套用用 `kubectl -n flux-system annotate kustomization tenant-im-apps reconcile.fluxcd.io/requestedAt="$(date -u +%s)"`；**要留下來的東西走 repo，不走 `kubectl apply`**（im 仍是 cluster-admin，apps repo 給的是持久與可審，不是限制）；沒有 apps repo 時這一節不適用。
- 測試：`test/claude-session-controls.sh` 兩個方向（有金鑰寫、無金鑰不寫、第二次零變化）；`test/im-policy-controls.sh` 對新節的 `check` 與對禁語的 `check_not`。

## Capabilities

### New Capabilities

- `im-apps-repo`：im 的 apps repo 寫入路——映像工具、session 配置的觸發條件與內容、policy 的內容規則。

### Modified Capabilities

- `im-permission-profile`：「Permission rules follow the tool surface」——tool surface 多了 apps repo 的 git／sops，`permissions.allow` 要跟著，條件是寫入路存在。

## Impact

- `Dockerfile`（base）：`sops` 安裝與版本斷言
- `claude-session`：新函式 `configure_apps_repo`，在 `merge_talos_permissions` 旁
- `im-policy.md`：新節
- `test/claude-session-controls.sh`、`test/im-policy-controls.sh`
- jg-base 的 im HelmRelease 要掛 Secret 與設 `IM_APPS_REPO` env——那是 root 的 jgct 渲染（`im-apps-write-key.sops.yaml`）＋ jg-base HelmRelease 的 `envFrom`／`persistence`，不在本 repo；本 repo 的契約是「env 與檔案在就配置」。
