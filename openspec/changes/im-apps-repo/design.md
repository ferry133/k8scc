# design — im-apps-repo（k8scc 份額）

Root 的設計決策在 `fleet-ops/openspec/changes/standalone-app-repos/design.md`（D5 寫入路）；這裡只寫 k8scc 內的形狀。

## Decisions

- **D-a deploy key 不是 PAT，SSH host alias 不是 credential helper**：金鑰是 repo 範圍、不綁帳號、可單撤；`~/.ssh/config` 的 `Host github.com-im-apps` 讓 im 只對這一個 remote 有身分——對 user repo 的 push 自然失敗（root spec 的負對照）。
- **D-b 配置是條件式且冪等**：`IM_APPS_REPO` 與金鑰檔**同時**存在才寫；寫的是固定內容，第二次執行 diff 為空。這與 `merge_talos_permissions` 對 `TALOS_MCP_URL` 的作法同形（`claude-session:126-135`）。
- **D-c 權限跟著 tool surface**：`Bash(git *)`、`Bash(sops *)` 進 `permissions.allow`，理由同 `im-permission-profile` 既有的一條——im 不依賴人按（ferry133 2026-10-15）。範圍只到這兩支；不開 `Bash(*)`。
- **D-d policy 寫「什麼進 repo」而不是「怎麼用 git」**：im 會 git；它不知道的是這個 repo 是 private 但仍是耐久紀錄、會隨交接凍結、而公司會讀它。禁語（log、IP、hostname 以外的環境細節）用 `check_not` 釘住舊版 policy 沒有這一節的狀態。

- **D-e 身分從 `IM_APPS_REPO` 推，不另加 env**：容器裡沒有任何東西帶叢集名（k8scc-0c review）；`user.email im@<repo>`，`<repo>` 是 `IM_APPS_REPO` 的 repo 半——它本來就是 `<cluster>-apps`，夠辨識、零新契約。
- **D-f host key 釘在映像、拒絕不寫入**：`StrictHostKeyChecking yes` ＋ 映像內的 `github-known-hosts`（來源 GitHub `/meta`，建置時比指紋）。GitHub 換 key 時 push 失敗、bump 流程更新；永遠不會靜默接受一把新 key。
- **D-g 金鑰 mode 是前提不是修補，而且前提要照 ssh 自己的規則**：ssh 只在**呼叫者擁有該檔**時才檢查 group／other 位元；`claude-session` 在配置前照同一條規則檢查，不 `chmod`——那是 jg-base HelmRelease `defaultMode` 的事，這裡只量、不修。今天 im 以 root 跑，Secret volume 也永遠是 root 擁有，所以 `0400`／`0600` 都可。若 im 日後改非 root：root 擁有的 `0600` 對非 root 一樣讀不到（我第一版寫「挑 0600 讓非 root 能讀」是錯的，k8scc-0c 指出），owner 也不可能跟 pod uid 走（Secret volume 一律 root）；可行路徑是 `fsGroup` → `root:<fsGroup> 0440`，ssh 接受（檔不是呼叫者擁有的就不查 group 位元），而那時 `claude-session` 的前提若還寫成「無 group／other 位元」就會錯拒——所以前提從一開始就寫成 ssh 的規則，不寫成固定 mode。
- **D-i `$HOME` 不是 ssh 的家**：OpenSSH 用 `getpwuid()` 的 `pw_dir` 找 `~/.ssh/config`，pod 以 uid 0 跑時那是 `/root`，而 `HOME` 釘在 `/home/claude`——我寫的 `~/.ssh/config` 永遠不會被讀，git 卻會讀 `~/.gitconfig`（k8scc-0c 在 OpenSSH 10.2 量到：HOME 重導後 `ssh -G <alias>` 忽略該檔，`-F` 指同一檔則解得出）。所以 `core.sshCommand = ssh -F /home/claude/.ssh/config` 進 gitconfig，controls 一律用 `ssh -F <file> -G`（否則 CI 讀的是 runner 自己的 `~/.ssh`），並加一格「HOME ≠ pw_dir」的案例。
- **D-j 金鑰路徑可設定（`IM_APPS_KEY`），因為 jg-base 不用 subPath 掛**（jg-base-0a [4e5e8d]，jg-base#183）：subPath 掛的 Secret 收不到輪替；而 im/enabled 是 base、每一座共用，Secret 不存在時 `optional` 的 subPath 會怎樣未量。整顆 Secret 掛成目錄（例如 `/etc/im-apps/`）沒有這兩個問題，`~/.ssh` 也維持可寫（容器檔案系統，不是 volume）。預設仍是 `~/.ssh/im-apps.key`；空字串等於未設。`IM_APPS_REPO` 空字串同樣等於未設——jg-base 以 `${IM_APPS_REPO:=}` 代換，沒有 apps repo 的叢集拿到的是空字串。
- **D-h allow 條目比金鑰活得久，接受**：`settings.json` 在 PVC 上，金鑰拿掉後 `Bash(git *)`／`Bash(sops *)` 仍在；沒有金鑰時它們開不了任何門，而寫移除邏輯會讓 `merge_talos_permissions` 那套「只加不減」的語意多一個例外。

## Risks

- [`sops` 進 base 映像加大體積] → 單一靜態二進位，量 bump 前後的映像大小寫進 PR。
- [known_hosts 過期（GitHub 換 host key）] → 用映像內釘住的 known_hosts，過期時 push 失敗、不會靜默接受；bump 流程更新。
- [im 以 cluster-admin 直接 `kubectl apply` 繞過 repo] → 不是本 repo 關得住的；policy 寫明，root 接受這個 trade-off。
