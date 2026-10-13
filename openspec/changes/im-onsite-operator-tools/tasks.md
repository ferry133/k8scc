## 1. `talos_mcp_server.py`

- [x] 1.1 讀 `TALOS_OPERATOR_CONFIG`（檔案路徑）與 `POD_IP`；啟動時算一次 `OPERATOR_STATE ∈ {absent, empty, expired(<date>), ok(<notAfter>)}`——0 byte 檔＝`empty`＝視為缺席；`notAfter` 由 `talosctl --talosconfig <f> config info` 解析，< 30 天設 `EXPIRY_WARNING`。
- [x] 1.2 把 `_run` 拆成 `_run_omni(node, *args)`（現行行為）與 `_run_direct(node, *args)`（`--talosconfig <operator> --endpoints <node> --nodes <node>`）；transport-class 錯誤分類（timeout、`Unavailable`、`no route to host`、`context deadline exceeded`）；四個讀取工具：主路失敗且 `OPERATOR_STATE == ok` → 退路一次；回應首行 `[via Omni]`／`[via direct LAN]`；`PermissionDenied`／參數錯不重試；無 operator 時附「direct-LAN fallback not available on this cluster (no operator credential)」。
- [x] 1.3 `reboot_node(node, confirm="")`：allowlist 由 `kubectl get nodes -o jsonpath=…InternalIP` 在呼叫當下取得，失敗 fail closed；第一段發 token（`secrets.token_urlsafe`，綁 node、120 秒、單次）並回 `will_run`；第二段驗 token → `node == POD_IP` 不帶 `--wait`、否則 `--wait` timeout 600 → 作廢 token；每段與每次拒絕寫一行 JSON audit 到 stdout（`ts`、`tool`、`node`、`phase`、`reason`、`rc`、`user=$CLAUDE_USER_ID`）。
- [x] 1.4 只在 `OPERATOR_STATE == ok` 時 `app.tool()(reboot_node)`；`EXPIRY_WARNING` 存在時每個工具回應前綴警告行（含 `talos-operator-credential rotate`）。
- [x] 1.5 `test/`：假 `talosctl` 與假 `kubectl` shim（以 `PATH` 注入，可用環境變數控制回傳：成功／transport 錯／`PermissionDenied`／節點清單）；案例：主路成功標 `[via Omni]`、主路 transport 錯＋operator → `[via direct LAN]`、主路 `PermissionDenied` 不退路、無 operator 的訊息、token 流程（空 confirm 不動作、正確 token 執行一次、重放拒、過期拒、錯 node 拒）、叢集外 IP 拒、kubectl 失敗 fail closed、`POD_IP` 情境不帶 `--wait`、注入字元成單一 argv。**每個案例配一個會 FAIL 的負對照**（把 shim 調成相反結果），否則 0 個失敗分不出「都好」與「量具死了」。

## 2. `claude-session`

- [x] 2.1 `TALOS_MCP_URL` 設定時，用 `python3 -c` 讀 `settings.json`、合併 `permissions.allow += [四個 mcp__talos__get_*]`、`permissions.ask += ["mcp__talos__reboot_node"]`（去重、保留既有、保留其他鍵）、寫回；跑兩次 diff 為空；`TALOS_MCP_URL` 未設時 `permissions` 不動。
- [x] 2.2 `~/workspace/inbox/` 存在且有非 `*.read` 檔時，在啟動 `claude` 之前印一行 `📥 inbox: N file(s) — read ~/workspace/inbox/ before anything else`；不讀內容。

## 3. 文件與映像

- [x] 3.1 `CLAUDE.md`：sidecar 段落改寫（兩把憑證、四層門檻、退路標記）；env 表加 `TALOS_OPERATOR_CONFIG`、`POD_IP`；新增「inbox 慣例」與「reboot 的批准來自 ttyd 前的人；auto mode 下若提示會倒數自動拒絕，切 manual」（後半句待 root 5.6 量到再定稿）。
- [ ] 3.2 CI 重建映像；把 tag 回報到 root（`jg-base/openspec/changes/im-onsite-operator-tools/tasks.md` 3.2）。
- [ ] 3.3 archive 本 sub-change 時回頭更新 root（四條規則第一條）；root 的 `talos-mcp-sidecar` delta 引用了本 repo 的 `talos-operator-escalation`，兩邊的工具名與路徑標記字串要逐字一致——archive 前 grep 一次。
