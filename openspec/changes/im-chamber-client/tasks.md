> 全部在 `k8scc`。root：`fleet-ops/openspec/changes/agent-delegation-envelope`（tasks 第 6 組）。請求：`k8scc#70`。
> **驗收不由起草者（`fleet-ops-98 [047e9c]`，#70 的開單者）自收**——由 k8scc 目錄的 session 或 ferry133 收。
> 每格寫預期再跑；三種結果（通過／失敗／量不到）分開寫；每個負對照配一個正對照；刻意變異要確認套用。
> 第一次接上真的 im 在拋棄式叢集做，不在生產的 im 上做。**會改變生產叢集上 im 行為的合併（映像 pin、`CHAMBER_MODE=deliver`、`SESSION_KEEP=1`）停在那一步之前，由 ferry133 點頭。**

## 1. Spikes（結果會改 design）

- [x] 1.1 映像內的 OpenSSH 驗不驗得了 `sk-ecdsa-sha2-nistp256@openssh.com` 的簽章（人的金鑰是 Secure Enclave＋Touch ID）。已有的讀數（2026-11-17，拋棄式叢集的 im pod，OpenSSH 9.2）：ed25519 的簽章驗得過、撤銷清單有效
  - **讀數（2026-11-19 00:3x，`k8scc-0f [7c5b85]`，拋棄式叢集的 im pod `app` container，映像 `e8d7522`，`OpenSSH_9.2p1 Debian-2+deb12u10`、python 3.11.2、amd64）：驗得過。** 向量是 fleet-ops 的固定向量（2026-11-15 以一把 Secure Enclave 金鑰簽、簽完即刪，簽的人 `vector-person`；初稿寫「還沒有」是錯的，起草者已更正）。`directive-verify`（sha256 `a58e8189b42f7670…`，當天含 `--use` 的那一份，以 stdin 帶進 pod 的暫存目錄，跑完刪掉）：原向量 → 0；內文翻一個位元 → 1（`signature does not verify`）；空的公鑰清單 → 2。不經腳本直接 `ssh-keygen -Y verify`：0（`Good "chamber-directive" signature … with ECDSA-SK key`）；namespace 錯 → 255；identity 錯 → 255。同一份向量在 Mac（OpenSSH 10.2）上也是 0／1
  - **沒量**：ferry133 真的那把金鑰簽的指令（root 4.2，要他在 MacBook 上簽一次）；arm64 的映像；`--use` 與那本帳在 pod 裡的行為（這次刻意沒帶 `--use`，`HOME` 指到暫存目錄，沒碰 PVC 上的任何檔）
- [ ] 1.2 session 常駐可不可行：沒有人接著 ttyd 時，detached tmux 裡以 `claude-session` 起的互動式 `claude` 能不能到達 prompt（終端機大小、信任對話框、OAuth 過期時的畫面各一格）；到了之後閒置一小時的 token 用量。不成立 ⇒ 回 root 重想 R20 在現有映像上怎麼做
  - **讀數（2026-11-19 00:2x，`fleet-ops-98 [047e9c]`，拋棄式叢集的 im pod，Claude Code 2.1.261、tmux 3.3a，一次）**：`tmux -u new-session -d -s probe -x 160 -y 45 -- claude`（另一個 session 名，沒有人接著；原有的 `claude` session 沒碰）⇒ 起得來，已登入，5 秒內畫出畫面；**但停在一個沒預期到的對話框**（「Try the new fullscreen renderer?」，`❯ 1. Yes, try it`／`2. Not now`，Enter 確認），15 秒後仍在那裡，`pane_current_command=claude`。所以：(a) detached 起動這一半成立；(b) **「行程是 `claude`」不等於到了 prompt**——design D-c 只列登入與信任兩種畫面不夠，要改成「是閒置的空 prompt 才算」（`inbox-watch` 的 `pane_idle` 正好是這個判斷，那個對話框會被它的選單規則擋下）；(c) 常駐的 session 會卡在這種一次性的對話框上，沒有人按就永遠不在線——要嘛讓它看得見，要嘛預先關掉（哪個設定關它，沒查）。沒量：直接經 `claude-session` 起（這次是直接起 `claude`）、OAuth 過期的畫面、信任對話框、閒置的用量
