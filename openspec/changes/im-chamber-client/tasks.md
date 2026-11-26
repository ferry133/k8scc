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
  - **讀數（2026-11-25，`fleet-ops-02 [e3795b]`，jg-jcc2，`SESSION_KEEP=1`（jg-jcc2 `39ecbd2`，ferry133 當天在 k8scc-bf 的 console 選「在 jg-jcc2 開」，只這一座）；每格事前寫預期，全文在 k8scc#70 issuecomment-7158510603）**：經 `claude-session` 的 detached 起動（映像 `17ce78a`，15:33–15:37）到了空提示，沒有對話框，session 起來後第一行狀態就是 `online=yes`。**閒置 62 分鐘**（映像 `550287e`，16:14–17:16，沒有人碰）：同一個 session（created 不變）、畫面仍是空提示；`~/.claude/projects` 沒有新的 transcript 檔、沒有任何檔在 session 建立後被寫過；186 行狀態全是 `online=yes`、`server=ok`；session-keep 沒有重起，pod 重啟 0
  - **這一項不勾。沒量**：token 用量本身（量到的是「沒有任何一輪對話被寫下」，帳號那一側的用量讀不到，不經 transcript 的背景請求看不到）；全新的設定卷（這個卷以前登入過，11-19 的那個一次性對話框這次沒出現，哪一個設定關掉它沒查）；信任對話框；OAuth 過期時的畫面
- [ ] 1.3 伺服器側多久把一個不再同步的帳號標成不在線（橋接程式被 `kill -9` 之後量）；據此定 `CHAMBER_BEAT`
- [ ] 1.4 pane 的「登入畫面／信任對話框」長什麼樣（在映像上錄下來，像 `inbox-watch` 當初量 prompt 那樣）；D-c 的判斷式據此寫
  - **錄到的（截至 2026-11-26）**：信任對話框（`TRUST_REAL`，jg-jcc2，2026-11-10）；登入過期後的 pane（`NOT_LOGGED_IN_REAL`，jcom，2026-11-25，Claude Code 2.1.261，`fleet-ops-02 [e3795b]` 貼在 k8scc#70 issuecomment-7166698330）。**沒有錄到**：第一次登入的畫面。所以不勾

## 2. 用戶端與驗章（root 6.2）

- [x] 2.1 `chamber/chamber`、`chamber/directive-verify` 進 repo；`Dockerfile` COPY 到 `/usr/local/bin/`
  - **做了（2026-11-19 06:43，`k8scc-0f [7c5b85]`）**：以 Mac 側當下的位元組進 repo——`chamber` sha256 `6f7ce8a2d00f9e19…`、`directive-verify` `a58e8189b42f7670…`（兩側各量一次，`fleet-ops-98 [047e9c]` 06:42 量到相同）。**沒有在檔頭加來源註記**：正本裁定住這裡（2.4），加了就不再逐位元組相同，而雜湊已記在這一行。`test/chamber-controls.sh` 20 格（出貨、起得來、用戶端的 1 與 2 分得開、固定 sk-ecdsa 向量的 0／1／2），四個變異各自轉紅（拿掉 COPY；驗不過當通過；沒有清單當失敗；連不上當失敗）。向量在 `test/chamber-vectors/sk-ecdsa/`
  - **沒量**：映像還沒建，所以「在映像裡起得來」這次沒量（tasks 1.1 量的是以 stdin 帶進 pod 的同一份 `directive-verify`；`chamber` 在 pod 裡沒跑過）
- [x] 2.2 對照測試進 `test/`（用戶端 20 格、驗章 24 格＋只用一次 13 格、變異各一支）；CI 對一台本機拋棄式伺服器跑。伺服器起不來 ⇒ 那幾格是「量不到」，CI 不算綠
  - **範圍寫明（`fleet-ops-98 [047e9c]` 驗收 #74 時指出，2026-11-19）**：`test/chamber-controls.sh` 的 20 格裡 `--relayed` 與撤銷清單是 0 格、`--use` 只有 1 格。那些格（以及 `transcript:` 的 28 格）現在只在 Mac 側的 bench。搬過來時要改兩處：它們載入 `directive-sign`（不在本 repo，也不該進映像），而且測試字串裡有叢集名（本 repo 是公開的）
  - **驗章的那一半做了（2026-11-25，`k8scc-bf [97703c]`）；這一項不勾，用戶端對真伺服器的 20 格還沒有**。`test/test_chamber_tools.py` 多 40 格：D1–D17（簽章形式，搬自 Mac 側的 `directive-test.py`，多一格 `CHAMBER_ACCOUNT`）、R1–R4（撤銷清單，#74 驗收時指出是 0 格）、O1–O7c（只用一次，搬自 `once-test.py`）。金鑰在測試時產生；header 是 `directive-sign` 格式的第二份定義（D1 是它的正對照）；測試字串不含叢集名。十四個變異各紅一次（Mac 側原有的九加三，另加「不讀 `CHAMBER_ACCOUNT`」「不用撤銷清單」）
  - **用戶端的那一半做了（2026-11-25，`k8scc-bf [97703c]`），據此勾**。`test/test_chamber_client_server.py`：測試時產生一台拋棄式 Synapse（loopback、http、五個帳號、三間房，跑完全刪），對它跑出貨的 `chamber/chamber`，20 格，搬自 Mac 側的 `chamber-test.py`。帳號改名（不含人名與叢集名），伺服器在測試裡自己建，不掛 guard 模組（那是生產伺服器的邀請規則，不是用戶端的）。三種結果：0／1／2，起不來是 2，CI 當紅。只在 `controls.yaml`（每張 PR）跑，不在 `build.yaml`：那個 job 握有 `packages: write`，而且第 16 格等的是伺服器的 presence 逾時，不該擋映像發布（fleet-ops-02 審 #141 時指出）。釘 `matrix-synapse==1.165.0`（Mac 側 bench 的版本）。讀數：本機 Synapse 1.143.0 上 20/20；CI 上 1.165.0 上 20/20
  - **變異**：`test/chamber-client-mutate.py`（六個，搬自 `chamber-mutate.py`；不進 CI，每個變異起一台伺服器）。第一輪在兩個版本上都有一個存活：「`wait` 又相信 initial sync 的位置」。成因是那一格之前沒有人做過 initial sync，所以沒有快取的舊位置可以錯；在過期訊息之前加一個 1 秒的 `wait` 之後，六個在本機（1.143.0）與 CI（1.165.0，臨時步驟，合併前拿掉）都被抓到
  - **沒量**：https（伺服器只聽 loopback 的 http）；bridge 的 socket 形式對真的伺服器；`members`、`--unanswered`；生產的 Synapse 版本是不是 1.165.0 沒有查
  - **沒搬的**：固定的 sk-ecdsa 向量（已在 `test/chamber-controls.sh`）、經真房間來回的 19a／19b、`directive-sign` 自己的拒絕（那支不在本 repo）、`transcript:` 的 28 格（這裡只有 T1–T8 與 O7）。**用戶端 20 格要一台拋棄式伺服器，CI 裡還沒有**；現有的用戶端格都對手寫的假伺服器
