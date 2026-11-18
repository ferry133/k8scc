Part of: `fleet-ops` / `agent-delegation-envelope`

**Owning repo**：`k8scc` —— 本 sub-change 改的檔案（映像內的 chamber 用戶端與驗章工具、橋接程式、`claude-session`、`entrypoint.sh`、`inbox-watch`、`im-policy.md`、`CLAUDE.md`、`test/`）全在這裡。root change 住在 private 的 `fleet-ops`，持有跨 repo 的契約（房間與成員、指令格式、收件方流程、裁定表）；**設計不在這裡複述，以那裡為準**。pod spec（sidecar container、登入憑證的 Secret、公鑰清單的 ConfigMap）屬 jg-base 的姊妹 sub-change（`jg-base#248`）。請求在 `k8scc#70`。

**作者與驗收**：由 `fleet-ops-98 [047e9c]` 依 ferry133 2026-11-19 的直接指示（「go ahead with section 6」）起草——k8scc 的 session 自 2026-11-15 起都不在線，#70 四天沒有人接。**k8scc 的 session 隨時可以接手或改寫**；作者同時是 #70 的開單者，所以**不自收**：驗收由 k8scc 目錄的 session 或 ferry133 收。

本 repo 是 public：叢集名、伺服器位址、成員名單都不寫在這裡。

## Why

im 今天只有一條單向、慢、看不到對方的通道：FAC 用 `kubectl exec` 把檔案放進 `~/workspace/inbox/`，`inbox-watch` 在 session 開著且閒置時打一行字；im 的回報留在同一個目錄等人來讀；im 沒有任何辦法主動找 FAC（root 的 Why 有量到的代價）。root change 把 agent 之間的溝通搬到 chamber——一台自架的 Matrix 伺服器上、成員制的房間——並規定保留類的動作只憑人簽章的逐字指令。k8scc 的份額是 **im 這一側**：讓映像裡的 im 成為它那一間房間的成員。

ferry133 對 im 這一側裁定了三件會改變現有行為的事（原話在 root 的裁定表）：

- **R19（2026-11-15）不喚起。** 任何訊息——包含人簽的直接指令——都不啟動 session；收件方不在線時，發出的一方立刻得到「不在線」。
- **R20（2026-11-15）im 隨時在線。** 不在線是故障。
- **「在線」＝有一個能動作的 session**（2026-11-15）：常駐程式自己連著不算。橋接可以是獨立的行程，他偏好 sidecar container，條件是與 agent 本體同步上下線。

現況與 R20 的距離（讀 `origin/main e8d7522`，不是量測）：`claude` 是 ttyd 有人連進來時由 `claude-session` 起的；沒有人開過 ttyd 的 pod 沒有 session。所以「隨時在線」不是加一個用戶端就有，要有東西在 pod 啟動時起 session、掉了再起。

## What Changes

- **新增：`chamber` 與 `directive-verify`**（COPY 進映像）。與 Mac 側是**同一份檔**（只用標準函式庫＋`ssh-keygen`），不是第二份實作；本 repo 帶著同一份對照測試當測試向量。行為由 root 的 spec 定義：點名過濾（過濾不是隔離）、送出的四種結果、驗章的三種結果、指令只用一次。
- **新增：`chamber-bridge`**（給 sidecar container 跑；同一個映像、`command` 覆寫，與 talos-mcp／netdev-mcp 同形）。它握有登入憑證，對 `app` 只開 loopback：`app` 裡的用戶端經它收發，**登入憑證不進 `app`**。它只在收到 `app` 送來的「session 活著」訊號期間把 im 顯示為在線；訊號停了就顯示不在線（量不到當成不在線）。
- **新增：`chamber-watch`**（在 `app`，由 `entrypoint.sh` 起，與 `inbox-watch` 並列並共用它的 pane 閘門）：(a) `claude` 行程活著時向橋接程式送活著訊號；(b) 有點名 im 的新訊息、且 pane 閒置時，在 session 裡打一行宣告（只說哪一間有幾則，不含內文）。
- **新增：session 常駐**。pod 啟動時起 tmux session `claude`，掉了重起；ttyd 連進來是接上既有的 session，不是另起一個。起不來（未登入、卡在對話框）時 pod log 與在線讀數都看得見。**這不是喚起**：它不由任何訊息觸發。
- **兩種模式**，環境變數 `CHAMBER_MODE`，預設 `observe`：
  - `observe`：橋接程式連上、收到的東西只寫 log（計數，不含內文）；不宣告、不顯示在線；交辦仍走舊 inbox。
  - `deliver`：宣告、在線讀數、回程都生效。切換由 ferry133 點頭。
  - `CHAMBER_URL` 空或未設＝整組功能不啟動，一行 log。
- **修改：`im-policy.md`** 新節「誰可以交辦你」與「chamber」：房間裡 FAC agent 的一般類請求、人簽的指令（含保留類）、其餘一律資料；`transcript:` 指標在 im 上一律當成沒有指令；第一個動作回 `ack`／`decline`／`blocked`；秘密值不進房間。
- **修改：`operator-inbox`**（MODIFIED delta）：「沒有 session 就什麼都不做」那一句改成「沒有 session 是故障，由 session 常駐處理；`inbox-watch` 自己仍然不起任何東西」；並存期間 inbox 檔照舊宣告。
- **測試**：`test/chamber-controls.sh`（對一台本機拋棄式伺服器）與既有三支 controls 的新格；每格寫預期、各有正對照；刻意變異各紅一次。

**不做**：chamber 伺服器的部署（jg-extras）；pod spec（jg-base）；非 Claude 的 runtime；Matrix 端到端加密；把 `inbox-watch` 拿掉（遷移完成後另議）。

## Capabilities

### New Capabilities

- `im-chamber-member`：映像這一側的契約——設定與啟動狀態、兩種模式、橋接程式與憑證隔離、活著訊號與在線讀數、session 常駐、宣告、不喚起、回程、驗章工具在映像內可用。jg-base 的 sidecar 據此寫。

### Modified Capabilities

- `operator-inbox`：見上。

## Impact

- **映像**：多三支程式與兩份 COPY；不加套件（`openssh-client`、`python3` 已在）。
- **每座 im**：新映像 bump 後，`CHAMBER_URL` 未設的 pod 行為不變**除了一件**——session 常駐會在 pod 啟動時起 `claude`（今天是有人開 ttyd 才起）。這一件單獨有開關（`SESSION_KEEP`，預設關），**打開它會改變生產叢集上 im 的行為，要 ferry133 點頭**。
- **jg-base**：sidecar container、Secret 只掛進 sidecar、ConfigMap（伺服器位址、公鑰清單兩個檔）、`CHAMBER_MODE`／`SESSION_KEEP` 的值。
- **未量、可能改變設計的**：見 tasks 第 1 組。