- [ ] 1.3 伺服器側多久把一個不再同步的帳號標成不在線（橋接程式被 `kill -9` 之後量）；據此定 `CHAMBER_BEAT`
- [ ] 1.4 pane 的「登入畫面／信任對話框」長什麼樣（在映像上錄下來，像 `inbox-watch` 當初量 prompt 那樣）；D-c 的判斷式據此寫

## 2. 用戶端與驗章（root 6.2）

- [x] 2.1 `chamber/chamber`、`chamber/directive-verify` 進 repo；`Dockerfile` COPY 到 `/usr/local/bin/`
  - **做了（2026-11-19 06:43，`k8scc-0f [7c5b85]`）**：以 Mac 側當下的位元組進 repo——`chamber` sha256 `6f7ce8a2d00f9e19…`、`directive-verify` `a58e8189b42f7670…`（兩側各量一次，`fleet-ops-98 [047e9c]` 06:42 量到相同）。**沒有在檔頭加來源註記**：正本裁定住這裡（2.4），加了就不再逐位元組相同，而雜湊已記在這一行。`test/chamber-controls.sh` 20 格（出貨、起得來、用戶端的 1 與 2 分得開、固定 sk-ecdsa 向量的 0／1／2），四個變異各自轉紅（拿掉 COPY；驗不過當通過；沒有清單當失敗；連不上當失敗）。向量在 `test/chamber-vectors/sk-ecdsa/`
  - **沒量**：映像還沒建，所以「在映像裡起得來」這次沒量（tasks 1.1 量的是以 stdin 帶進 pod 的同一份 `directive-verify`；`chamber` 在 pod 裡沒跑過）
- [ ] 2.2 對照測試進 `test/`（用戶端 20 格、驗章 24 格＋只用一次 13 格、變異各一支）；CI 對一台本機拋棄式伺服器跑。伺服器起不來 ⇒ 那幾格是「量不到」，CI 不算綠
  - **範圍寫明（`fleet-ops-98 [047e9c]` 驗收 #74 時指出，2026-11-19）**：`test/chamber-controls.sh` 的 20 格裡 `--relayed` 與撤銷清單是 0 格、`--use` 只有 1 格。那些格（以及 `transcript:` 的 28 格）現在只在 Mac 側的 bench。搬過來時要改兩處：它們載入 `directive-sign`（不在本 repo，也不該進映像），而且測試字串裡有叢集名（本 repo 是公開的）
- [x] 2.3 `directive-verify` 在映像裡的預設：公鑰清單讀 `CHAMBER_SIGNERS_DIR`；`transcript:` 指標一律回 2（映像裡沒有別人的 transcript——確認不是剛好找不到，是不找）
  - **做了（2026-11-19 06:50，`k8scc-0f [7c5b85]`）**：`CHAMBER_SIGNERS_DIR` 有值時預設清單是 `<dir>/allowed_signers`（`--signers` 仍然贏；撤銷清單照舊是清單旁的 `revoked_signers`）。`CHAMBER_TRANSCRIPTS=off` 時每個指標回 2，在找任何檔之前就回，訊息說的是「這台主機不讀」而不是「找不到」。`Dockerfile` 以 `ENV` 設這兩個。`test/test_chamber_tools.py` V1–V5、T1–T8、I1–I2
  - ⚠️ **那個開關是預設，不是邊界**：`app` 裡的行程可以把環境變數拿掉。拿掉之後 im 讀得到的只有它自己的 transcript（人在 ttyd 打的字，那本來就是直接指令），讀不到別台主機上的
  - **沒量**：這兩個 `ENV` 在 pod 裡是否真的傳到 tmux 裡的 `claude`（`entrypoint.sh` → ttyd → tmux 這條路，讀的，沒量）；ConfigMap 掛上去之後的實際讀數（jg-base 側還沒有）