- [x] 2.3 `directive-verify` 在映像裡的預設：公鑰清單讀 `CHAMBER_SIGNERS_DIR`；`transcript:` 指標一律回 2（映像裡沒有別人的 transcript——確認不是剛好找不到，是不找）
  - **做了（2026-11-19 06:50，`k8scc-0f [7c5b85]`）**：`CHAMBER_SIGNERS_DIR` 有值時預設清單是 `<dir>/allowed_signers`（`--signers` 仍然贏；撤銷清單照舊是清單旁的 `revoked_signers`）。`CHAMBER_TRANSCRIPTS=off` 時每個指標回 2，在找任何檔之前就回，訊息說的是「這台主機不讀」而不是「找不到」。`Dockerfile` 以 `ENV` 設這兩個。`test/test_chamber_tools.py` V1–V5、T1–T8、I1–I2
  - ⚠️ **那個開關是預設，不是邊界**：`app` 裡的行程可以把環境變數拿掉。拿掉之後 im 讀得到的只有它自己的 transcript（人在 ttyd 打的字，那本來就是直接指令），讀不到別台主機上的
  - **沒量**：這兩個 `ENV` 在 pod 裡是否真的傳到 tmux 裡的 `claude`（`entrypoint.sh` → ttyd → tmux 這條路，讀的，沒量）；ConfigMap 掛上去之後的實際讀數（jg-base 側還沒有）
- [x] 2.5 （2.4 已裁定，正本在本 repo；改在這裡做，Mac 側重新安裝）`chamber` 用戶端認得 socket 形式的位址（`unix:<path>`；design D-b 2026-11-19 的更正）。這是共用檔的修改：Mac 側一起收，同一份測試向量加一格（經 socket 與經 https 的行為逐格相同）
  - **做了（2026-11-19 06:50，`k8scc-0f [7c5b85]`）**：`CHAMBER_URL=unix:<path>` 時用戶端經那個 socket 說 HTTP，**不讀憑證、不送 `Authorization`**（憑證由另一端加）。`test/test_chamber_tools.py` C1–C9：同一台假伺服器同時開 TCP 與 socket，`whoami` 兩邊答案相同；TCP 那邊看得到憑證（正對照）、socket 那邊沒有；socket 不存在 ⇒ 2；拒絕 ⇒ 兩邊都是 1。六個變異各自轉紅（socket 仍讀憑證；socket 帶憑證；開關失效；開關分大小寫；清單目錄被忽略；連不上當成拒絕）
  - **回歸**：Mac 側 bench 的三支對改過的 `directive-verify` 跑（`VERIFY_BIN`）：24／24、13／13、28／28，各有一格 skip（bench 沒開、沒給真的指標）
  - **沒量**：改過的 `chamber` 對真的伺服器（https）沒跑過——Mac 側的 20 格要 bench 伺服器；`send`（POST 帶內文）經 socket 沒有格，只有 GET；真的 bridge 還不存在
- [x] 2.6 用過的指令的那本帳在 PVC 上（預設 `~/.claude/chamber/used_directives`，而 `~/.claude` 是 PVC——讀 `directive-verify` 與 `CLAUDE.md` 得來，沒在 pod 上量）：control 是「`--use` 之後重啟 pod，同一份再驗回 1」
  - **一半（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `aaae5d4`，2026-11-24 06:32–06:39，每格事前寫預期；讀數全文在 k8scc#70 的留言）**：`~/.claude/chamber/used_directives` 的 sha256 在兩次 pod 重啟前後相同。「同一份再驗回 1」要 ferry133 簽的指令，沒量
  - **另一半（`fleet-ops-02 [e3795b]`，jg-jcc2，2026-11-27 00:1x，k8scc#70 issuecomment-7197610644；預期由 `k8scc-bf [97703c]` 事前寫）**：ferry133 2026-11-27 00:12 簽的指令 A（id `0ec6092462ee`，內文「不要求任何動作」），逐位元組複製進 pod（兩側 sha256 相同）。在 `app` 裡 `--use` 回 0，帳本 1 → 2 行，多出的那一行是唯一差異；同一個 pod 不帶 `--use` 再驗回 1（already used）。重啟後的新 pod 回 1，訊息是「already used on this host at 2026-11-26T16:16:58Z」，帳本仍是 2 行。據此勾
