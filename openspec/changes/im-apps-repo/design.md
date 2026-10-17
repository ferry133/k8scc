# design — im-apps-repo（k8scc 份額）

Root 的設計決策在 `fleet-ops/openspec/changes/standalone-app-repos/design.md`（D5 寫入路）；這裡只寫 k8scc 內的形狀。

## Decisions

- **D-a deploy key 不是 PAT，SSH host alias 不是 credential helper**：金鑰是 repo 範圍、不綁帳號、可單撤；`~/.ssh/config` 的 `Host github.com-im-apps` 讓 im 只對這一個 remote 有身分——對 user repo 的 push 自然失敗（root spec 的負對照）。
- **D-b 配置是條件式且冪等**：`IM_APPS_REPO` 與金鑰檔**同時**存在才寫；寫的是固定內容，第二次執行 diff 為空。這與 `merge_talos_permissions` 對 `TALOS_MCP_URL` 的作法同形（`claude-session:126-135`）。
- **D-c 權限跟著 tool surface**：`Bash(git *)`、`Bash(sops *)` 進 `permissions.allow`，理由同 `im-permission-profile` 既有的一條——im 不依賴人按（ferry133 2026-10-15）。範圍只到這兩支；不開 `Bash(*)`。
- **D-d policy 寫「什麼進 repo」而不是「怎麼用 git」**：im 會 git；它不知道的是這個 repo 是 private 但仍是耐久紀錄、會隨交接凍結、而公司會讀它。禁語（log、IP、hostname 以外的環境細節）用 `check_not` 釘住舊版 policy 沒有這一節的狀態。

## Risks

- [`sops` 進 base 映像加大體積] → 單一靜態二進位，量 bump 前後的映像大小寫進 PR。
- [known_hosts 過期（GitHub 換 host key）] → 用映像內釘住的 known_hosts，過期時 push 失敗、不會靜默接受；bump 流程更新。
- [im 以 cluster-admin 直接 `kubectl apply` 繞過 repo] → 不是本 repo 關得住的；policy 寫明，root 接受這個 trade-off。