- [x] 2.5 （2.4 已裁定，正本在本 repo；改在這裡做，Mac 側重新安裝）`chamber` 用戶端認得 socket 形式的位址（`unix:<path>`；design D-b 2026-11-19 的更正）。這是共用檔的修改：Mac 側一起收，同一份測試向量加一格（經 socket 與經 https 的行為逐格相同）
  - **做了（2026-11-19 06:50，`k8scc-0f [7c5b85]`）**：`CHAMBER_URL=unix:<path>` 時用戶端經那個 socket 說 HTTP，**不讀憑證、不送 `Authorization`**（憑證由另一端加）。`test/test_chamber_tools.py` C1–C9：同一台假伺服器同時開 TCP 與 socket，`whoami` 兩邊答案相同；TCP 那邊看得到憑證（正對照）、socket 那邊沒有；socket 不存在 ⇒ 2；拒絕 ⇒ 兩邊都是 1。六個變異各自轉紅（socket 仍讀憑證；socket 帶憑證；開關失效；開關分大小寫；清單目錄被忽略；連不上當成拒絕）
  - **回歸**：Mac 側 bench 的三支對改過的 `directive-verify` 跑（`VERIFY_BIN`）：24／24、13／13、28／28，各有一格 skip（bench 沒開、沒給真的指標）
  - **沒量**：改過的 `chamber` 對真的伺服器（https）沒跑過——Mac 側的 20 格要 bench 伺服器；`send`（POST 帶內文）經 socket 沒有格，只有 GET；真的 bridge 還不存在
- [ ] 2.6 用過的指令的那本帳在 PVC 上（預設 `~/.claude/chamber/used_directives`，而 `~/.claude` 是 PVC——讀 `directive-verify` 與 `CLAUDE.md` 得來，沒在 pod 上量）：control 是「`--use` 之後重啟 pod，同一份再驗回 1」
- [ ] 2.4 正本住哪（design Open Questions 1）裁定後：另一側改成安裝的副本，加一格雜湊比對
  - **裁定（ferry133 2026-11-19，在 `k8scc-0f` 的 console，逐字）：「正本放 k8scc」。** 正本是本 repo 的 `chamber/`。Mac 側（`~/.claude/scripts/`）改成安裝的副本與雜湊比對那一格由 fleet-ops 收（`fleet-ops-98 [047e9c]` 已接）；那一半做完這一項才勾

## 3. 橋接程式（root 6.3 的一半）

