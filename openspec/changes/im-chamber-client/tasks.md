> 全部在 `k8scc`。root：`fleet-ops/openspec/changes/agent-delegation-envelope`（tasks 第 6 組）。請求：`k8scc#70`。
> **驗收不由起草者（`fleet-ops-98 [047e9c]`，#70 的開單者）自收**——由 k8scc 目錄的 session 或 ferry133 收。
> 每格寫預期再跑；三種結果（通過／失敗／量不到）分開寫；每個負對照配一個正對照；刻意變異要確認套用。
> 第一次接上真的 im 在拋棄式叢集做，不在生產的 im 上做。**會改變生產叢集上 im 行為的合併（映像 pin、`CHAMBER_MODE=deliver`、`SESSION_KEEP=1`）停在那一步之前，由 ferry133 點頭。**

## 1. Spikes（結果會改 design）

- [ ] 1.1 映像內的 OpenSSH 驗不驗得了 `sk-ecdsa-sha2-nistp256@openssh.com` 的簽章（人的金鑰是 Secure Enclave＋Touch ID）。向量由 fleet-ops 提供（root tasks 4.2，**還沒有**——要 ferry133 在 MacBook 上簽一次）。已有的讀數（2026-11-17，拋棄式叢集的 im pod，OpenSSH 9.2）：ed25519 的簽章驗得過、撤銷清單有效；`sk-ecdsa` 沒量
- [ ] 1.2 session 常駐可不可行：沒有人接著 ttyd 時，detached tmux 裡以 `claude-session` 起的互動式 `claude` 能不能到達 prompt（終端機大小、信任對話框、OAuth 過期時的畫面各一格）；到了之後閒置一小時的 token 用量。不成立 ⇒ 回 root 重想 R20 在現有映像上怎麼做
- [ ] 1.3 伺服器側多久把一個不再同步的帳號標成不在線（橋接程式被 `kill -9` 之後量）；據此定 `CHAMBER_BEAT`
- [ ] 1.4 pane 的「登入畫面／信任對話框」長什麼樣（在映像上錄下來，像 `inbox-watch` 當初量 prompt 那樣）；D-c 的判斷式據此寫

## 2. 用戶端與驗章（root 6.2）

- [ ] 2.1 `chamber/chamber`、`chamber/directive-verify` 進 repo（與 Mac 側逐位元組相同；檔頭記來源的雜湊與日期）；`Dockerfile` COPY 到 `/usr/local/bin/`
- [ ] 2.2 對照測試進 `test/`（用戶端 20 格、驗章 24 格＋只用一次 13 格、變異各一支）；CI 對一台本機拋棄式伺服器跑。伺服器起不來 ⇒ 那幾格是「量不到」，CI 不算綠
- [ ] 2.3 `directive-verify` 在映像裡的預設：公鑰清單讀 `CHAMBER_SIGNERS_DIR`；`transcript:` 指標一律回 2（映像裡沒有別人的 transcript——確認不是剛好找不到，是不找）
- [ ] 2.4 正本住哪（design Open Questions 1）裁定後：另一側改成安裝的副本，加一格雜湊比對

## 3. 橋接程式（root 6.3 的一半）

- [ ] 3.1 `chamber-bridge`：讀憑證、loopback、路徑白名單、sync 位置落檔接著用、`observe`／`deliver`、啟動的三種狀態
- [ ] 3.2 controls：白名單外的路徑被拒；從 pod IP 連不到；憑證不出現在 log 與任何回應；`observe` 下 `send` 被拒、在線讀數不變；伺服器連不上時用戶端得到「量不到」不是「不在線」
- [ ] 3.3 在沒有 sidecar 的 pod 上由 `entrypoint.sh` 起（只給拋棄式叢集），啟動行說「憑證在 app 內」

## 4. 在線與常駐（root 6.3 的另一半、6.4）

- [ ] 4.1 `chamber-watch` 的活著訊號（design D-c）與橋接程式的在線維持
- [ ] 4.2 controls（拋棄式叢集）：殺掉 `claude` ⇒ 三個間隔內顯示不在線、FAC 側 `chamber send` 回 3；session 回來 ⇒ 在線（正對照）；卡在登入畫面 ⇒ 不在線；只有橋接程式活著 ⇒ 不在線
- [ ] 4.3 `session-keep`（`SESSION_KEEP=1`）：pod 啟動時起、掉了重起、ttyd 接上同一個；起不來時 log 與在線讀數看得見。依 1.2 的讀數
- [ ] 4.4 不喚起的負對照（root 6.4）：`SESSION_KEEP` 關、沒有 session、送一份驗得過的直接指令 ⇒ 沒有任何行程被起、發送方得到「不在線」。正對照：同一份指令在 session 存在時被宣告

## 5. 宣告與回程（root 6.3、6.5）

- [ ] 5.1 pane 閘門抽成 `inbox-watch` 與 `chamber-watch` 共用的一個檔；`test/inbox-watch-controls.sh` 原有的格全數照過
- [ ] 5.2 `chamber-watch` 的宣告（design D-e）與 controls（閒置時宣告一次、permission prompt 時不打字、點名別人的不宣告、送失敗不標記）
- [ ] 5.3 回程（design D-f）：四個詞、`--reply-to`、並存期間「從哪裡來回哪裡」；`observe` 下不送

## 6. policy 與文件（root 6.6）

- [ ] 6.1 `im-policy.md` 新節「誰可以交辦你」「chamber」；`test/im-policy-controls.sh` 加格
- [ ] 6.2 `CLAUDE.md`：新節與 Runtime Configuration 表的 env；`operator-inbox` 的 delta 對應的文字
- [ ] 6.3 冷讀：一個沒讀過 root 的 session 只憑 `im-policy.md`，回答四則訊息（一般／保留有指令／保留而推導超出／人沒簽章的破壞性請求）做還是不做

## 7. 整體 controls（root 6.7；拋棄式叢集）

- [ ] 7.1 讀寫別的房間、邀人、開房間（伺服器拒絕——這三格量的是伺服器，在這裡只確認用戶端回 1 而不是 2）
- [ ] 7.2 偽造簽章、清單外的金鑰、過期指令、用過的指令、`transcript:` 指標：`directive-verify` 各回 1／1／1／1／2，im 都不動作
- [ ] 7.3 一般訊息與直接指令都不喚起（4.4）
- [ ] 7.4 `observe` → `deliver` 的切換與退回：退回後行為與切換前逐格相同

## 8. 收尾

- [ ] 8.1 契約（spec `im-chamber-member`）定稿時貼到 `jg-base#248`
- [ ] 8.2 歸檔時回頭更新 root 的 Sub-changes 表與 tasks 第 6 組
