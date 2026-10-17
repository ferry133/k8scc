> 全部在 `k8scc`。驗收由 root 的開單者（fleet-ops `fleet-ops-77 [af719f]`，或同目錄的繼承者）在 merge 之前收；
> 歸檔時由作者回頭更新 root（`fleet-ops/openspec/changes/standalone-app-repos`）。
> 測試照既有 controls 的形狀：每一格有 `check` 與 `check_not`；Python 測試用 `python3 test/<file>.py` 跑，不用 `-m unittest <path>`。

## 1. 映像

- [x] 1.1 `Dockerfile`：照 `omnictl`／#18 樣式——`ARG SOPS_VERSION` 提到第一個 `FROM` 之前（`:15` 旁）；base 層按 `ARCH` 下載、比對釘在 Dockerfile 的 per-arch sha256（無釘值的架構 `exit 1`）；factory 層移除 `:318` 的 `ARG SOPS_VERSION` 與其安裝步驟；base 的斷言區加 `sops --version`。
- [x] 1.2 `github-known-hosts`：從 `https://api.github.com/meta` 的 `ssh_keys` 產生、進 repo；Dockerfile 釘各 key 指紋，建置時 `ssh-keygen -lf` 比對；`test/assert-controls.sh` 加同一斷言與一個改一字元的負對照。
- [ ] 1.3 bump 前後映像大小寫進 PR；容器內 `sops --version`、`ssh-keygen -lf /etc/ssh/github-known-hosts` 成功；factory 層 `sops` 只出現一次。

## 2. claude-session

- [x] 2.1 `configure_apps_repo`：`IM_APPS_REPO` ＋ 金鑰檔存在 ＋ mode 過 ssh 自己的規則（檔為呼叫者所有時不得有 group／other 位元；不是呼叫者所有則不查）→ 寫 `/home/claude/.ssh/config`（alias、`IdentitiesOnly yes`、`UserKnownHostsFile` 指釘住的檔、`StrictHostKeyChecking yes`）與 `~/.gitconfig`（`user.name im`、`user.email im@${IM_APPS_REPO#*/}`、**`core.sshCommand = ssh -F /home/claude/.ssh/config`**）；任一前提不成立 → 不寫、印一行點名哪個前提。冪等。
- [x] 2.2 `permissions.allow` 合入 `Bash(git *)`、`Bash(sops *)`（同 `merge_talos_permissions` 的合併語意）；寫入路缺時不加；與 talos 那組彼此獨立（有一無一的四種組合都對）。
- [x] 2.3 `test/claude-session-controls.sh`：有→寫、無金鑰→不寫、mode `0644`→不寫、第二次零 diff；**`ssh -F <file> -G github.com` 不解出 identityfile、`ssh -F <file> -G <alias>` 解出**（負對照；不用裸 `ssh -G`，它讀的是 runner 自己的 `~/.ssh`）；一格 HOME ≠ pw_dir：HOME 重導後裸 `ssh -G <alias>` 不解出、`-F` 解出——這就是 `core.sshCommand` 存在的理由；四種 env 組合下 `permissions.allow` 的內容。

## 3. policy

- [x] 3.1 `im-policy.md`「你的 apps repo」一節（內容見 proposal）。
- [x] 3.2 `test/im-policy-controls.sh`：新節的 `check`；對「明文 Secret」「貼 log」類措辭的 `check_not`；對舊版 policy 跑要紅。

## 4. spec 與收尾

- [ ] 4.1 `im-permission-profile` 的 MODIFIED 在歸檔時合進 `openspec/specs/`；`im-apps-repo` 成為新 spec。
- [x] 4.2 jg-base 側要配合的兩件事開成 jg-base issue（不在本 repo 做；→ jg-base#183）：im HelmRelease 掛 Secret 到 `/home/claude/.ssh/im-apps.key`，`defaultMode` `0400` 或 `0600` 皆可（今天 im 以 root 跑；Secret volume 永遠 root 擁有，所以 mode 救不了非 root——若日後改非 root，走 `fsGroup` 得 `root:<fsGroup> 0440`，ssh 接受）；設 `IM_APPS_REPO` env。
- [ ] 4.3 PR 內文署名；開單者驗收在 merge 之前；merge 後 PR 留一行署名。
- [ ] 4.4 歸檔本 sub-change，回更新 root 的 Sub-changes 表與 tasks §4。
