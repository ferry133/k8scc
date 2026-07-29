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
1. Taps the ttyd WebSocket, learns terminal size from auth/resize frames, and
   reassembles the full URL across wrapped lines (full-width line ⇒ continues).
2. Overlays a clickable "開啟 Claude 登入頁 / Open sign-in page" button. Shown
   iff a login URL appears *later in the stream* than the last post-login
   marker (`Welcome back`, `? for shortcuts`, …) — ordering, not proximity, so
   partial redraws can't confuse it. Screen-clear (`CSI 2J`, alt-screen) resets
   the buffer.
3. Wraps `window.open()` with a facade so clicking the truncated in-terminal
   link navigates to the full reassembled URL (WebLinksAddon opens links as
   `w = window.open(); w.location.href = url`).
4. Auto-submits the auth code: on tab focus (or via the 📋 button as a
   user-gesture fallback for clipboard permission), reads the clipboard and, if
   the text ends with `#<state>` matching the captured URL's OAuth `state`
   param, types it into the terminal via the WebSocket (`'0'` input frame) +
   Enter. State validation means nothing else can ever be injected; clipboard
   is only read while a login is pending. A fully automatic callback is
   impossible remotely — Anthropic's OAuth app only allows localhost or
   platform.claude.com redirect URIs.

Unit-testable without a browser: stub `window`/`document`/`WebSocket` and feed
simulated frames (the helper is a self-contained IIFE using only those globals).

## Talos MCP Sidecar (isolated, read-only cluster diagnostics)

A second container, `talos-mcp` (same image, `command` override — see below), gives the agent read-only Talos diagnostics for the client's own cluster without any credential crossing the remote-operator boundary. See `openspec/changes/insideman` in `jg-base` for the full design.

- **Credential**: an `os:reader`-scoped Talos client certificate (Talos's own native RBAC role, independent of Omni — `talosctl config new --roles=os:reader`), unique per client, bootstrapped once at onboarding. `os:reader` is enforced by the Talos API server itself: any mutating RPC (reboot, config apply, upgrade) is rejected regardless of what the calling code requests.
- **Isolation**: the credential is mounted only into the `talos-mcp` container's own filesystem/env — never the `app` container the terminal user/agent shell runs in. The two containers share a pod (`hostNetwork: true`, so the same network namespace) but not a filesystem.
- **Transport**: `talos_mcp_server.py` runs as a long-lived process (not a stdio subprocess like `memory`), exposing MCP over SSE bound to `127.0.0.1:8765`. Started via a `command` override (`python3 /usr/local/bin/talos_mcp_server.py`) that bypasses `entrypoint.sh` entirely — this container never runs ttyd.
- **Registration**: `claude-session` registers it in `settings.json` as a remote MCP server (`"type": "sse"`) when `TALOS_MCP_URL` is set — a different registration shape than `memory`'s local stdio entry, since a remote/sidecar server needs a URL, not a `command`/`args` pair to spawn.
- **Tool surface** (read-only by construction; no mutating tool exists in this file, full stop): `get_node_status`, `get_etcd_members`, `get_link_status`, `get_service_logs` — each shells out to the bundled `talosctl` binary.

## Runtime Configuration

| Env Var | Description |
|---------|-------------|
| `DATABASE_URL` | Optional PostgreSQL DSN; enables MCP memory server when set |
| `TTYD_INTERFACE` | Optional bind address for ttyd (e.g. `127.0.0.1` when an oauth2-proxy sidecar fronts it in the same netns) |
| `TTYD_AUTH_HEADER` | Optional trusted header ttyd requires (e.g. `X-Forwarded-Email` from oauth2-proxy); its value surfaces to `claude-session` as `TTYD_USER` |
| `CLAUDE_USER_ID` | Resolved in `claude-session` from `TTYD_USER` if present, else this var, else `claude_code`; used as `agent_id` in the memory DB |
| `TALOS_MCP_URL` | Optional; set only when the `talos-mcp` sidecar exists. Registers it as a remote MCP server (see above) |
| `TALOS_NODES` (talos-mcp container only) | Comma-separated node IPs the sidecar's credential is scoped to |

## Persistent Memory

Two layers of persistence:

| Layer | Mechanism | Storage | Notes |
|-------|-----------|---------|-------|
| Auto-memory | Claude Code built-in | PVC at `/home/claude/.claude` | Survives container rebuild; `.md` files |
| Explicit memory | MCP Memory Server | PostgreSQL `knowledge` table | Enabled when `DATABASE_URL` is set; `remember()` / `recall()` / `forget()` tools |

The MCP server (`memory_mcp_server.py`) uses a **dedicated PostgreSQL instance** (separate from linebot), keyed by `agent_id = CLAUDE_USER_ID`.
