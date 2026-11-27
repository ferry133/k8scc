> 全部在 `k8scc`。**驗收由 root 擁有者——fleet-ops 目錄的 session——在 merge 之前收**；作者（k8scc-c7 [9b9eab]）也是 root 的開單者，**不自收**。
> 歸檔時由作者回頭更新 root（`fleet-ops/openspec/changes/im-unifi-management` 的 Sub-changes 表與 tasks §2）；**契約（spec `netdev-mcp-server`）定稿時貼到 jg-base#222**。
> 測試照 `test/test_talos_mcp_server.py` 的形狀：假物記錄每個請求、每格有負對照；三種結果（通過／失敗／量不到）分開寫。
> **沒有真 controller 的格一律標「量不到」**——root 6.x 在 FAC1 真 UniFi 上量，不在這裡。

## 1. Spikes（結果會改 design）

- [x] 1.1 `httpx` 在 `mcp` 釘版的依賴樹裡嗎（映像內 `python3 -c 'import httpx; print(httpx.__version__)'`）；不在 ⇒ 照 `mcp` 的釘法加進 base 層，design D-a 改。**讀數（2026-10-26）**：`pip show mcp`（1.29.0）的 `Requires` 有 `httpx`，裝出來是 0.28.1——依賴宣告與平台無關；在 darwin 的 venv 量，不是映像內，映像內那一句留給 image build 後。D-a 不改。
- [x] 1.2 消費 root spike 1.1 的讀數（FAC1 UniFi 的 API 面：哪些 collection 走 Integration、哪些只有 legacy；PUT 整物件或 PATCH；登入流程的 header／cookie 名；`set_dns_forward` 有沒有 API）→ 填 `ROUTES` 表與 fixture；錄一份去值的 legacy 登入 request／response 當 fixture 依據。**root 1.1 沒量之前，這裡只能寫 Integration 半邊與假 controller。**
  - **消費了（2026-11-27 對帳，`k8scc-bf [97703c]`）**：root 1.1 的讀面（fleet-ops-b0 [5a4f4b]，2026-11-12，UCG Ultra／Network 11.0.99）與寫面的已量部分（`rest/user` 合併式 PUT、`DELETE rest/user` 404、View Only cookie 寫入 403、v2 `static-dns` 的條件式轉發）經 k8scc#67（finding 1–8，#68／#69 → `e8d7522`）與 k8scc#109 進了 `ROUTES`／`WRITES`、fixture 與 `set_dns_forward`／`clear_dns_forward`。**沒有 Integration API 的路由**：它對 View Only cookie 回 401。
  - **仍沒量，所以 `ROUTES`／`WRITES` 的這幾項還是暫定**：`networkconf`／`portforward`／`wlanconf`／防火牆的寫入；`self/sites`、v2 `firewall/zone` 的讀取。**`stat/device` 與 `stat/sta` 在 FAC1 上讀過**：前者在 spike 1.1 以 legacy 讀到 200（root design D4）；後者是 sidecar 的 `get_clients`，root 6.2–6.5 讀過很多次，11-25 有一次回 500，sidecar 據此拒絕了所有寫入（root 6.5 (d)）。初稿把這兩項寫成沒量，是 fleet-ops-1e [83ff46] 驗收 #152 時指出的root 1.1 因此也還沒勾。「錄一份去值的登入 fixture」沒有做，fixture 是照文件上的欄位名手寫的（CLAUDE.md 有記）
- [x] 1.3 消費 root spike 1.5（fine-grained token 的最小 scope）→ design D-g 寫明 scope；本地用假 FAC 先做。
  - **消費了（2026-11-27 對帳，`k8scc-bf [97703c]`）**：root 1.5 由 fleet-ops-1e [83ff46] 在 2026-11-24 量到（root design D9），結論是 (a) 可行。D-g 已寫進 scope；CLAUDE.md 原本那句「not yet measured」也改了

## 2. server