- [ ] 2.4 正本住哪（design Open Questions 1）裁定後：另一側改成安裝的副本，加一格雜湊比對
  - **裁定（ferry133 2026-11-19，在 `k8scc-0f` 的 console，逐字）：「正本放 k8scc」。** 正本是本 repo 的 `chamber/`。Mac 側（`~/.claude/scripts/`）改成安裝的副本與雜湊比對那一格由 fleet-ops 收（`fleet-ops-98 [047e9c]` 已接）；那一半做完這一項才勾
  - **Mac 那一半（2026-11-19 06:48，`fleet-ops-98 [047e9c]`，k8scc#70 的留言）**：`~/.claude/scripts/chamber-originals` 比雜湊，六格都與預期相同。它自己寫明「沒有任何東西定期跑它」
  - **2026-11-24 06:21 量到分岔（`k8scc-bf [97703c]`）**：`chamber` 兩側相同（`9a813efaf798e592`）；`directive-verify` 正本 `8b6edaeb638829e6`、Mac 副本 `5270cbe1ff545ed7`。副本比正本新：root R22 的 `--step`（2026-11-22 18:12 寫在 Mac 側，沒有經過本 repo 的 PR）。本 repo 收下 Mac 的位元組並把 `steps-test.py` 的 20 格搬進 `test/test_chamber_tools.py`（ST1–ST18；舊正本跑同一批 20 格全紅；七個變異各自轉紅）。**這一項仍不勾**：分岔兩天沒有任何東西說出來，「雜湊比對」要有東西定期跑才算數，那一半在 fleet-ops

## 3. 橋接程式（root 6.3 的一半）

- [x] 3.1 `chamber-bridge`：讀憑證、Unix socket（不開 TCP；**socket 檔的擁有者與權限要有一格**——兩個 container 的 uid 相不相同沒量，design 沒寫；`fleet-ops-98 [047e9c]` 2026-11-19 確認 `6cb3be3` 時指出）、路徑白名單、sync 位置落檔接著用、`observe`／`deliver`、啟動的三種狀態
  - **做了（2026-11-19 07:15，`k8scc-0f [7c5b85]`）**：`chamber/chamber-bridge`（只用標準函式庫），映像 COPY 到 `/usr/local/bin/`，**`entrypoint.sh` 不起它**。socket 0600。未設定與設定不全都**留著不退出**（與初稿「不啟動」不同：sidecar 退出會讓 pod NotReady，把 ttyd 的路由一起帶下來——`fleet-ops-98 [047e9c]` 起草 jg-base 側時指出，spec 已跟著改）。多做了初稿沒列的四件，都是收緊：不跟隨 redirect（跟了會把憑證送到別的主機）；伺服器確認憑證屬於 `CHAMBER_ACCOUNT` 之前什麼都不轉送；每一個 sync 都帶 `set_presence=offline`（sync 本身會讓帳號顯示在線）；送出的內文上限 1 MiB
  - **狀態行（2026-11-24 起是契約，逐欄貼在 jg-base#248 issuecomment-7130894158；之後只在行尾加欄位）**：`chamber-status ts=<epoch> interval=<秒> mode=observe|deliver|unconfigured server=ok|unreachable|refused|unmeasured rooms=<排序、逗號分隔｜-｜unmeasured> online=yes|no|unmeasured`。`interval=` 是 `jg-base-56 [c30591]` 的輸入。`online` 是向伺服器讀回來的，不是 bridge 自己的說法。**`rooms` 是全有或全無**（k8scc#84，2026-11-20）：有一間的名字沒讀到（請求失敗）整格就是 `unmeasured`，不印房間 id；只有伺服器明說這間沒有名字（404 `M_NOT_FOUND`）時那一間才以 id 列出。第一版把讀取失敗也印成 id，`fleet-ops-98 [047e9c]` 在拋棄式叢集的 pod 裡量到（DNS 間歇失敗），daily-check 會把它讀成「im 在不該在的房間」。同一張單：讀數開始失敗、或失敗的種類改變時，log 多一行 `chamber-bridge: <哪個讀數> (<例外型別｜http=狀態>)`，只有型別名、沒有內文；`URLError/gaierror` 是名稱解析不到，`URLError/ConnectionRefusedError` 是連線被拒。`chamber-verify-status`（`app` 那一行）還沒有，隨 `chamber-watch` 做
  - **`CHAMBER_URL_FILE`（同日加，`fleet-ops-98 [047e9c]` 的請求）**：`CHAMBER_URL` 沒有值時從檔案讀位址（預設 `/etc/chamber/url`，去掉結尾空白）；檔不存在或 0 位元組＝未設定，不是設定不全。原因是 `jg-base-56 [c30591]` 量到的：im 的 chart 不收 env 的 `secretKeyRef.optional`，位址又不能寫進公開的 jg-base，所以只能跟憑證走同一個 optional 的 Secret volume。變數有值時照舊贏過檔案。格 F1–F4 與兩個啟動狀態；兩個變異轉紅
  - **#76 驗收的四件（2026-11-19 07:42，`fleet-ops-98 [047e9c]` 對 bench Synapse 量的）**，都做了：(1) **Secret 晚到**——以空的位址檔啟動後再寫入，16 秒後 socket 仍沒出現；現在閒置時每個 beat 重讀全部輸入，齊了就自己啟動，原因只在改變時印一次。(2) 檔頭、spec、design 的「擋的是憑證被帶走」改字（見 design D-b 的更正）。(3) socket 路徑太長原本是 traceback 後退出，現在是「設定不全」的一行、留著。(4) 不帶目錄的 `CHAMBER_SOCKET`。新增 8 格（共 71），四個變異各自轉紅
  - ~~bench 上量到、還沒解的一格（`online` 自己問與別人看的落差）~~ **撤回**：`fleet-ops-98 [047e9c]` 在 #77 的驗收更正——那是只讀了 log 最後一行造成的；同一瞬間讀三個來源（別人看、im 經 socket 自己問、狀態行），+10 s／+45 s／+90 s 三者一致。留下的讀數只有：`set_presence=offline` 是「不碰」不是「設成離線」；直接 `PUT presence online` 後不續，+45 s 仍在線、+90 s 不在線（三個點，確切的界沒量）
  - **真的伺服器已量（bench，loopback http）**：啟動、`whoami`、`rooms`、收、`deliver` 送、`observe` 拒送、管理路徑 404、沒有 TCP listener、位址從檔案讀、憑證不在 log——十格事前寫的預期全部相符（#76 的驗收留言）
  - **沒做、沒量**：在線維持（4.1；所以 `deliver` 模式下 im 目前也不在線）；sync 位置檔預設放在 socket 的目錄，那是 emptyDir，pod 重啟就沒了，重啟後的第一次同步會拿到舊位置而重算一些訊息——只影響 log 裡的數字，宣告（5.2）做之前要回來處理；憑證輪替要重啟 bridge；**真的伺服器、真的 pod、兩個 container 之間的 socket 都沒跑過**
