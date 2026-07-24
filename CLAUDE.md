# CLAUDE.md — k8scc

## Project Overview

Containerised Claude Code CLI + ttyd web terminal.
- Image: `ghcr.io/ferry133/claude-code`
- Base: `debian:12-slim` + ttyd 1.7.7 + GNOME Keyring (OAuth token storage)
- Non-root user: `claude` (uid 1000), workspace at `/home/claude/workspace`
- Web terminal exposed on port 7681

## CI/CD

GitHub Actions workflow: `.github/workflows/build.yaml`
- Triggers: push to `main`, `workflow_dispatch`
- Builds multi-arch image: `linux/amd64`, `linux/arm64`
- Pushes to GHCR with `latest` tag (main branch) + short SHA tag

## GHCR Authentication — Important

**Use Classic PAT, not Fine-grained PAT.**

The workflow uses `secrets.GHCR_TOKEN` (not `secrets.GITHUB_TOKEN`) because:
1. The `claude-code` GHCR package was originally created by the `ferry133/jg-jiahd` repo's workflow, so it is bound to that repo. `GITHUB_TOKEN` from `k8scc` cannot write to it.
2. Fine-grained PATs have incomplete GHCR support and will fail with scope mismatch.

**Required secret**: `GHCR_TOKEN` = Classic PAT with `write:packages` scope
- Create at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
- Add to: `ferry133/k8scc` → Settings → Secrets and variables → Actions

## Network Diagnostics Toolkit

Installed for remotely supporting clients' jg-cluster-template deployments — clients typically lack networking background, so CC inventories their LAN and debugs router/DHCP/VPN/port-forward config directly.

Included: `nmap`, `fping`, `masscan`, `arp-scan`, `iproute2` (`ip`/`ss`/`bridge`), `net-tools`, `tcpdump`, `dnsutils` (`dig`/`nslookup`/`host`), `nbtscan`, `snmp` (`snmpwalk`), `mtr-tiny`, `traceroute`, `ipcalc`, `netcat-openbsd`, `socat`.

Deliberately omitted:
- `avahi-browse` — needs a privileged `avahi-daemon` + system D-Bus session; not worth running in this non-root container for one tool.
- `iw` / `nmcli` — manage host network interfaces via NetworkManager, which Talos nodes don't run.

**Packet-level tools need `CAP_NET_RAW`** (nmap SYN/OS-detection, masscan, arp-scan, tcpdump, fping ICMP). The image doesn't grant this itself — it's added at the pod `securityContext.capabilities.add` in the deploying HelmRelease (`jg-base`'s `claudecode/claude-code` app, and `jg-cluster-template`'s per-client `instances/helmrelease.yaml.j2`), alongside `drop: ["ALL"]`. The `claudecode` namespace also needs `pod-security.kubernetes.io/enforce: privileged` for `hostNetwork: true` to be allowed at all (baseline/restricted block host namespaces).

Per-client instances deploy with `replicas: 0` by default — this is a LAN-facing, network-scanning-capable shell; scale to 1 only while actively supporting that client.

## Login Link Helper (patched ttyd client)

Claude Code's OAuth login URL (~450 chars) is hard-wrapped by its TUI with real
newlines at the terminal width, so ttyd's stock xterm.js client links only the
first (truncated) line — clicking it opens a broken URL, and no client-side link
regex can rejoin hard-wrapped lines.

Fix: `patch-ttyd-index.py` (build time) extracts the gzipped index.html embedded
in the ttyd binary and injects `login-link-helper.js` before the client bundle;
`entrypoint.sh` serves it via `ttyd --index /usr/local/share/ttyd/index.html`.
The helper:
1. Taps the ttyd WebSocket, learns terminal width from auth/resize frames, and
   reassembles the full URL across wrapped lines (full-width line ⇒ continues).
2. Overlays a clickable "開啟 Claude 登入頁 / Open sign-in page" button (hides on
   `Login successful`, dismissible with ✕).
3. Wraps `window.open()` with a facade so clicking the truncated in-terminal
   link navigates to the full reassembled URL (WebLinksAddon opens links as
   `w = window.open(); w.location.href = url`).

Unit-testable without a browser: stub `window`/`document`/`WebSocket` and feed
simulated frames (the helper is a self-contained IIFE using only those globals).

## Runtime Configuration

| Env Var | Description |
|---------|-------------|
| `DATABASE_URL` | Optional PostgreSQL DSN; enables MCP memory server when set |
| `AUTH0_DOMAIN` | Optional Auth0 domain for device-flow login |
| `AUTH0_CLIENT_ID` | Optional Auth0 client ID for device-flow login |
| `CLAUDE_USER_ID` | Set automatically by Auth0 login; used as `agent_id` in DB (default: `claude_code`) |

## Persistent Memory

Two layers of persistence:

| Layer | Mechanism | Storage | Notes |
|-------|-----------|---------|-------|
| Auto-memory | Claude Code built-in | PVC at `/home/claude/.claude` | Survives container rebuild; `.md` files |
| Explicit memory | MCP Memory Server | PostgreSQL `knowledge` table | Enabled when `DATABASE_URL` is set; `remember()` / `recall()` / `forget()` tools |

The MCP server (`memory_mcp_server.py`) uses a **dedicated PostgreSQL instance** (separate from linebot), keyed by `agent_id = CLAUDE_USER_ID`.
