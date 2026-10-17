Part of: `fleet-ops` / `standalone-app-repos`

**Owning repo**：`k8scc` —— 本 sub-change 改的檔案（`Dockerfile`、`claude-session`、`im-policy.md`、`test/*-controls.sh`）全在這裡；root change 住在 `fleet-ops`，持有叢集側與公司側程序的 spec。這個 repo 持有 `im-permission-profile`，本 sub-change 對它出 delta。

## Why

Root 的 Why 不在這裡複述（`fleet-ops/openspec/changes/standalone-app-repos/proposal.md`）。k8scc 的份額是讓 im 這個作者**走得到**自己的 apps repo、而且**只走得到那裡**。量到的現況（2026-10-17，`origin/main 99c3568`）：base 映像有 `git`、沒有 `sops`／`gh`／`flux`；`entrypoint.sh` 與 `claude-session` 不設 git 身分、沒有 credential helper（grep 0）；`im-policy.md` 22 行裡沒有一句講 git 或 repo；`settings.json` 由 `claude-session` 在執行期合成，只放五支 talos 工具。

## What Changes

- base 映像加 **`sops`**，照 `omnictl`／#18 的樣式：`SOPS_VERSION` 提到第一個 `FROM` 之前、base 層按架構下載並比對釘在 Dockerfile 的 sha256、`factory` 層**不再**自己裝（今天 `:318` 的 `ARG SOPS_VERSION=v3.12.1` 與其安裝步驟移除；`:475-493` 是 factory 最後一層的斷言，不是 base 的——k8scc-0c review 指出我第一版指錯層）。不加 `age`：加密只要公鑰，im 從不持有任何私鑰。不加 `flux`／`kustomize`：`kubectl annotate` 與 `kubectl kustomize` 已夠。
- **釘住 GitHub 的 SSH host key**：repo 內新增 `github-known-hosts`（來源 `https://api.github.com/meta` 的 `ssh_keys`），Dockerfile 釘各 key 的指紋並在建置時以 `ssh-keygen -lf` 斷言；不在清單裡的 host key 一律拒絕、不寫入。
- `claude-session`：當 `IM_APPS_REPO`（`owner/repo`）與掛載的寫入金鑰（root D5 渲染的 Secret，掛在 `/home/claude/.ssh/im-apps.key`，無 group／other 位元（`0600` 或更嚴；ssh 自己的規則就是這條）——Secret 的 `defaultMode` 由 jg-base 的 HelmRelease 決定）**都在**時，寫 `/home/claude/.ssh/config`（`Host github.com-im-apps`、`IdentityFile`、`IdentitiesOnly yes`、`UserKnownHostsFile` 指釘住的檔、`StrictHostKeyChecking yes`）與 `~/.gitconfig`（`user.name im`、`user.email im@<repo>`——`<repo>` 取 `IM_APPS_REPO` 的 repo 半，容器裡沒有任何東西帶叢集名、不另加 env——**以及 `core.sshCommand = ssh -F /home/claude/.ssh/config`**：pod 以 uid 0 跑、`HOME` 釘在 `/home/claude`，而 **OpenSSH 讀設定用的是 passwd 的家目錄不是 `$HOME`**，所以沒有 `-F` 時 `~/.ssh/config` 永遠不會被讀、git 卻會讀 `~/.gitconfig`——k8scc-0c 在 OpenSSH 10.2 上量到，`-F` 是正對照），並確保 `permissions.allow` 含 `Bash(git *)` 與 `Bash(sops *)`；任一不在或 mode 太開時**什麼都不寫**、印一行說哪個前提不成立。冪等，第二次執行零變化。
- `im-policy.md` 新節「你的 apps repo」：只放 manifest／kustomization／README；Secret 必以 tenant 公鑰加密（repo 的 `.sops.yaml` 已有）；commit 不含 log、IP、hostname 以外的環境細節；套用用 `kubectl -n flux-system annotate kustomization tenant-im-apps reconcile.fluxcd.io/requestedAt="$(date -u +%s)"`；**要留下來的東西走 repo，不走 `kubectl apply`**（im 仍是 cluster-admin，apps repo 給的是持久與可審，不是限制）；沒有 apps repo 時這一節不適用。
- 測試：`test/claude-session-controls.sh` 兩個方向（有金鑰寫、無金鑰不寫、mode 太開不寫、第二次零變化），負對照用 `ssh -G`（CI 跑得動：只有 alias 解出 `identityfile`）；`test/im-policy-controls.sh` 對新節的 `check` 與對禁語的 `check_not`；`test/assert-controls.sh` 加 known_hosts 指紋斷言。
- 已知且接受：`permissions.allow` 的兩條 git／sops 進了 PVC 上的 `settings.json`，金鑰被拿掉後它們還在——沒有金鑰時那兩條什麼都開不了，所以不做移除邏輯；寫進 design。

## Capabilities

### New Capabilities

- `im-apps-repo`：im 的 apps repo 寫入路——映像工具、session 配置的觸發條件與內容、policy 的內容規則。

### Modified Capabilities

- `im-permission-profile`：「Permission rules follow the tool surface」——tool surface 多了 apps repo 的 git／sops，`permissions.allow` 要跟著，條件是寫入路存在。

## Impact

- `Dockerfile`：`ARG SOPS_VERSION` 提到頂、base 層安裝＋sha256、factory 層移除重裝；known_hosts 指紋斷言
- `github-known-hosts`（新檔，COPY 進映像）
- `claude-session`：新函式 `configure_apps_repo`，在 `merge_talos_permissions` 旁
- `im-policy.md`：新節
- `test/claude-session-controls.sh`、`test/im-policy-controls.sh`、`test/assert-controls.sh`
- jg-base 的 im HelmRelease 要掛 Secret 與設 `IM_APPS_REPO` env——那是 root 的 jgct 渲染（`im-apps-write-key.sops.yaml`）＋ jg-base HelmRelease 的 `envFrom`／`persistence`，不在本 repo；本 repo 的契約是「env 與檔案在就配置」。
