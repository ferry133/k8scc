> 設計的上半（房間、身分、指令、收件方流程）在 root：`fleet-ops/openspec/changes/agent-delegation-envelope/design.md`。這裡只有 im 映像這一側的決定。每一條標明性質：**裁定**＝ferry133 說的；**提案**＝起草者提的；**量過**＝附日期與版本。

## 形狀

```
 pod
 ┌─────────────────────────────── app ───────────────────────────────┐
 │ tmux session `claude` ── claude（agent 本體）                      │
 │   ▲ 一行宣告（pane 閒置時）        │ chamber read/send/…           │
 │ chamber-watch ── 活著訊號 ─────────┼──────────┐                    │
 │ session 常駐（掉了重起）           ▼          ▼                    │
 └────────────────────────── unix socket（共用 emptyDir）─────────────┘
 ┌──────────────────────────── chamber-bridge（sidecar）─────────────┐
 │ 登入憑證（Secret 只掛這裡）· 上一次的 sync 位置 · 在線讀數         │
 └───────────────────────────────── https ──▶ chamber 伺服器 ────────┘
```

## Decisions

### D-a 用戶端與驗章工具是同一份檔，不是第二份實作（正本住哪已裁定）

root tasks 6.2 留了一個選擇：共用檔，或各自實作加共用測試向量。選共用檔：`chamber` 與 `directive-verify` 各只有幾百行、只用標準函式庫，映像裡的 python3 與 OpenSSH 都夠（OpenSSH 9.2 在 im pod 裡驗 ed25519 簽章量過，2026-11-17；`sk-ecdsa-sha2-nistp256@openssh.com` 量過，2026-11-19，映像 `e8d7522`，tasks 1.1——用的是固定向量，不是 ferry133 的金鑰）。

**正本住哪**是開著的問題：Mac 側那份在 `~/.claude/scripts/`，不在任何 repo。提案：**本 repo 的 `chamber/` 目錄是正本**（它在 git 裡、有 CI），Mac 側是安裝出來的副本，由一格檢查比對雜湊。在裁定之前，本 repo 的副本帶著來源的雜湊與日期，測試向量兩邊跑同一份。

**裁定（ferry133 2026-11-19，逐字）：「正本放 k8scc」。** 所以往後這兩個檔的修改在本 repo 做、經 PR 與 CI，Mac 側重新安裝；Mac 側那一格雜湊比對回答的是「這份副本是不是 main 上的那一份」。

### D-b 橋接程式握憑證，`app` 經一個 Unix socket 說話（裁定的偏好＋提案的形狀）

ferry133 偏好 sidecar（2026-11-15）；sidecar 的實質好處是登入憑證不在 agent 本體拿得到的地方。但宣告要打進 `app` 的 tmux，sidecar 做不到，所以拆成兩支：

- `chamber-bridge`（sidecar）：唯一持有憑證的行程。開一個很窄的 HTTP 面（**在 Unix socket 上，不在 loopback 上**——見下方 2026-11-19 的更正）——只轉送用戶端用得到的那幾條 Matrix 路徑（whoami、joined_rooms、sync、messages、event、send、presence 的讀），自己加上憑證；其餘路徑一律 404。存上一次的 sync 位置接著用（root 量過：重複的初始同步回的是快取的舊位置）。
- `app` 裡的 `chamber` 用戶端把 `CHAMBER_URL` 指到那個 socket，不帶憑證。

**2026-11-19 更正（`k8scc-0f [7c5b85]` 驗收時；讀數＋提案）**：初稿寫 loopback。im 的 pod 是 `hostNetwork: true`（拋棄式叢集的 im pod 上讀 `.spec.hostNetwork` → `true`；`CLAUDE.md` 也這樣寫），所以 `127.0.0.1` 是**節點的** loopback：節點網路命名空間裡的每個行程、同一節點上每個 host-network 的 pod 都連得到，不只「pod 裡的行程」。改成 **Unix socket，放在只有 `app` 與 bridge 兩個 container 掛的 `emptyDir` 裡**（jg-base 的 sidecar 要多這一個 volume）；bridge 不開任何 TCP。代價：`chamber` 用戶端要認得 socket 形式的位址（`unix:<path>`）——那是共用檔的修改，要 Mac 側一起收（tasks 2.5）。既有的 `talos-mcp`（8765）與 `netdev-mcp`（8766）有同一個暴露面；那是它們的事，這裡只記一筆，不在本 change 處理。

