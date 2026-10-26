> 全部在 `k8scc`。**驗收由 root 擁有者——fleet-ops 目錄的 session——在 merge 之前收**；作者（k8scc-c7 [9b9eab]）也是 root 的開單者，**不自收**。
> 歸檔時由作者回頭更新 root（`fleet-ops/openspec/changes/im-unifi-management` 的 Sub-changes 表與 tasks §2）；**契約（spec `netdev-mcp-server`）定稿時貼到 jg-base#222**。
> 測試照 `test/test_talos_mcp_server.py` 的形狀：假物記錄每個請求、每格有負對照；三種結果（通過／失敗／量不到）分開寫。
> **沒有真 controller 的格一律標「量不到」**——root 6.x 在 FAC1 真 UniFi 上量，不在這裡。

## 1. Spikes（結果會改 design）

- [x] 1.1 `httpx` 在 `mcp` 釘版的依賴樹裡嗎（映像內 `python3 -c 'import httpx; print(httpx.__version__)'`）；不在 ⇒ 照 `mcp` 的釘法加進 base 層，design D-a 改。**讀數（2026-10-26）**：`pip show mcp`（1.29.0）的 `Requires` 有 `httpx`，裝出來是 0.28.1——依賴宣告與平台無關；在 darwin 的 venv 量，不是映像內，映像內那一句留給 image build 後。D-a 不改。
- [ ] 1.2 消費 root spike 1.1 的讀數（FAC1 UniFi 的 API 面：哪些 collection 走 Integration、哪些只有 legacy；PUT 整物件或 PATCH；登入流程的 header／cookie 名；`set_dns_forward` 有沒有 API）→ 填 `ROUTES` 表與 fixture；錄一份去值的 legacy 登入 request／response 當 fixture 依據。**root 1.1 沒量之前，這裡只能寫 Integration 半邊與假 controller。**
- [ ] 1.3 消費 root spike 1.5（fine-grained token 的最小 scope）→ design D-g 寫明 scope；本地用假 FAC 先做。

## 2. server

- [x] 2.1 `netdev_mcp_server.py` 骨架：設定讀取、三種啟動狀態、`_startup_readings`、TLS 規則（D-b）、`Controller` 與兩個 backend、`ROUTES`。（reader-only 版：operator／通道不讀，啟動狀態兩種；`ROUTES` 值是暫定，等 1.2。）
- [x] 2.2 reader 10 支＋`_strip`（D-c）＋ session 快取（D-d）＋路徑標記。
- [x] 2.3 class A 的四個純函式（D-e）＋ fixture（group／位址／子網三種寫法、正負對照）。
- [x] 2.4 operator 工具（proposal 的清單；`set_dns_forward` 依 1.2 決定出不出）：型別化參數、伺服器組 body、reason／快取／token／冷卻／計數五道。（八支；`set_dns_forward` 不出——1.2 沒讀數。body 從快照的未剝物件組，不是從已剝的快取，否則秘密會被寫成 `<stripped>`。`WRITES`／`LOGIN_PATH` 與 `ROUTES` 一樣是暫定值。）
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

- [ ] 5.1 spec `netdev-mcp-server` 定稿 ⇒ 貼到 jg-base#222（env 名、port、Secret 掛載路徑、快照目錄、daily-check 列要讀的 stderr 行），jg-base-ed [d73263] 據此開 `netdev-mcp-sidecar`。
- [ ] 5.2 `im-permission-profile` 的 MODIFIED 在歸檔時合進 `openspec/specs/`（寫在 `im-ha-mcp` 的 delta 之上；順序相反就重疊一次）；`netdev-mcp-server` 成為新 spec。
- [ ] 5.3 活 controller 的格（root §6，FAC1 真 UniFi、拋棄式）——本 sub-change 不等它歸檔，但 PR 內文要寫「真 controller 未量」與哪幾格是假物量的。
- [ ] 5.4 PR 內文署名；驗收者（fleet-ops 目錄的 session）在 merge 之前收；merge 後 PR 留一行 `peer name [ref]`。先出 reader-only 的映像（root Migration 2），operator 面第二張 PR。
- [ ] 5.5 歸檔本 sub-change，回更新 root 的 Sub-changes 表與 tasks §2。