- [x] 3.2 controls：白名單外的路徑被拒；bridge 沒有任何 TCP listener（正對照：同一份列表看得到 ttyd 的 port）；憑證不出現在 log 與任何回應；`observe` 下 `send` 被拒、在線讀數不變；伺服器連不上時用戶端得到「量不到」不是「不在線」
  - **做了（同上）**：`test/test_chamber_bridge.py` 71 格（含 `CHAMBER_URL_FILE` 的 8 格、驗收四件的 8 格）：bridge 以出貨的樣子當行程跑，對一台手寫的假伺服器，真的 `chamber` 用戶端經 socket 走。16 條白名單外的路徑全是 404 且伺服器一條都沒看到；沒有 TCP listener（`lsof`／`ss`，兩者都沒有 ⇒ 量不到 ⇒ 不算綠；正對照是同一份列表看得到假伺服器的 port）；憑證不在任何回應與 log；`observe` 下 `chamber send` 回 1；伺服器連不上 ⇒ 用戶端回 2（兩格：從沒連上、確認過之後才斷）。十五個變異各自轉紅。**第一輪變異抓到兩個對照的毛病**：「每個 sync 都要求不在線」那格在 bridge 自己還沒 sync 時只讀到測試自己送的那一個（時序，乾淨的 run 剛好都過）；「連不上 ⇒ 2」那格被「帳號還沒確認」的路徑遮住，變異存活——兩格都改了
  - **假伺服器是照用戶端用到的路徑手寫的，不是 Matrix**：這些格量的是 bridge，不是協定
- [ ] 3.3 在沒有 sidecar 的 pod 上由 `entrypoint.sh` 起（只給拋棄式叢集），啟動行說「憑證在 app 內」
  - **2026-11-19 08:53 的狀態（`k8scc-0f [7c5b85]`）：可能不需要了，沒做**。當初寫這一項是因為 jg-base 還沒有 sidecar；jg-base#251 已合併，component 以 `command: ["chamber-bridge"]` 起一個 container。bridge 本身認得 `CHAMBER_BRIDGE_IN_APP=1`（啟動行會說憑證在 app 內，有格），但 `entrypoint.sh` 沒有起它的路徑。第一次走通若直接用 jg-base 的 component，這一項就劃掉；要不要留由第一次走通的人定

## 4. 在線與常駐（root 6.3 的另一半、6.4）

- [x] 4.1 `chamber-watch` 的活著訊號（design D-c）與橋接程式的在線維持
  - **做了（2026-11-19 08:06，`k8scc-0f [7c5b85]`）**：`chamber/chamber-watch`（bash，在 `app` 裡，`entrypoint.sh` 起；`CHAMBER_URL` 空就印一行結束，所以現有的 pod 只多一行 log）。每個 beat：session 存在、pane 的前景行程是 `claude`、而且 pane 是閒置的空 prompt 或正在跑回合，才經 socket 送 `POST /bridge/beat`。bridge 在 `deliver` 下：訊號在 3 個 beat 內 ⇒ 每一輪（上限 20 s，比 bench 量到的 45–90 s 密）對伺服器 `PUT presence online`；訊號停 ⇒ 設 `offline` 一次；bridge 結束時也設 `offline`。`observe` 下訊號收了但什麼都不設。狀態行的 `online` 仍是向伺服器讀回來的
  - `chamber-verify-status ts= interval= signers= revoked= sshkeygen=` 由 `chamber-watch` 每個 beat 印到 `app` 的 log（`signers`／`revoked` 是金鑰數，清單不在就是 `unmeasured`）。**它只說清單讀得到，不說驗過一份簽章**
  - **沒量**：真的 tmux 與 Claude Code（`pane_current_command` 是 `claude` 這一點是 tasks 1.2 在 pod 上讀過一次的）；真的 Synapse 上 20 s 一輪夠不夠密；pod 裡 `app` 的 uid 開不開得了 bridge 建的 0600 socket（兩個 container 都是 uid 0 是 jg-base 側說的，沒量）