- [x] 2.1 `netdev_mcp_server.py` 骨架：設定讀取、三種啟動狀態、`_startup_readings`、TLS 規則（D-b）、`Controller` 與兩個 backend、`ROUTES`。（reader-only 版：operator／通道不讀，啟動狀態兩種；`ROUTES` 值是暫定，等 1.2。）
- [x] 2.2 reader 10 支＋`_strip`（D-c）＋ session 快取（D-d）＋路徑標記。
- [x] 2.3 class A 的四個純函式（D-e）＋ fixture（group／位址／子網三種寫法、正負對照）。
- [x] 2.4 operator 工具（proposal 的清單；`set_dns_forward` 依 1.2 決定出不出）：型別化參數、伺服器組 body、reason／快取／token／冷卻／計數五道。（八支；`set_dns_forward` 不出——1.2 沒讀數。**k8scc#109（2026-11-22）：root 1.1 的寫入讀數（#67 finding 7／8）到了，加上 `get_dns_records` 與 `set_dns_forward`／`clear_dns_forward`，共十支；只對假物量過。**body 從快照的未剝物件組，不是從已剝的快取，否則秘密會被寫成 `<stripped>`。`WRITES`／`LOGIN_PATH` 與 `ROUTES` 一樣是暫定值。）
- [x] 2.5 向內 dead-man：探針（D-f）、票與 ack（D-g）、快照、健檢、回滾、release 三值、`revert-failed` 的 inbox 指令。
- [x] 2.6 稽核行（spec 的欄位）＋ stdout 除此之外不印任何東西。**讀數（2026-10-26）**：「talos 已這樣做」不成立——uvicorn 的 access log 是 `ext://sys.stdout` 上的 StreamHandler，照 FastMCP 原樣跑，兩個 POST 在 stdout 留兩行；`__main__` 把 `sys.stdout` 指向 stderr、真的 stdout 只給 `_audit`，之後 stdout 0 行。talos-mcp 沒有這一步，另開 issue。

## 3. 測試

- [x] 3.1 假 controller、假 FAC、三個探針 listener、`kubectl` shim（D-i）；fixture 目錄含帶 `x_passphrase` 的 WLAN、帶 group 的 firewall rule、gateway／AP／交換器各一。（reader-only 版做了：假 controller、fixture；假 FAC、探針、kubectl shim 屬寫入那張。）
- [x] 3.2 spec 每一個 scenario 一格，各帶正／負對照；`will_change.after` 與假 controller 收到的 body 逐欄位比。
- [x] 3.3 四個刻意變異（不拍快照、token 不消耗、class A 網路放行、探針全掉不回滾）各讓對應測試紅一次，寫進 PR。（2026-10-26：11／1／2／6 紅；另加兩個——任何留言都算見證 1 紅、create 的回滾刪掉所有新 id 1 紅。spec 的「Own network refused」「Firewall group expansion」兩格沒有寫入工具會打到 network／firewall 物件，由 class A 純函式的格蓋。）
- [x] 3.4 `test/claude-session-controls.sh`：`NETDEV_MCP_URL` 未設／空 ⇒ 靜默；設 ⇒ `netdev` 註冊、`allow` 有 `mcp__netdev`；第二次零 diff。`test/im-policy-controls.sh`：新節各句 `check`、「直接 curl controller」「略過 will_change」`check_not`。（reader-only 版做了：claude-session 的格，與 policy 新節各句＋「直接 curl controller」；「略過 will_change」等寫入規則進 policy 時加。）

## 4. claude-session、policy、文件

- [x] 4.1 `claude-session`：`NETDEV_MCP_URL` 門（照 talos 那段，空＝未設）。
- [x] 4.2 `im-policy.md`「你的路由器」一節（內容見 proposal）。（reader-only 版寫了讀與清冊的部分；寫入規則等寫入工具。）
- [x] 4.3 `CLAUDE.md`：新節「Netdev MCP Sidecar」（三種啟動狀態、七道閘門、向內 dead-man、class A、票、稽核、D-b 的 TLS 規則、D-h 的記憶體冷卻）、Runtime Configuration 表全部 env。（reader-only 版寫了讀的那半與它的 env；閘門、dead-man、票、稽核等寫入那張。）
- [x] 4.4 `Dockerfile`：COPY 新 server（＋1.1 決定的 `httpx` 釘版，若需要）。