後果，明寫：能打開那個 socket 的行程（`app` 裡的任何行程）都能以 im 的名義收發——擋的是**憑證被帶走**，不是 `app` 內的冒用。驗章仍在 `app` 做，不經橋接程式：保留類不信伺服器，也不信橋接程式。

沒有 sidecar 的 pod（jg-base 還沒改）：`chamber-bridge` 可以在 `app` 裡由 `entrypoint.sh` 起，憑證就在 `app` 裡——**只給拋棄式叢集的第一次走通用**，啟動行印出「憑證在 app 內」。

### D-c 在線＝session 的活著訊號（裁定的行為＋提案的機制）

- `chamber-watch` 每 `CHAMBER_BEAT`（預設 20）秒檢查：tmux session `claude` 存在、pane 的前景行程是 `claude`。成立就對橋接程式送一次活著訊號。
- 橋接程式：`deliver` 模式下，最近一次訊號在 `3×CHAMBER_BEAT` 內 ⇒ 向伺服器維持 `online`；超過 ⇒ 設 `offline` 並停止長輪詢式的在線維持。沒收過訊號＝不在線。
- container 活著、`claude` 已結束 ⇒ 訊號停 ⇒ 不在線。橋接程式自己被殺 ⇒ 靠伺服器側的逾時（多久，沒量，tasks 1.3）。
- 「pane 的前景行程是 `claude`」是代理：`claude` 卡在登入畫面或信任對話框時行程也活著。所以活著訊號另外要求 pane 不是那兩種畫面（用 `inbox-watch` 現有的 pane 讀法）；判不出來 ⇒ 不送訊號。
- **2026-11-19 的讀數改了這一條**（tasks 1.2）：detached 起的 `claude` 停在一個一次性的功能詢問對話框上，行程活著、不是登入也不是信任畫面。所以判斷式改成正面表列：**pane 是閒置的空 prompt，或正在跑一個回合**（`esc to interrupt`）才送訊號；其他一律不送。spec 的對應句已跟著改（2026-11-19，`k8scc-0f [7c5b85]`）。

### D-d session 常駐，不是喚起（R20 裁定；機制是提案，可行性沒量）

`entrypoint.sh` 起一支 `session-keep`：tmux session `claude` 不存在就以 `claude-session` 起一個（detached），每 `SESSION_KEEP_INTERVAL` 秒檢查一次；`claude-session` 被 ttyd 呼叫時今天就是「有就接上、沒有才起」（`claude-session:379`，`tmux -u new-session -A -s claude -- claude`，讀 `origin/main e8d7522`）；`session-keep` 起的是同一個名字的 session，所以 ttyd 連進來會接上它。

- 它不讀任何訊息，所以 R19（不喚起）不受影響；2026-11-10「inbox 不起 headless agent」也不受影響——起的是互動式的 session，不是 `claude -p`，而且不是為了某一則訊息起的。
- **沒量**：沒有人接著 ttyd 時，detached tmux 裡的互動式 `claude` 起不起得來（終端機大小、首次的信任對話框、OAuth 過期時的畫面）；起來之後沒人看著，token 用量是多少（閒置的 session 不該有用量——要量）。tasks 1.2。這一格不成立，R20 在現有映像上就做不到，要回 root 重想。
- 開關 `SESSION_KEEP`，預設關。
- **它也改變 inbox 這條舊通道的行為**（2026-11-19 驗收時補）：今天 `inbox-watch` 只對「有人開過 ttyd 才存在」的 session 打字；session 常駐之後，丟進 inbox 的交辦會在**沒有人在 ttyd 前**的情況下被宣告並處理。這與 2026-11-10 的裁定（告訴正在跑的 session，不起 headless agent）字面上不衝突，但實際效果接近，ferry133 點頭 `SESSION_KEEP=1` 時要知道這一點。