- [ ] 4.2 controls（拋棄式叢集）：殺掉 `claude` ⇒ 三個間隔內顯示不在線、FAC 側 `chamber send` 回 3；session 回來 ⇒ 在線（正對照）；卡在登入畫面 ⇒ 不在線；只有橋接程式活著 ⇒ 不在線
  - **(a) 量了（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `aaae5d4`，2026-11-24 06:32–06:39，每格事前寫預期；讀數全文在 k8scc#70 的留言）**：`pkill -x claude` 後 bridge 在 +75 秒記 `online=no`，`fo` 側 +77 秒第一次讀到 offline；FAC 側 `send` 回 3，25 秒後 pod 裡沒有 `claude`。(b)、(d) 先前已有讀數。**(c) 卡在登入畫面沒量**（不登出造不出那個畫面），所以不勾
  - **(c) 的一種量到了，不是原本寫的那一種（2026-11-25～26，`fleet-ops-02 [e3795b]`，jcom，生產）**：session 不是卡在登入畫面，而是登入過期後停在空提示。舊映像（`e6926e6`）上它報 `online=yes`、`send` 回 0，通知被打進去而沒有人處理。PR #143（`pane-gate` 認得 `Not logged in · Run /login` 與 `Login expired · Please run /login`，合併為 `5bc611d`）之後，在 `615cc51` 上讀到（issuecomment-7170188569）：chamber-watch 記 `no alive signal: not logged in`，bridge 轉 `online=no`，`send` 回 3，沒有東西被打進 pane；同一顆映像上已登入的 jg-jiahd、jg-jcc2 是 `online=yes`。狀態列在任何東西被打進去之前就在
  - **開頭的窗口**：那次讀數裡，session 剛起來的第一拍狀態列還沒畫出來，被當成閒置送了一次在線訊號，bridge 因此在線了三拍（約一分鐘）。改法（2026-11-26，`k8scc-bf [97703c]`）：比 `CHAMBER_WATCH_SETTLE`（預設 30 秒）年輕的 session 不判斷、不送訊號，打字本來就有這條。代價是每一座 im 的 session 起來後前 30 秒讀起來不在線。只有那一個讀數；改法沒有在 pod 上跑過
  - **這一項仍不勾**：第一次登入的畫面（沒有輸入框的那一種）沒有量；登入在 session 閒置中途過期沒有量
- [ ] 4.3 `session-keep`（`SESSION_KEEP=1`）：pod 啟動時起、掉了重起、ttyd 接上同一個；起不來時 log 與在線讀數看得見。依 1.2 的讀數
  - **程式與假的對照做了（2026-11-19 08:31，`k8scc-0f [7c5b85]`）；這一項不勾，因為它的實質是 pod 上的讀數**。`session-keep`（`entrypoint.sh` 起，`SESSION_KEEP` 不是 `1` 就印一行結束——預設關，現有的 pod 只多一行 log）。session 不在時以 `CLAUDE_SESSION_DETACHED=1 claude-session` 起：`claude-session` 多一個模式，照常做 MCP 註冊與權限設定，然後 `tmux new-session -d -x 200 -y 50`，沒有歡迎詞、沒有 inbox 的 8 秒停頓；ttyd 之後連進來照舊 `-A` 接上同一個。已存在的 session 不動。起不來只說一次。**它不讀任何訊息**，也不判斷 session 有沒有到 prompt——那由 `chamber-watch` 的訊號與 log 顯示
  - controls：`test/session-keep-controls.sh` 36 格（跑的是真的 `claude-session`）。七個變異各自轉紅；**其中一個第一輪存活**（拿掉 `session-keep` 自己的「已存在就不動」）：「`claude-session` 完全沒被跑」那格在沒有 MCP 要註冊時分不出「沒跑」與「跑了但沒事做」，已改
  - ⚠️ **一次沒解釋的紅**：這份對照第一次跑紅了 10 格（都是要跑 `claude-session` 的那些），接著連跑 10 次全綠，成因沒找到——第一次的暫存目錄已經刪掉。現在紅的 run 會印出各格的 log。若 CI 上再出現，照印出來的查，不要重跑了事
  - **沒量（都要 pod）**：detached 起的 `claude` 到不到得了 prompt（1.2 量到它會停在一次性的對話框）；ttyd 連上之後畫面大小有沒有跟著改；閒置的用量；OAuth 過期時的樣子
  - **pod 上的讀數（2026-11-25，`fleet-ops-02 [e3795b]`，jg-jcc2，全文在 k8scc#70 issuecomment-7158510603）**：啟動時先印 `keeping the session 'claude' running, checked every 30s`，再印 `no session, started one (detached)`；用 Chrome 開頁接上的是同一個 session（created 與 pane pid 不變，attached 0→1，關掉分頁後回 0，session 與 online 都還在）；殺掉 `claude` 後 23 秒有新的 session 並再到空提示（映像 `17ce78a`）。那次留下兩個殭屍行程，是 k8scc#133；PR #136（tini 當 PID 1）合併為 `54da413`，在 `c3070f1` 上同一個動作 3 秒與 48 秒各讀到 0 個 defunct
  - **這一項不勾。沒量**：「起不來時 log 與在線讀數看得見」在 pod 上沒有出現過（每一次都起得來），那一格只有假的對照；生產上沒有開，開不開是 ferry133 的決定
  - **生產上的讀數（2026-11-25 23:1x，`fleet-ops-02 [e3795b]`，映像 `e6926e6`；ferry133 當晚在它的 console 指示兩座生產 im 開 `SESSION_KEEP=1`，jcom `32b52c3`、jg-jiahd `90b1092`；全文在 k8scc#70 issuecomment-7166698330）**：兩座 pod 重建後 session 都自己起來，bridge `online=yes`，`chamber send` 回 0。**jg-jiahd：第一則被生產 im 處理的房間訊息**，23:12:09 送出，im 23:12:50 讀到，23:13:24 在房間回 ack。jcom：通知進了 session，Claude Code 回 `Login expired · Please run /login`，房間裡沒有回覆（見 4.2）。這一項照舊不勾：「起不來時看得見」仍然只有假的對照
