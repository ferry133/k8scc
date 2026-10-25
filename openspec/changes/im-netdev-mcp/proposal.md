Part of: `fleet-ops` / `im-unifi-management`

**Owning repo**：`k8scc` —— 本 sub-change 改的檔案（新的 `netdev_mcp_server.py`、`Dockerfile` 的 COPY、`claude-session`、`im-policy.md`、`CLAUDE.md`、`test/`）全在這裡；root change 住在 `fleet-ops`，持有跨 repo 的契約（兩把憑證、自主模型、向內 dead-man、class A、清冊存放）。jg-base 的 sub-change `netdev-mcp-sidecar`（jg-base#222）**等本 sub-change 的 server 契約**——契約就是本 sub-change 的 spec `netdev-mcp-server`，定稿後貼到 #222。這個 repo 持有 `im-permission-profile`，本 sub-change 對它出 delta（寫在 `im-ha-mcp` 的 delta 之上；若歸檔順序相反，歸檔時重疊一次）。

## Why

Root 的 Why 不在這裡複述（`fleet-ops/openspec/changes/im-unifi-management/proposal.md`；三次裁定：選項 3、「FAC and IM」、「FAC 沉默、IM 從裡面回滾」）。k8scc 的份額是**那個 server**：im 透過它讀 UniFi、寫 UniFi，而每一道門、每一份快照、每一次回滾、每一行稽核都在它裡面。量到的現況（2026-10-26 01:02，`origin/main 59a5d24`）：`talos_mcp_server.py` 557 行是現成的形狀——`FastMCP` 綁 `127.0.0.1`、`_exec` 以 list 形式 shell out、兩階段 token 與 `_prune_tokens`、`_audit` JSON 行、`_startup_readings`；`test/test_talos_mcp_server.py` 614 行以 shim 記錄 argv、每格有負對照。im 對路由器今天的能力是零：沒有憑證、沒有寫入路，只有 `nmap`／`arp-scan` 那類唯讀盤點。

## What Changes

- **`netdev_mcp_server.py`**（新，COPY 進 base 映像；`FastMCP("netdev", host="127.0.0.1", port=8766)`，`command` 覆寫啟動，不跑 ttyd）：
  - **設定全部來自 env 與掛載檔**（契約見 spec「Configuration」）：`UNIFI_CONTROLLER_URL`、`UNIFI_SITE`（預設 `default`）、`UNIFI_CA_FILE`（console 的自簽憑證，**TLS 一律驗、不驗就不連**；`http://` 只在 `NETDEV_ALLOW_INSECURE_HTTP=1` 時接受，給測試用，啟動行印出）、`UNIFI_READER_KEY_FILE`（Integration API key）、`UNIFI_OPERATOR_FILE`（本地帳號，兩行：user、password）、`POD_IP`、`NETDEV_SNAPSHOT_DIR`／`NETDEV_SNAPSHOT_KEEP`、`NETDEV_HEALTH_TIMEOUT`（預設 120）、`NETDEV_WRITES_PER_HOUR`（12）、`NETDEV_OUTSIDE_PROBES`（三條，逗號分隔）、`NETDEV_WITNESS_WAIT`（120）、`IM_FAC_TICKET_REPO`／`IM_FAC_TICKET_TOKEN_FILE`、`NETDEV_READ_TIMEOUT`（15）、`CLAUDE_USER_ID`。
  - **三種啟動狀態**：reader 缺 ⇒ 每支工具回 `not configured`；reader 在、operator 或通道缺 ⇒ **reader-only**（寫入工具不註冊，啟動行說缺哪個）；三者都在且 operator 登得進 ⇒ 全部註冊。
  - **reader 工具 10 支**（唯讀、剝秘密、回應開頭標 `[via integration]`／`[via legacy]`）：`get_sites`、`get_controller_info`、`get_devices`、`get_clients`、`get_networks`、`get_firewall_rules`（含 group 展開後的位址）、`get_port_forwards`、`get_dhcp_reservations`、`get_wlans`（PSK 已剝）、`get_lan_inventory`（controller 側的合併表；nmap 半邊由 im 在 `app` 跑，policy 說怎麼合）。
  - **operator 工具**（型別化、伺服器組 body、無 raw passthrough）：`set_dhcp_reservation(mac, ip, reason)`、`clear_dhcp_reservation(mac, reason)`、`block_client(mac, reason)`、`unblock_client(mac, reason)`、`set_port_forward(name, proto, wan_port, lan_ip, lan_port, reason)`、`delete_port_forward(id, reason)`、`set_dns_forward(domain, addr, reason)`（**spike 門控**：UniFi 的 API 有沒有暴露條件式轉發；沒有就不出這支）、`restart_device(mac, reason)`（gateway 拒）、`power_cycle_port(switch_mac, port, reason)`。每一支都走七道閘門與向內 dead-man（spec）。
  - **class A 判定**（每次從 reader 讀）：`POD_IP` 與 controller 所在的 network、節點的交換器 port／AP、涵蓋 pod↔controller 或 pod↔API server 的 firewall rule（group 展開）⇒ 拒絕並點名；讀不到 ⇒ 全部拒絕。
  - **向內 dead-man**：基線三條外部探針＋開票（GitHub issue，不含快照不含秘密）⇒ 等 FAC pre-ack 最多 `NETDEV_WITNESS_WAIT`（沒有不擋，記 `unmeasured`）⇒ token ⇒ 快照 ⇒ 套用 ⇒ 健檢（controller、API server、物件讀回、探針 ≥2/3、armed 時 post-ack）⇒ 失敗從 LAN 內 PUT 快照 ⇒ release 附三種讀數。
  - **稽核**：每一次 operator 呼叫一行 JSON 到 stdout（`ts`、`tool`、`target`、`phase`、`stated_reason`、`reason`／`rc`、`user`、`snapshot_digest`、`diff_digest`、`outbound`、`fac_witness`）。
