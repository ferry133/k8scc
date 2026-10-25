# design — im-netdev-mcp

Root 的設計在 `fleet-ops/openspec/changes/im-unifi-management/design.md`（D1 形狀、D2 七道閘門、D3 向內 dead-man 與 class A、D4 API 選擇、D5 快照、D6 稽核、D7 清冊、D9 通道）。這裡只記 server 自己要決定的事。

## Decisions

### D-a ─ 形狀照 `talos_mcp_server.py`，但不 shell out

talos 的每一支工具都 `_exec(["talosctl", …])`；UniFi 沒有 CLI，所以這個 server 自己講 HTTP。沿用的：`FastMCP` 綁 loopback、`_respond`、`_audit` JSON 行、兩階段 token 與 `_prune_tokens`、`_startup_readings`、fail-closed 的措辭。換掉的：`_exec` → `Controller`（一個 class，兩種 backend：`IntegrationAPI`（`X-API-Key`）與 `LegacyAPI`（`POST /api/auth/login` 取 cookie＋CSRF、`PUT /proxy/network/api/s/<site>/rest/<collection>/<_id>`）；哪些 collection 走哪個 backend 由 root spike 1.1 的讀數填進一張表 `ROUTES`，表是資料不是程式碼）。HTTP 客戶端用 `httpx`——`mcp` 的釘版已經把它帶進映像（spike 1.1 確認；沒有就加釘版，不用 `requests`）。

### D-b ─ TLS 一律驗；console 的自簽憑證以 `UNIFI_CA_FILE` 釘

UniFi console 預設自簽。`verify=False` 是一行就能寫出來、而且永遠「能用」的東西——所以不提供這個選項。`unifi-im-credential` skill 在 onboarding 匯出 console 的憑證（或其 CA）進 Secret，server 以 `httpx.Client(verify=UNIFI_CA_FILE)` 連；檔缺或不符 ⇒ 連線失敗 ⇒ reader 讀數是 `not configured`／錯誤，寫入工具不註冊。`http://` 只在 `NETDEV_ALLOW_INSECURE_HTTP=1` 時接受（測試的假 controller 用），啟動行印 `insecure http accepted (test only)`，controls 斷言沒有這個 env 時 `http://` 被拒。憑證輪替（console 換憑證）⇒ Secret 更新 ⇒ sidecar 重啟；daily-check 列會先看到 reader 失聯。

### D-c ─ 剝秘密是一個函式，套在三個出口

`_strip(obj)`：遞迴，鍵名符合 `^x_`、`_secret$`、`_key$`、`passphrase`、`password`、`psk`（不分大小寫）的值換成 `"<stripped>"`。套在 (1) 每一支 reader 工具的回應、(2) `will_change` 的 before／after、(3) 票的內文。**快照不剝**（它是回滾的原料，進 sidecar 專屬 volume）。controls 用含 `x_passphrase` 的 WLAN fixture 對三個出口各斷言 0 命中，正對照是快照檔裡 1 命中。

### D-d ─ 寫入的物件來自 session 快取，`_id` 不接受模型自報

`_seen[collection][_id] = (obj, ts)`，由 reader 工具填、TTL 600 s。operator 工具以 MAC／`_id`／name 查快取，查不到就拒「先讀再寫」；body 由伺服器從快取的物件複製再改**指定欄位**（例如 `set_dhcp_reservation` 只動 `use_fixedip`／`fixed_ip`），`will_change` 是這兩份的 diff。模型永遠沒有一個「給我整份 JSON」的入口。

### D-e ─ class A 的判定是四個純函式，各自可測

`own_networks(pod_ip, controller_ip, api_server_ips, networks)`、`attachment(pod_ip, clients, devices)`（節點連到的交換器 port／AP）、`fw_rules_covering(addr_pairs, rules, groups)`（group 展開、CIDR 包含）、`class_a(target_collection, target_id, …)` 回 `None` 或「相交的是哪一段」。輸入全是 reader 已剝過的 dict，沒有 I/O，所以 controls 可以用純 fixture 蓋 group／位址／子網三種寫法與負對照。任一輸入讀不到 ⇒ `class_a` 回「讀不到 ⇒ 拒」。

### D-f ─ 探針與見證是兩個獨立讀數，合成三值