- [x] 4.4 不喚起的負對照（root 6.4）：`SESSION_KEEP` 關、沒有 session、送一份驗得過的直接指令 ⇒ 沒有任何行程被起、發送方得到「不在線」。正對照：同一份指令在 session 存在時被宣告
  - **量了（`fleet-ops-02 [e3795b]`，jg-jcc2，2026-11-27 00:1x–00:22，k8scc#70 issuecomment-7197610644）**：
    - 負對照：jg-jcc2 `ad628b6` 關掉 `SESSION_KEEP`，pod 裡沒有 tmux server。從 `fo` 送指令 A 的全文並點名 im：`chamber send` 回 3（offline）；30 秒後沒有任何 `claude` 或 tmux 行程；bridge 的 `pending` 由 {} 變成那間房 1；bridge log `received … for-me=1`。
    - 正對照：`6da1eba` 逐位元組退回，`SESSION_KEEP` 重新開啟。新 pod 的 bridge 記「1 message(s) kept for the session」，之後轉在線、`pending` 歸 {}，訊息確實宣告給了 session。
  - ⚠️ **im 回的是 `blocked`，不是預期的 ack**：2.6 已在同一台主機的帳本上把 A 用掉，所以 im 的 `directive-verify --relayed` 回 1（already used），照政策停下、不動作、在房間裡問 ferry133。這是格子的排序造成的，這份行為本身也是一份讀數（見 7.2「用過的」）。**沒量**：im 對一份還沒用過的指令回 ack。task 本身只要求「被宣告」，所以這一項照勾

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
- [x] 5.3 回程（design D-f）：四個詞、`--reply-to`、並存期間「從哪裡來回哪裡」；`observe` 下不送
  - **2026-11-19 08:53 的狀態：各部分都在了，這一項留到 pod 上走一次才勾**。`--reply-to` 是用戶端原有的；四個詞與「從哪裡來回哪裡」寫在 `im-policy.md`「chamber」一節（6.1，有逐句的對照）；`observe` 下 bridge 拒絕 `send`（3.2 的格，bench 上也量過）、政策寫明那時不依房間裡的訊息動作。**沒有的**是一次真的來回：FAC 送、im 讀、回 `ack`、做、回 `report`
  - **走了一次（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `36c95fd`，2026-11-27 06:46，k8scc#70 issuecomment-7205021516；ferry133 在 `k8scc-bf [97703c]` 的 console 交辦，預期由 k8scc-bf 事前寫）**。
    - 從 `fo` 送一則沒有簽章、唯讀的請求（回報映像 tag 與 `online=`）：06:46:07 `send` 回 0。
    - 06:46:44（37 秒）im 在同一個討論串回 `report`：tag `36c95fd` 與 `online=yes` 都對，也列出它沒查的部分。im 自己的 `chamber read` 把那則標成 `(answered by you: …)`；內文裡沒有那則的 id，所以這個標記來自 `--reply-to`。im 的下一次 `--unanswered` 沒有列出它。
    - 沒有 `blocked` 或 `decline`；帳本仍是 2 行，pod 沒有重啟。「沒有寫入」除了這兩項，靠的是 im 自己說的，沒有讀稽核紀錄。
    - `observe` 下不送：見 7.4。
  - **沒有 `ack`，這是 k8scc-bf 寫預期時錯了，不是 im 錯了**。im 說：「請求是唯讀的，所以先查看，直接回 report。」`im-policy.md`「先回，再做」那一條本來就寫「唯讀的查看不算動手，可以先看再回」，這個請求從頭到尾都是查看，所以不必先回 `ack`。
  - 要動手的請求先回 `ack` 的那條路，這一次沒有走到。生產上有一次讀數：jg-jiahd 2026-11-25 23:13:24 回了 `ack`（見 4.3）。據此勾

## 6. policy 與文件（root 6.6）