### D-e 宣告：沿用 `inbox-watch` 的 pane 閘門（提案）

`chamber-watch` 在 `deliver` 模式下，從橋接程式讀「點名我或 all、還沒宣告過的訊息數（按房間）」；pane 閒置時打一行：

    💬 chamber: 2 message(s) for you in <room> — run `chamber read <room>` and handle them per your policy

- 只有房間名與數字，不含內文與寄件人——內文由 agent 自己用 `chamber read` 讀，照 policy 判斷。
- pane 閘門不重寫：把 `inbox-watch` 的 `pane_idle` 抽成兩支共用的一個檔，控制測試跑同一份。
- 沒有 session ⇒ 不宣告、不起任何東西；訊息留在伺服器。

### D-f 回程與並存（提案）

- `ack`／`decline`／`blocked`／`report` 用 `chamber send <room> --to <對方> --reply-to <那一則>`；這四個詞是訊息的第一個字，不是另外的訊息型別。
- 並存期間：交辦從哪裡來，回報就回哪裡——inbox 檔來的寫 `-report.md`，房間來的回房間。長的產出（超過一個畫面）仍寫成 workspace 裡的檔，房間裡只說路徑與結論。
- `observe` 模式下 im 不往房間送任何東西。

### D-g 設定（提案）

| env | 意思 | 預設 |
|---|---|---|
| `CHAMBER_URL` | 伺服器位址（bridge 用）；`app` 裡是 bridge 的 socket 位址 | 空＝整組不啟動 |
| `CHAMBER_MODE` | `observe`／`deliver` | `observe` |
| `CHAMBER_TOKEN_FILE` | 登入憑證檔（bridge 才有） | `/etc/chamber/token` |
| `CHAMBER_ACCOUNT` | 這座 im 的帳號名 | 無；缺＝不啟動並說缺什麼 |
| `CHAMBER_SIGNERS_DIR` | 公鑰清單與撤銷清單所在的目錄（ConfigMap） | `/etc/chamber/signers` |
| `CHAMBER_BEAT` | 活著訊號的間隔（秒） | 20 |
| `SESSION_KEEP` | `1`＝session 常駐 | 關 |

三種啟動狀態，啟動行各說一句：未設定（`CHAMBER_URL` 空）；設定不全（說缺哪一個，不啟動）；啟動（說模式、憑證在哪個 container、公鑰清單幾把）。

## Risks

- **[常駐的 session 沒人看著]** 它會讀房間裡的東西並動作。保留類有簽章擋；一般類的上限是 im 的權限 profile。`observe` 先行，就是為了在它動作之前先看它收到什麼。
- **[一行宣告打進不該打的畫面]** 沿用並共用 `inbox-watch` 的閘門與它的 controls。
- **[橋接程式是 pod 內的開放面]** 只開 Unix socket（pod 是 hostNetwork，loopback 等於節點）、只轉送列出的路徑；controls 有「其他路徑 404」與「bridge 沒有任何 TCP listener」。
- **[兩份檔分岔]** D-a；正本在本 repo，Mac 側是副本，靠那一側的雜湊比對（tasks 2.4）。
- **[在線讀數說謊]** 兩個方向都要 control：session 沒了仍顯示在線（R19 的答覆說不出來）；session 在卻顯示不在線（發送方白等）。

## Open Questions

1. ~~`chamber` 與 `directive-verify` 的正本住哪（D-a）。~~ 已裁定：本 repo（ferry133 2026-11-19）。
2. 沒有 sidecar 時允不允許憑證在 `app` 裡跑生產（提案：不允許）。
3. session 常駐起來的 `claude` 用哪一個權限 profile——與有人在 ttyd 前時相同，還是更窄。
4. 伺服器側多久把斷線的帳號標成不在線（tasks 1.3 量了再定 `CHAMBER_BEAT`）。