探針：`NETDEV_OUTSIDE_PROBES` 三條，各是 `https://host/path`（GET，2xx／3xx 算通）或 `host:port`（TCP connect）。基線（W1）要三條全通；健檢每 10 s 重探，`lost` ≥2 ⇒ 回滾，＝1 ⇒ `degraded`。見證：票的留言流裡找固定前綴 `pre-ack:`／`post-ack:`（任何 GitHub 身分留的都算——FAC 共用 ferry133 的身分，署名在留言內文）；pre-ack 在 `NETDEV_WITNESS_WAIT` 內沒來 ⇒ `unmeasured`、不擋；armed 而健檢窗內 post-ack 沒來且探針全通 ⇒ 回滾、`lost`。兩個讀數都寫進 release 與稽核行。**通道未設定（`IM_FAC_TICKET_REPO` 空或 token 缺）⇒ operator 工具不註冊**——root W1 要求票先出 LAN，而且 stdout 之外這是直接 LAN 寫入唯一的公司側紀錄。

### D-g ─ 票是 GitHub issue，用最小 scope 的 fine-grained token，只在 sidecar

`IM_FAC_TICKET_REPO`（`owner/repo`，private）＋ `IM_FAC_TICKET_TOKEN_FILE`。scope：issues read／write 於那一個 repo（root spike 1.5 量最小集合）。server 直接 `httpx` 打 REST（`POST /repos/{r}/issues`、`GET …/comments`、`PATCH …` close），不用 `gh`（base 映像沒有，不加）。票的標題 `[netdev] <cluster> <tool> <target>`；內文：叢集、collection＋`_id`、剝過的 diff、截止時間、票號；release 是關單留言，帶三種讀數。GitHub 同時是三條探針之一（D-f），所以「開不了票」與「探針掉」在基線時是同一個讀數。

### D-h ─ 冷卻與計數在記憶體；own-node 那種洞這裡沒有

talos 的 own-node cooldown 要靠 `/proc/uptime`，因為重開自己會抹掉記憶。這裡的寫入不會重啟 sidecar（class A 擋掉會切斷自己的那類），所以每物件 600 s 冷卻與每小時計數放記憶體即可；sidecar 重啟歸零，稽核 stdout 仍看得到兩行靠近——寫進 CLAUDE.md，與 talos 同一句。

### D-i ─ 測試的三個假物與一個 shim

`test/test_netdev_mcp_server.py`：(1) 假 controller——`http.server` 子執行緒，依 `test/netdev-mcp-fixtures/` 回 JSON、記錄每個請求、可被叫「從現在開始不回應」（健檢必敗）；(2) 假 FAC——同形的假 GitHub issues API，可預設「永不 ack」「pre-ack 不 post-ack」「兩個都 ack」；(3) 探針——三個本機 listener，可逐一關掉；(4) `kubectl` shim（`test/netdev-mcp-shims/`）回 `/readyz` 與 endpoints。server 以 `NETDEV_ALLOW_INSECURE_HTTP=1` 連假 controller。每格先跑正對照再跑負對照；四個變異以 monkeypatch 關掉對應程式碼、斷言對應測試變紅。

## Risks

- [Integration API 與舊版 API 的欄位在 UniFi 小版間變] → `ROUTES` 與 fixture 都標版本；daily-check 的版本列（jg-base#222）讓變動被看見；`set_*` 的 body 只動指定欄位、其餘照 GET 回寫，所以新欄位不會被洗掉。
- [`httpx` 不在 `mcp` 的依賴樹] → spike 1.1 第一格；不在就釘版加進 base 層（照 `mcp` 的釘法）。
- [假 controller 與真 controller 的登入流程不同（CSRF header 名、cookie 名）] → spike 1.1 在 FAC1 真 console 上錄一次 legacy 登入的 request／response（去掉值）當 fixture 的依據；controls 標「照 X 版錄的」。
- [探針選到會被 Cloudflare 或 ISP 擋的端點] → root Open Question（三條走不同路）；server 只要求「三條、可設定」，選哪三條是 jgct／fleet-ops 的值。
- [`set_dns_forward` 沒有 API] → spike 門控，不出這支；root 7.2 的第一筆真實寫入改用其他工具。