- [x] 3.1 `chamber-bridge`：讀憑證、Unix socket（不開 TCP；**socket 檔的擁有者與權限要有一格**——兩個 container 的 uid 相不相同沒量，design 沒寫；`fleet-ops-98 [047e9c]` 2026-11-19 確認 `6cb3be3` 時指出）、路徑白名單、sync 位置落檔接著用、`observe`／`deliver`、啟動的三種狀態
  - **做了（2026-11-19 07:15，`k8scc-0f [7c5b85]`）**：`chamber/chamber-bridge`（只用標準函式庫），映像 COPY 到 `/usr/local/bin/`，**`entrypoint.sh` 不起它**。socket 0600。未設定與設定不全都**留著不退出**（與初稿「不啟動」不同：sidecar 退出會讓 pod NotReady，把 ttyd 的路由一起帶下來——`fleet-ops-98 [047e9c]` 起草 jg-base 側時指出，spec 已跟著改）。多做了初稿沒列的四件，都是收緊：不跟隨 redirect（跟了會把憑證送到別的主機）；伺服器確認憑證屬於 `CHAMBER_ACCOUNT` 之前什麼都不轉送；每一個 sync 都帶 `set_presence=offline`（sync 本身會讓帳號顯示在線）；送出的內文上限 1 MiB
  - **狀態行（草案，還不是契約）**：`chamber-status ts=<epoch> interval=<秒> mode=observe|deliver|unconfigured server=ok|unreachable|refused|unmeasured rooms=<排序、逗號分隔｜-｜unmeasured> online=yes|no|unmeasured`。`interval=` 是 `jg-base-56 [c30591]` 的輸入。`online` 是向伺服器讀回來的，不是 bridge 自己的說法。`chamber-verify-status`（`app` 那一行）還沒有，隨 `chamber-watch` 做
  - **`CHAMBER_URL_FILE`（同日加，`fleet-ops-98 [047e9c]` 的請求）**：`CHAMBER_URL` 沒有值時從檔案讀位址（預設 `/etc/chamber/url`，去掉結尾空白）；檔不存在或 0 位元組＝未設定，不是設定不全。原因是 `jg-base-56 [c30591]` 量到的：im 的 chart 不收 env 的 `secretKeyRef.optional`，位址又不能寫進公開的 jg-base，所以只能跟憑證走同一個 optional 的 Secret volume。變數有值時照舊贏過檔案。格 F1–F4 與兩個啟動狀態；兩個變異轉紅
  - **#76 驗收的四件（2026-11-19 07:42，`fleet-ops-98 [047e9c]` 對 bench Synapse 量的）**，都做了：(1) **Secret 晚到**——以空的位址檔啟動後再寫入，16 秒後 socket 仍沒出現；現在閒置時每個 beat 重讀全部輸入，齊了就自己啟動，原因只在改變時印一次。(2) 檔頭、spec、design 的「擋的是憑證被帶走」改字（見 design D-b 的更正）。(3) socket 路徑太長原本是 traceback 後退出，現在是「設定不全」的一行、留著。(4) 不帶目錄的 `CHAMBER_SOCKET`。新增 8 格（共 71），四個變異各自轉紅
  - ~~bench 上量到、還沒解的一格（`online` 自己問與別人看的落差）~~ **撤回**：`fleet-ops-98 [047e9c]` 在 #77 的驗收更正——那是只讀了 log 最後一行造成的；同一瞬間讀三個來源（別人看、im 經 socket 自己問、狀態行），+10 s／+45 s／+90 s 三者一致。留下的讀數只有：`set_presence=offline` 是「不碰」不是「設成離線」；直接 `PUT presence online` 後不續，+45 s 仍在線、+90 s 不在線（三個點，確切的界沒量）
  - **真的伺服器已量（bench，loopback http）**：啟動、`whoami`、`rooms`、收、`deliver` 送、`observe` 拒送、管理路徑 404、沒有 TCP listener、位址從檔案讀、憑證不在 log——十格事前寫的預期全部相符（#76 的驗收留言）
  - **沒做、沒量**：在線維持（4.1；所以 `deliver` 模式下 im 目前也不在線）；sync 位置檔預設放在 socket 的目錄，那是 emptyDir，pod 重啟就沒了，重啟後的第一次同步會拿到舊位置而重算一些訊息——只影響 log 裡的數字，宣告（5.2）做之前要回來處理；憑證輪替要重啟 bridge；**真的伺服器、真的 pod、兩個 container 之間的 socket 都沒跑過**
- [x] 3.2 controls：白名單外的路徑被拒；bridge 沒有任何 TCP listener（正對照：同一份列表看得到 ttyd 的 port）；憑證不出現在 log 與任何回應；`observe` 下 `send` 被拒、在線讀數不變；伺服器連不上時用戶端得到「量不到」不是「不在線」
  - **做了（同上）**：`test/test_chamber_bridge.py` 71 格（含 `CHAMBER_URL_FILE` 的 8 格、驗收四件的 8 格）：bridge 以出貨的樣子當行程跑，對一台手寫的假伺服器，真的 `chamber` 用戶端經 socket 走。16 條白名單外的路徑全是 404 且伺服器一條都沒看到；沒有 TCP listener（`lsof`／`ss`，兩者都沒有 ⇒ 量不到 ⇒ 不算綠；正對照是同一份列表看得到假伺服器的 port）；憑證不在任何回應與 log；`observe` 下 `chamber send` 回 1；伺服器連不上 ⇒ 用戶端回 2（兩格：從沒連上、確認過之後才斷）。十五個變異各自轉紅。**第一輪變異抓到兩個對照的毛病**：「每個 sync 都要求不在線」那格在 bridge 自己還沒 sync 時只讀到測試自己送的那一個（時序，乾淨的 run 剛好都過）；「連不上 ⇒ 2」那格被「帳號還沒確認」的路徑遮住，變異存活——兩格都改了
  - **假伺服器是照用戶端用到的路徑手寫的，不是 Matrix**：這些格量的是 bridge，不是協定