- [x] 6.1 `im-policy.md` 新節「誰可以交辦你」「chamber」；`test/im-policy-controls.sh` 加格
  - **k8scc#82 裁定（ferry133，2026-11-21，原話「#82 走 (c)」）**：房間裡請 im 做各節放在它自己判斷之內的動作，那則請求只是資訊——不是授權也不是命令，不需要簽章；im 唯讀查看後依自己的診斷、照那一節的規定決定，支持就 `ack`→做→`report`，不支持就 `decline` 並說明查到什麼。政策原本「裁定之前當保留類」那一條換掉。**還沒裁的一半**：各節寫明不做的事，簽章指令能不能解開（#82 的第三問）——政策寫「不做，回 `blocked`」，#82 為它留著
  - **R21（ferry133，2026-11-21 09:25；root tasks 2.21）**：他的理由延伸到 im 在自己房間裡收到的一切——等同 ttyd console 的輸入，**保留類也不需要簽章**。依據是同機指標 `transcript:b3d8ebaa-8686-4544-a05f-327523b2998c/02e7669f-de4e-4fa7-a207-66bedab25166`（`k8scc-0f [7c5b85]` 自己驗，`directive-verify --relayed` 回 0）：`fleet-ops-98 [047e9c]` 把兩種讀法與第二種的後果（fo 在房間裡一句話就能讓 im 做破壞性操作；握有伺服器管理權或 fo 登入憑證的人也一樣）寫給他，他引第二種說「this one is what i meant.」。「誰可以交辦你」整節重寫；spec 那一條 requirement 與情境跟著改。**沒有裁、政策沒有替他寫的**：(1) 禁止項能不能被房間指令解開（不做、`blocked`）；(2) im 的查看與指令相反時怎麼辦；(3) 驗章設施留不留（留著；附了簽章指令就照驗並回報，0 或 2 不以它為前提，**1 停下來回 `blocked`**——這一格是 `fleet-ops-98 [047e9c]` 審 #90 時要求的，理由是「沒簽章是什麼都沒有，驗章失敗是有證據顯示不對」，仍是暫定）；(4) **「只適用於 im 當收件方、訊息在它自己的房間裡」是 `fleet-ops-98 [047e9c]` 從他的句子讀出來的範圍，不是他的原話**
  - **k8scc#82 最後四格（ferry133，2026-11-24，在 `k8scc-bf [97703c]` 的 console，以選項作答，不是他自己的字）**：(1) 禁止項——選「房間指令可以解開」（選項原文：「公司明確指示時 im 就做，事後照舊寫報告。等於禁止項只擋 im 自己起意」）；(2) 查看與指令相反——選「照做並在回報裡寫明」；(3) 驗章設施——選「留著，暫定做法轉正」；(4) R21 的範圍——選「對，就是這個範圍」。政策多寫的一條是 `k8scc-bf` 的讀法、不是裁定：解開的是動作，保密的幾條（秘密不進房間與報告、配對碼、chamber 憑證）不解開
  - **k8scc#82 修訂（ferry133，2026-11-21 09:15）**：上一條 (c)「只是資訊」被他改掉，原話：「是授權也是命令，不需要簽章: IM 是公司放在客戶端的代理人，IM 所收到的指令都從公司發送，等同於從ttyd console input.」政策那一條改成：房間裡請 im 做各節放在它自己判斷之內的動作 ⇒ `ack`、照那一節去做、`report`。**只改了這一條**：保留類六條仍要簽章的指令，禁止項仍不做；他給的理由比這一條寬，是否延伸到那兩處沒有裁
  - **k8scc#86（2026-11-20）**：政策原本寫「`blocked` 要問 ferry133 時用 `--to all`」，im 在拋棄式叢集上照做了兩次；`fleet-ops-98 [047e9c]` 在 bench 上量到 agent 的 `--to all` 不通知人、點名才通知。改成點名人、送兩則（先問人，再短短告知請求方）。誰是人：`chamber members ROOM` 把帳號名出現在簽章清單上的成員標 `person`——**暫定慣例，不是裁定**（root 沒有定兩個名字相等，fleet-ops-98 會拿去問 ferry133）；清單讀不到或這間沒有清單上的人就退回 `--to all` 並說明。`--to` 維持一個名字或 `all`（多收件人要 ferry133 裁，沒做）
  - **做了（2026-11-19 08:37，`k8scc-0f [7c5b85]`）**：兩節加在檔尾，內容照 `~/coding/CLAUDE.md`「agent 之間的委派」改寫成 im 的版本（類別自己判、一般類直接做、保留類要驗過的指令並讀原文、`--use`、`transcript:` 一律無效、沒簽章的話不是指令、先回再做、秘密不進房間）。對照 188 → 306 格：兩節各一張陳述清單，每一句拿掉都會紅；政策引用的那一行是從 `chamber-watch` 抽出來比的
  - **第一版被退回（2026-11-19 08:44，#81）**，兩個來源各抓到我沒看到的：
    - `fleet-ops-98 [047e9c]` 的驗收：(A) 我寫「人沒有簽章的話不是指令」就停了，漏了 root 規則的另一半（效力與同僚的一般類請求相同），照字面 im 會不理 ferry133 在房間裡沒簽章的一般類請求；(B) 「本政策其他各節的界線照舊」那一句兩半都在裁沒有人裁過的事——**拿掉，開成 k8scc#82 等 ferry133 裁**，政策改成明寫「還沒有裁定，裁定之前照判不出來處理」；(C) `--to all` 也算點名；(D) session 開始先讀一次房間，蓋住 5.2 的缺口；(E) im 當發送方
    - **冷讀（tasks 6.3，一次）**：一個只讀過 `im-policy.md` 的 agent 回答五則訊息。五則的做／不做都與預期一致（一般類做、有指令的刪除做、推導超出原話的不做、沒簽章的破壞性請求不做、重啟請求它讀成「依自己的診斷」）。它列了 23 個缺口，改掉的有：「不加旗標」與「加 `--relayed`」互相矛盾（改成不加 `--use`）；`blocked` 要點名 ferry133 卻沒說怎麼點（`--to all`）；`chamber send` 沒寫內文放哪；通過到底查了什麼、原話從哪裡讀（`--print-body`）；`--use` 之後那一步沒做完怎麼辦；觀察模式下回不了話時該不該動作；「職責」沒定義；房間來的重啟要不要照寫 inbox 報告。**沒改的**：保留類第 1、3、4 條對 im 幾乎用不到（照 root 的六條原樣留著）；inbox 檔來的保留類請求沒有同樣的檢驗（不在這一節的範圍，也沒有人裁過）
    - **第二輪冷讀（2026-11-19 08:49，改完的版本，七則訊息）**：七則的做與不做都與預期一致，包括新加的三格——沒簽章的人的一般類請求照做、`--to all` 算點名、點名別人的訊息不動；房間來的重啟請求這次讀成「唯讀查看後回 `blocked`」（第一輪讀成依自己的診斷去做）。它再列的缺口裡改了三處小的（「職責」含查看與診斷；先看再回的順序；一則請求部分能做時第一個字用哪一個），其餘都是 k8scc#82 那一題的內容，已貼到那張單
    - **冷讀的限制**：只跑一次、一個模型、五則訊息；它回答的是「讀得懂嗎」，不是「im 真的會這樣做」
  - **多寫的一句**（初稿沒列）：chamber 的登入憑證不是 im 的，不要去讀，**包括用 `kubectl` 讀那個 Secret**——sidecar 擋不住 cluster-admin（design D-b 的更正），所以這一格只能靠政策
- [x] 6.2 `CLAUDE.md`：新節與 Runtime Configuration 表的 env；`operator-inbox` 的 delta 對應的文字
  - **做了（隨 #74–#81 各自的 PR）**：「Chamber tools」一節（正本、bridge、`chamber-watch`、`session-keep`、政策、映像預設）與 Runtime Configuration 表的 `CHAMBER_*`、`SESSION_KEEP*`；inbox 那一節補了 `pane-gate`。`operator-inbox` 的 delta 是 #72 起草時就有的，沒有再動
