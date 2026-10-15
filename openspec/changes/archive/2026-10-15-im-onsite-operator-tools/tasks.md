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

- [x] 3.1 `CLAUDE.md`：sidecar 段落改寫（兩把憑證、四層門檻、退路標記）；env 表加 `TALOS_OPERATOR_CONFIG`、`POD_IP`；新增「inbox 慣例」與「reboot 的批准來自 ttyd 前的人（10-15 裁定前）；auto mode 下若提示會倒數自動拒絕，切 manual」（後半句待 root 5.6 量到再定稿）。**2026-10-15 裁定後這句作廢，改寫見 4.2。**
- [x] 3.2 CI 重建映像；把 tag 回報到 root（`jg-base/openspec/changes/im-onsite-operator-tools/tasks.md` 3.2）。
  - 首版 `fb98a11@sha256:3ba87565…`（jg-base#156，root 3.2b）；最終 `d8d0fd3@sha256:53978cb549042068d4bc3c4123a2e05822b90591a58c05dc4ab32b0d63842aff`（含 #23／#24／#26；jg-base#169 合併 `9e80ba6`，2026-10-16 兩座部署，root 7.4 讀數 jg-base#170）。
- [x] 3.3 archive 本 sub-change 時回頭更新 root（四條規則第一條）；root 的 `talos-mcp-sidecar` delta 引用了本 repo 的 `talos-operator-escalation`，兩邊的工具名與路徑標記字串要逐字一致——archive 前 grep 一次。
  - 2026-10-16 對 jg-base `main` 的 root `specs/talos-mcp-sidecar/spec.md` 抽出全部反引號字面值逐一 grep：k8scc 側該有的全命中——`get_node_status`、`get_etcd_members`、`get_link_status`、`reboot_node`、`[via direct LAN]`、`Unavailable`、`talos-mcp`、`os:reader`／`os:operator`、`siderolink`、`talos-operator-escalation`；未命中的都是 jg-base 自己的字面值（`${TALOS_OPERATOR_CONFIG_B64:-}`、`claude_instances`、`omnictl talosconfig … --break-glass`、`operator-credential-lifecycle`），不歸本 repo。root 守衛語（quorum／per-node cooldown／caller-supplied reason）與本 spec 的三條 Requirement 對應。root 6.3（依四條規則更新 root）由 jg-base 收，archive commit 回報給 jg-base-0a。

## 4. 2026-10-15 裁定後（root tasks §7：im 是公司的工具，坐在 ttyd 前的只有公司員工，守衛在伺服端）

- [x] 4.1（root 7.2）`reboot_node` 伺服端守衛：(a) 另一台 control-plane 不 Ready 就拒，與 allowlist 同一次 `kubectl get nodes -o json`；(b) 每節點冷卻 600 秒，寫進 token 回應與拒絕訊息；(c) 自身節點兩段回應明說 im 隨之停擺；(d) 必填 `reason`（MCP schema required）→ 每行 audit 的 `stated_reason`。每格負對照（CP NotReady fixture、目標自己 NotReady 仍過、冷卻內第二次、空 reason）；四個突變（關 quorum／關冷卻／收空 reason／execute 不重查）各轉紅。
- [x] 4.2（root 7.3，不可先於 4.1）`claude-session` 的 `mcp__talos__reboot_node` 從 `ask` 改 `allow`，並把自己先前寫進 `ask` 的那一項遷走；`test/claude-session-controls.sh` 的斷言翻面並留負對照；九處前提句子（server docstring／note、`claude-session` 註解、`im-permission-profile` spec、design／proposal／tasks 3.1、`CLAUDE.md` 103／107）一起改；`CLAUDE.md` 寫明「reboot 在 im 的權限內，依診斷執行，事後向公司回報（inbox／health mail）」；inbox 敘述改「公司同仁 → im」。
- [x] 4.3（root 7.3b）pod 內 im 讀的指示由映像出貨：`im-policy.md` → `/etc/claude-code/CLAUDE.md`（Claude Code managed policy，最先載入、每個使用者、個人設定蓋不掉）；三句：reboot 在 im 權限內、門在伺服端、事後回報 inbox／health mail。控制組 `test/im-policy-controls.sh`（Dockerfile COPY 行＋關鍵字各有對照）進 CI。部署後 jg-base 7.4 `kubectl exec -c app -- head` 讀一次。
- [x] 4.4（fleet-ops-77 2026-10-15 拋棄式讀數的兩點觀察）(1) 自身節點的冷卻改從開機起算（`/proc/uptime`）：自身節點重啟必定連 sidecar 一起重啟，記憶體冷卻在那條路上等於沒有；讀不到就拒；其他節點仍用記憶體。(2) `_startup_readings()` 進控制組（in-process 可直接呼叫），guards 行加上讀到的 uptime——root 7.4 在真 pod 上讀它，順便量 `/proc/uptime` 是不是 host 的。兩個新案例＋一個啟動讀數案例，各配負對照；四個突變（關守衛／窗口歸零／讀不到放行／守衛套到所有節點）各轉紅。