- [ ] 3.3 在沒有 sidecar 的 pod 上由 `entrypoint.sh` 起（只給拋棄式叢集），啟動行說「憑證在 app 內」

## 4. 在線與常駐（root 6.3 的另一半、6.4）

- [x] 4.1 `chamber-watch` 的活著訊號（design D-c）與橋接程式的在線維持
  - **做了（2026-11-19 08:06，`k8scc-0f [7c5b85]`）**：`chamber/chamber-watch`（bash，在 `app` 裡，`entrypoint.sh` 起；`CHAMBER_URL` 空就印一行結束，所以現有的 pod 只多一行 log）。每個 beat：session 存在、pane 的前景行程是 `claude`、而且 pane 是閒置的空 prompt 或正在跑回合，才經 socket 送 `POST /bridge/beat`。bridge 在 `deliver` 下：訊號在 3 個 beat 內 ⇒ 每一輪（上限 20 s，比 bench 量到的 45–90 s 密）對伺服器 `PUT presence online`；訊號停 ⇒ 設 `offline` 一次；bridge 結束時也設 `offline`。`observe` 下訊號收了但什麼都不設。狀態行的 `online` 仍是向伺服器讀回來的
  - `chamber-verify-status ts= interval= signers= revoked= sshkeygen=` 由 `chamber-watch` 每個 beat 印到 `app` 的 log（`signers`／`revoked` 是金鑰數，清單不在就是 `unmeasured`）。**它只說清單讀得到，不說驗過一份簽章**
  - **沒量**：真的 tmux 與 Claude Code（`pane_current_command` 是 `claude` 這一點是 tasks 1.2 在 pod 上讀過一次的）；真的 Synapse 上 20 s 一輪夠不夠密；pod 裡 `app` 的 uid 開不開得了 bridge 建的 0600 socket（兩個 container 都是 uid 0 是 jg-base 側說的，沒量）
- [ ] 4.2 controls（拋棄式叢集）：殺掉 `claude` ⇒ 三個間隔內顯示不在線、FAC 側 `chamber send` 回 3；session 回來 ⇒ 在線（正對照）；卡在登入畫面 ⇒ 不在線；只有橋接程式活著 ⇒ 不在線
- [ ] 4.3 `session-keep`（`SESSION_KEEP=1`）：pod 啟動時起、掉了重起、ttyd 接上同一個；起不來時 log 與在線讀數看得見。依 1.2 的讀數
  - **程式與假的對照做了（2026-11-19 08:31，`k8scc-0f [7c5b85]`）；這一項不勾，因為它的實質是 pod 上的讀數**。`session-keep`（`entrypoint.sh` 起，`SESSION_KEEP` 不是 `1` 就印一行結束——預設關，現有的 pod 只多一行 log）。session 不在時以 `CLAUDE_SESSION_DETACHED=1 claude-session` 起：`claude-session` 多一個模式，照常做 MCP 註冊與權限設定，然後 `tmux new-session -d -x 200 -y 50`，沒有歡迎詞、沒有 inbox 的 8 秒停頓；ttyd 之後連進來照舊 `-A` 接上同一個。已存在的 session 不動。起不來只說一次。**它不讀任何訊息**，也不判斷 session 有沒有到 prompt——那由 `chamber-watch` 的訊號與 log 顯示
  - controls：`test/session-keep-controls.sh` 36 格（跑的是真的 `claude-session`）。七個變異各自轉紅；**其中一個第一輪存活**（拿掉 `session-keep` 自己的「已存在就不動」）：「`claude-session` 完全沒被跑」那格在沒有 MCP 要註冊時分不出「沒跑」與「跑了但沒事做」，已改
  - ⚠️ **一次沒解釋的紅**：這份對照第一次跑紅了 10 格（都是要跑 `claude-session` 的那些），接著連跑 10 次全綠，成因沒找到——第一次的暫存目錄已經刪掉。現在紅的 run 會印出各格的 log。若 CI 上再出現，照印出來的查，不要重跑了事
  - **沒量（都要 pod）**：detached 起的 `claude` 到不到得了 prompt（1.2 量到它會停在一次性的對話框）；ttyd 連上之後畫面大小有沒有跟著改；閒置的用量；OAuth 過期時的樣子