- [x] 6.3 （兩輪，見 6.1）冷讀：一個沒讀過 root 的 session 只憑 `im-policy.md`，回答四則訊息（一般／保留有指令／保留而推導超出／人沒簽章的破壞性請求）做還是不做

## 7. 整體 controls（root 6.7；拋棄式叢集）

- [x] 7.1 讀寫別的房間、邀人、開房間（伺服器拒絕——這三格量的是伺服器，在這裡只確認用戶端回 1 而不是 2）
  - **量了（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `aaae5d4`，2026-11-24 06:32–06:39，每格事前寫預期；讀數全文在 k8scc#70 的留言）**：經 pod 的 bridge，`chamber read／members／send fac` 都回 1 不是 2；以房間 id 直接打 bridge 是 403；開房間、邀人、以別名加入都是 404 `CHAMBER_NOT_FORWARDED`；正對照是自己的房間回 0
- [x] 7.2 偽造簽章、清單外的金鑰、過期指令、用過的指令、`transcript:` 指標：`directive-verify` 各回 1／1／1／1／2，im 都不動作
  - **過期與用過的（`fleet-ops-02 [e3795b]`，jg-jcc2，2026-11-27，k8scc#70 issuecomment-7198925832 與 -7197610644）**：
    - 過期：ferry133 00:19 簽的指令 B2（id `50c8da05fd73`，`--expires 1h`）在 pod 裡 16:24:11Z 回 0，這是正對照；17:19:53Z 回 1，訊息「expired at 2026-11-26T17:19:22Z」。送進房間（`send` 回 0，在線）之後，im 42 秒內回 `blocked`，理由寫的是過期，沒有任何動作，並在房間問 ferry133。帳本維持 2 行。
    - 用過的：在 pod 裡由 im 自己驗，結果見 4.4：回 1、`blocked`、不動作。
  - 五格到此都有讀數：偽造（T3）、清單外的金鑰（pod）、過期（pod）、用過的（Mac S4 與 pod）、`transcript:`（pod，2）。據此勾
  - ⚠️ **「im 都不動作」只在過期與用過的兩格讀過 im 本身**（`fleet-ops-02 [e3795b]` 驗收 #149 時指出）。偽造、清單外的金鑰只量了 `directive-verify` 的回傳值 1，那時 im 會怎麼做是從政策讀來的（回 1 就停下、回 `blocked`，與過期、用過的走同一條規則），沒有在 im 身上讀過。`transcript:` 回 2，依 R21 與 2026-11-24 的裁定，0 與 2 都不改變結果，所以那一格 im 照指令行事，「不動作」在那裡本來就不成立。這一項照勾，因為驗章那一半五格都量了，而回 1 時 im 的行為已在兩格讀到
  - **兩格量了（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `aaae5d4`，2026-11-24 06:32–06:39，每格事前寫預期；讀數全文在 k8scc#70 的留言）**：清單外的金鑰在 pod 裡回 1（同一份位元組在清單含那把金鑰的 Mac 上回 0）；`transcript:` 指標回 2。偽造簽章（T3）與用過的（S4）先前已有。**過期那一格沒量**（要 ferry133 簽）。R21 之後「im 都不動作」只在結果是 1 時成立
- [x] 7.3 一般訊息與直接指令都不喚起（4.4）
  - 直接指令：見 4.4，`send` 回 3，沒有任何行程被起。一般訊息：2026-11-24 那次 `pkill` 之後，從 FAC 側送的 `send` 回 3、pod 裡沒有 `claude`（4.2 (a)），讀數是同一個形狀
- [x] 7.4 `observe` → `deliver` 的切換與退回：退回後行為與切換前逐格相同
  - **量了（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `aaae5d4`，2026-11-24 06:32–06:39，每格事前寫預期；讀數全文在 k8scc#70 的留言）**：deliver → observe → deliver，五個容器的映像摘要三次都相同；observe 時 pod 裡 `send` 回 1（403 `CHAMBER_OBSERVE`）、收得到；退回後 `send` 回 3，與切換前相同。**切換時 pod 裡沒有 session**，有 session 時的切換沒量；所以不勾（fleet-ops-02 驗收 #134 時要求：兩邊都沒有 session，「相同」比的是兩個 `online=no`）
  - **有 session 的那一格量了（`fleet-ops-02 [e3795b]`，jg-jcc2，映像 `550287e`，`SESSION_KEEP=1`，2026-11-25 17:17–17:24；jg-jcc2 `b6defdd` 切 observe、`1d9565d` 切回；全文在 k8scc#70 issuecomment-7158510603，更正在 -7158518736）**：deliver → observe → deliver，狀態行 `online=yes`／`no`／`yes`；`fo` 送給 im 的 `send` 回 0／3／0；bridge 收到 +1／+1／+1、宣告給 session +1／+0／+1；pod 裡 `send` 回 3／1（403 `CHAMBER_OBSERVE`）／3；映像摘要相同。退回後五格與切換前逐格相同，據此勾（fleet-ops-02：「7.4 我認為可以勾了」）。**沒量**：切換當下 im 正在處理一則交辦；每次切換 pod 都重建，session 是 session-keep 新起的，不是同一個 session 跨過切換；生產上的切換只有 observe → deliver 那一個方向（11-25，jcom、jg-jiahd），而且那兩座沒有 session

## 8. 收尾

- [x] 8.1 契約（spec `im-chamber-member`）定稿時貼到 `jg-base#248`——2026-11-24 貼了兩行狀態列的格式（issuecomment-7130894158，k8scc-bf [97703c]，回 jg-base-d4 [a9cc43] 的詢問）。貼的是 daily-check 要讀的那一部分，不是整份 spec；`refused`／`unreachable`／`rooms=-` 仍只對假伺服器跑過
- [ ] 8.2 歸檔時回頭更新 root 的 Sub-changes 表與 tasks 第 6 組