## 5. 契約與收尾

- [x] 5.1 spec `netdev-mcp-server` 定稿 ⇒ 貼到 jg-base#222（env 名、port、Secret 掛載路徑、快照目錄、daily-check 列要讀的 stderr 行），jg-base-ed [d73263] 據此開 `netdev-mcp-sidecar`。
  - **做了（2026-11-27 對帳，`k8scc-bf [97703c]`）**：k8scc-c7 [9b9eab] 2026-10-26 在 jg-base#222 貼了契約（`5bf8f25`，jg-base-ed 回「收到契約」），20:29 又貼了寫入映像的 addendum（#45）。jg-base#222 已關閉，驗收是 k8scc-bf 在 2026-11-24 代 k8scc-c7 收的
- [ ] 5.2 `im-permission-profile` 的 MODIFIED 在歸檔時合進 `openspec/specs/`（寫在 `im-ha-mcp` 的 delta 之上；順序相反就重疊一次）；`netdev-mcp-server` 成為新 spec。
- [x] 5.3 活 controller 的格（root §6，FAC1 真 UniFi、拋棄式）——本 sub-change 不等它歸檔，但 PR 內文要寫「真 controller 未量」與哪幾格是假物量的。
  - **對帳（2026-11-27 對帳，`k8scc-bf [97703c]`）**：root §6 的 6.1–6.7 都已勾，在 FAC1 真 UniFi 加拋棄式叢集 jg-jcc2 上量，各格由誰量寫在 root tasks §6 那一格。6.8（清場）還開著，那是 fleet-ops 的。**仍沒在真 controller 上量的**：
    - DNS 轉發的兩支工具。要等 root 7.2 在 jcom 上量
    - port forward 的 DELETE 回滾
    - 跨 sidecar 重啟的 `interrupted`
    - 同時送出第二個寫入會被拒（#132）
    - undo 的 baseline 豁免，#142 (a)。ferry133 裁定走 (b)
    - im 自己（Claude）呼叫寫入工具。6.3、6.5、6.6 的呼叫端都是 fleet-ops-1e 寫的 client
    - `restart_device`／`power_cycle_port` 真的執行。FAC1 上只量過被 class A 拒絕的路：6.5 (f)，以及 #73 設備類型那一半
  - **在 FAC1 上讀過、初稿寫成只對假物量的**（fleet-ops-1e [83ff46] 驗收 #152 時指出，issuecomment-7222158916）：
    - #132：等 token 的 124 s 裡，另一條 session 讀了 9 次 `get_clients`，都有回應（2026-11-26，映像 `615cc51`，票 #17／#18）
    - #132：set 與 clear 共用 cooldown，undo 例外拿到 token 並 `applied`（票 #12／#13）
    - #142 (b)：token 綁的是變更，同一場那 9 次讀取都沒有讓它作廢。改之前的對照是票 #14，那一次 token 被作廢
- [x] 5.4 PR 內文署名；驗收者（fleet-ops 目錄的 session）在 merge 之前收；merge 後 PR 留一行 `peer name [ref]`。先出 reader-only 的映像（root Migration 2），operator 面第二張 PR。
  - **做了（2026-11-27 對帳，`k8scc-bf [97703c]`）**：reader-only 是 k8scc#43（2026-10-26 合併，fleet-ops-65 驗收），operator 面是 #45（同日合併）。之後的修正各有自己的 issue，由開單者驗收（清單在 CLAUDE.md 的「Netdev MCP Sidecar」一節，這裡不複述）
- [ ] 5.5 歸檔本 sub-change，回更新 root 的 Sub-changes 表與 tasks §2。