- [ ] 4.4 不喚起的負對照（root 6.4）：`SESSION_KEEP` 關、沒有 session、送一份驗得過的直接指令 ⇒ 沒有任何行程被起、發送方得到「不在線」。正對照：同一份指令在 session 存在時被宣告

## 5. 宣告與回程（root 6.3、6.5）

- [x] 5.1 pane 閘門抽成 `inbox-watch` 與 `chamber-watch` 共用的一個檔；`test/inbox-watch-controls.sh` 原有的格全數照過
  - **做了（2026-11-19 07:27，`k8scc-0f [7c5b85]`）**：`pane-gate`（被 source 的 shell 檔），樣式與 `pane_idle` 原封搬過去，`inbox-watch` 從自己旁邊讀它；映像 COPY 到 `/usr/local/bin/pane-gate`。**閘門檔不在 ⇒ `inbox-watch` 不啟動、說出來、什麼都不打**（不是「沒有閘門照打」）。原有 57 格照過，新增 7 格（共 64）。五個變異各自轉紅：閘門永遠回閒置（14 紅）、看不到跑中的回合（2）、忽略下方選單（1）、拿掉 dim 規則（1）、容忍閘門檔不在（2）
  - **順手補的一個洞**：`test/inbox-watch-controls.sh` 原本只在 `build.yaml`（推到 main 之後）跑，不在 PR 的 `controls.yaml`——動到 `inbox-watch` 的 PR，它的對照是合併之後才第一次在 Linux 上跑。已加進 `controls.yaml`
  - **沒量**：新映像裡 `inbox-watch` 真的找得到 `/usr/local/bin/pane-gate`（讀的：`BASH_SOURCE` 在經 PATH 執行時是完整路徑）。**這是生產 im 現在就在跑的元件**，所以 bump 到含這一顆的映像之後，要在拋棄式叢集的 pod log 確認啟動行是 `watching …` 而不是 `the pane gate is missing`