- **`claude-session`**：`NETDEV_MCP_URL` 非空（空＝未設）⇒ 註冊 remote SSE server `netdev`（無 header，同 pod loopback）、`mcp__netdev` 合進 `permissions.allow`。
- **`im-policy.md` 新節「你的路由器」**：適用條件；自主與事後報告；`will_change` 是你自己的第二眼；被拒就照訊息處理不要繞；收到 `revert-failed` 第一件事是寫 inbox；清冊寫 `~/workspace/lan-inventory.yaml`、不進任何 repo；不用 `curl` 直接打 controller（你沒有憑證，那是設計）。
- **`Dockerfile`**：COPY 新 server；不加任何新套件——HTTP 客戶端用 `mcp` 已帶進來的 `httpx`（spike 1.1 確認在 `mcp` 的釘版裡）。
- **測試**：`test/test_netdev_mcp_server.py` 對 **假 controller**（in-process HTTP server，記錄每個請求的 method／path／body，供 fixture）、**假 FAC**（假 GitHub issues API）、**可開關的探針**（本機 listener）與 `kubectl` shim 跑 spec 的每一格，每格負對照；四個刻意變異（不拍快照、token 不消耗、class A 網路放行、探針全掉不回滾）各紅一次。`test/claude-session-controls.sh` 加 `NETDEV_MCP_URL` 的格；`test/im-policy-controls.sh` 加新節的格。
- **`CLAUDE.md`**：新節「Netdev MCP Sidecar」、Runtime Configuration 表的全部 env。

## Capabilities

### New Capabilities

- `netdev-mcp-server`：server 自己的契約——設定與三種啟動狀態、reader 面與剝秘密、operator 面的七道閘門、向內 dead-man 的每一步與讀數、class A 判定、票與 ack 的形狀、稽核欄位、TLS 規則。jg-base#222 據此寫 sidecar。

### Modified Capabilities

- `im-permission-profile`：tool surface 多了 `mcp__netdev`，條件是 `NETDEV_MCP_URL` 非空。

## Impact

- `netdev_mcp_server.py`（新）；`Dockerfile`（COPY）
- `claude-session`：`NETDEV_MCP_URL` 門（照 talos 那段）
- `im-policy.md`：新節；`CLAUDE.md`：新節＋表
- `test/test_netdev_mcp_server.py`（新）、`test/netdev-mcp-fixtures/`（新：controller 回應的 fixture，含一組帶 `x_passphrase` 的 WLAN 與帶 group 的 firewall rule）、`test/claude-session-controls.sh`、`test/im-policy-controls.sh`
- **不在本 repo**：jg-base#222（container、Secret、快照 PVC、daily-check 列）；jgct（`unifi_*` 欄位，root 4.1）；fleet-ops skill `unifi-im-credential`（建帳號、匯出 CA）與 `unifi-change-witness`（FAC 側）。