- [x] 5.2 `chamber-watch` 的宣告（design D-e）與 controls（閒置時宣告一次、permission prompt 時不打字、點名別人的不宣告、送失敗不標記）
  - **做了（同上）**：bridge 在 `deliver` 下按房間記「點名我或 all、還沒宣告」的數目（`GET /bridge/pending`，一行一間 `<數目> <房間>`，沒有內文與寄件人；`observe` 下永遠是空的）。`chamber-watch` 在 pane 閒置時一間打一行，**每打一行之前重驗一次閘門**（上一行會開始一個回合），打成功才 `POST /bridge/announced`，以數目扣而不是清空（讀清單之後才到的訊息不會被吃掉）。session 太新（`CHAMBER_WATCH_SETTLE`，預設 30 s）不打
  - controls：`test/chamber-watch-controls.sh` 84 格（假 tmux／sleep／curl，pane 樣本是從 `inbox-watch` 的對照裡抽出來的同一批）；`test/test_chamber_bridge.py` 71 → 100 格，其中 7 格是**真的 `chamber-watch` 對真的 bridge**。變異：`chamber-watch` 十一個、bridge 新增的部分八個，各自轉紅。**有一個第一輪存活**：拿掉「閒置才宣告」的外層判斷仍全綠，因為每一行前的重驗擋住了同一件事——兩道是同一個前提；補了一格讓外層有自己的對照（跑回合時連清單都不問）
  - **房間名不能帶話（2026-11-19 08:15；提交後的自動安全檢查指出，成立）**：宣告那一行會被 agent 當成輸入讀，而房間名是伺服器上設的。第一版只把非文字字元換成底線——沒限長度、Unicode 字母照過、`chamber-watch` 完全信 bridge 給的名字，而且換過的名字 `chamber read` 也用不了。現在：**名字本身是 1–64 個 `A-Z a-z 0-9 . _ -` 才印名字**，bridge 與 `chamber-watch` 各驗一次；其餘的房間合併成一行固定的句子（不含任何來自伺服器的字），請 agent 自己跑 `chamber rooms`。pod log 與狀態行裡的房間名也改成 ASCII、最多 64 個字元（log 也有 agent 在讀）。格：`chamber-watch` 71 → 84、bridge 95 → 100；五個變異，四個轉紅，**一個存活並照實留著**：`typeable` 裡的 `LC_ALL=C` 拿掉後在 bash 5.3 上仍全綠（那裡範圍預設就是 ASCII），它是給舊 shell 的保險，沒有能鑑別它的格
  - **這條規則對房間命名有要求**：root 的房間名若含空白或非 ASCII，im 收到的就是那句固定的話而不是房間名。請 root 確認命名規則落在這個字元集裡
  - **待處理的已知缺口**：「還沒宣告」的數目在 bridge 的記憶體裡，bridge 重啟就歸零；而 sync 位置檔在 emptyDir，pod 重啟後會從舊位置重算——兩者合起來，pod 重啟後可能重複宣告或漏宣告。訊息本身都還在房間裡（`chamber read` 讀得到），所以是多一行或少一行提醒，不是掉訊息。拋棄式叢集走通之前要定：位置檔放 PVC，或重啟後一律「從現在開始」
- [ ] 5.3 回程（design D-f）：四個詞、`--reply-to`、並存期間「從哪裡來回哪裡」；`observe` 下不送

## 6. policy 與文件（root 6.6）

- [x] 6.1 `im-policy.md` 新節「誰可以交辦你」「chamber」；`test/im-policy-controls.sh` 加格
  - **做了（2026-11-19 08:37，`k8scc-0f [7c5b85]`）**：兩節加在檔尾，內容照 `~/coding/CLAUDE.md`「agent 之間的委派」改寫成 im 的版本（類別自己判、一般類直接做、保留類要驗過的指令並讀原文、`--use`、`transcript:` 一律無效、沒簽章的話不是指令、先回再做、秘密不進房間）。對照 188 → 274 格：兩節各一張陳述清單，每一句拿掉都會紅；政策引用的那一行是從 `chamber-watch` 抽出來比的
  - ⚠️ **有一句是我讀出來的，不是裁定，請 ferry133 或 root 確認**：「本政策其他各節的界線照舊」——各節寫明在 im 權限內、由工具把關的事（重啟節點、家庭自動化、路由器的八支寫入工具）不因為請求來自房間而多一道簽章。另一種讀法是「房間來的請求一律照六條重判，重啟節點算破壞性操作」。我選前者的理由：那些事 ferry133 已各自裁定在 im 權限內、把關在伺服端；但把 FAC agent 的一句話接到重啟節點上，是這條通道新開的面
  - **多寫的一句**（初稿沒列）：chamber 的登入憑證不是 im 的，不要去讀，**包括用 `kubectl` 讀那個 Secret**——sidecar 擋不住 cluster-admin（design D-b 的更正），所以這一格只能靠政策
  - **沒做**：6.3 的冷讀
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
