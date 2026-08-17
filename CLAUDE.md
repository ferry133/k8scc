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

- **Credential**: a dedicated **Omni service account holding the Omni `Reader` role**, plus the talosconfig `omnictl talosconfig -c <cluster>` issues for that identity — one per client, bootstrapped once at onboarding. The Talos-level ceiling is a function of the identity's *Omni* role, not a per-request flag: minting a narrower cert with `talosctl config new --roles=os:reader` is a dead end, because it requires the caller to already hold `os:admin` and no Omni-obtainable identity ever does (not an Admin-role user, not an Admin-role SA, not `--break-glass`). Enforcement is server-side: any mutating or admin-only RPC (reboot, config apply, upgrade, cert minting) is rejected regardless of what the calling code requests.
- **Credential shape**: three values, not one. The talosconfig file carries *no key material* — a fresh container has no locally-registered PGP key, so `OMNI_SERVICE_ACCOUNT_KEY` and `OMNI_ENDPOINT` must also be in the container's env; talosctl's own auth library reads them directly. `OMNI_ENDPOINT` must be a direct gRPC path, never one behind a Cloudflare Tunnel — the Tunnel breaks the gRPC trailers the Talos siderov1 proxy depends on (confirmed 2026-07-30 against jg-jiahd).
- **Isolation**: the credential is mounted only into the `talos-mcp` container's own filesystem/env — never the `app` container the terminal user/agent shell runs in. The two containers share a pod (`hostNetwork: true`, so the same network namespace) but not a filesystem.
- **Transport**: `talos_mcp_server.py` runs as a long-lived process (not a stdio subprocess like `memory`), exposing MCP over SSE bound to `127.0.0.1:8765`. Started via a `command` override (`python3 /usr/local/bin/talos_mcp_server.py`) that bypasses `entrypoint.sh` entirely — this container never runs ttyd.
- **Registration**: `claude-session` registers it in `settings.json` as a remote MCP server (`"type": "sse"`) when `TALOS_MCP_URL` is set — a different registration shape than `memory`'s local stdio entry, since a remote/sidecar server needs a URL, not a `command`/`args` pair to spawn.
- **Tool surface** (read-only by construction; no mutating tool exists in this file, full stop): `get_node_status`, `get_etcd_members`, `get_link_status`, `get_service_logs` — each shells out to the bundled `talosctl` binary.

## Factory Variant (`factory-*` tags)

The `Dockerfile` builds **two** targets. Building it with no `--target` gives you the factory image, so both the workflow and any local build must name one.

| Target | Tags | Contents |
|--------|------|----------|
| `base` | `<short-sha>`, `latest` | What every existing consumer pins. Unchanged by the factory work. |
| `factory` | `factory-<short-sha>`, `factory-latest` | `base` + cluster-build toolchain + `omni-machine-watch` |

Same GHCR repository (`ghcr.io/ferry133/claude-code`), not a sibling package: a new package would be created private and bound to `k8scc`, so every consumer would need a pull secret and a visibility flip before it could pull anything. The tag prefix carries the distinction instead. Consumers pin short SHAs (not `latest`), so `factory-<sha>` can never collide with a tag someone is already pulling.

### Toolchain

Beyond `base`, the factory target adds `omnictl` `gh` `cloudflared` `age`/`age-keygen` `sops` `cue` `makejinja` `task` `helm` `helmfile` `talhelper` `flux` `kustomize` `kubeconform` `yq` `jq` `uv`, and re-pins `kubectl`/`talosctl` to the driven repo's versions rather than the hosting cluster's.

Two rules govern the pins:

- **They are a copy of the driven repo's `.mise.toml`, and copies drift.** Bump them together. A mismatch surfaces as a cue schema or makejinja rendering error partway through a client's cluster build, not as a clean startup failure.
- **`helm` `talhelper` `flux` `kustomize` `kubeconform` `yq` `jq` are not in the issue's list.** They are there because the named tools shell out to them — `task configure`/`bootstrap` targets in the driven repo reference `talhelper` 16×, `sops` 17×, `yq`/`kubeconform`/`flux` 8× each. Shipping `task` without them delivers a tool that cannot run.

`makejinja` cannot use the system `pip3`: it requires Python ≥ 3.12 and `debian:12` ships 3.11. It is installed with `uv` against a managed CPython matching the driven repo's own Python pin.

Every binary is exercised in the final build layer, per target platform under emulation, so a wrong-arch or truncated download fails the build instead of a client's cluster build.

One exception, because it bites: **`makejinja --version` reports the wrong number.** `cli.py` uses `@click.version_option(None)`, whose auto-detection resolves to the wrong distribution and prints rich-click's version (`1.9.8`) for makejinja `2.8.2`. The real version is asserted at install time from the distribution metadata and fails the build on a mismatch; the final layer only checks the entry point runs. Do not "fix" a future version bump by trusting that flag.

### `omni-machine-watch`

A long-lived Go process holding a COSI watch (`safe.StateWatchKind` on `MachineStatuses.omni.sidero.dev`) — `omnictl` stays for cluster-creation calls but cannot serve this, as `omnictl get --watch` prints a human table and dies with the command. Source in `omni-machine-watch/`, built against `github.com/siderolabs/omni/client`.

- **Output**: one JSON object per line on stdout (`created`/`updated`/`destroyed`/`bootstrapped`/`reconnect`/`stopped`), operational noise on stderr. `bootstrapped` marks the boundary between existing contents and live changes — that is what a "wait until the machine appears" caller keys on.
- **Lifetime is the caller's**: SIGINT/SIGTERM cancels the context, which tears down the subscription and exits 0.
- **Reconnects with backoff.** The example in the Omni source returns on `state.Errored`; a factory run outlives its watch being dropped by a restart or LB timeout, so this re-establishes and re-bootstraps instead of going silently quiet.
- **Not started by `entrypoint.sh`.** It needs an Omni credential and the terminal container must not hold one — the same split as the `talos-mcp` sidecar. Run it as its own container (`command: ["/usr/local/bin/omni-machine-watch"]`) or from a shell that already has the credential in its environment.
- **Testable without an Omni**: `stream()` takes a `state.CoreState`, so `main_test.go` drives it against an in-memory COSI state. This is not decoration — a live watch on an idle instance emits no changes, so "no `updated` events" there cannot distinguish a quiet cluster from a broken post-bootstrap stream. `go test` runs in the builder stage, so a regression fails the image build.

## Runtime Configuration

| Env Var | Description |
|---------|-------------|
| `DATABASE_URL` | Optional PostgreSQL DSN; enables MCP memory server when set |
| `TTYD_INTERFACE` | Optional bind address for ttyd (e.g. `127.0.0.1` when an oauth2-proxy sidecar fronts it in the same netns) |
| `TTYD_AUTH_HEADER` | Optional trusted header ttyd requires (e.g. `X-Forwarded-Email` from oauth2-proxy); its value surfaces to `claude-session` as `TTYD_USER` |
| `CLAUDE_USER_ID` | Resolved in `claude-session` from `TTYD_USER` if present, else this var, else `claude_code`; used as `agent_id` in the memory DB |
| `TALOS_MCP_URL` | Optional; set only when the `talos-mcp` sidecar exists. Registers it as a remote MCP server (see above) |
| `TALOSCONFIG` (talos-mcp only) | Path to the mounted talosconfig (`/etc/talos-mcp/talosconfig`). Routing metadata only — see `OMNI_SERVICE_ACCOUNT_KEY` |
| `OMNI_SERVICE_ACCOUNT_KEY` (talos-mcp only) | The Reader-role Omni SA's actual bearer credential; read straight from env by talosctl's auth library |
| `OMNI_ENDPOINT` (talos-mcp only) | Direct gRPC endpoint for the client's Omni. Must bypass any Cloudflare Tunnel (gRPC trailers) |
| `OMNI_ENDPOINT`, `OMNI_SERVICE_ACCOUNT_KEY` (omni-machine-watch) | Same pair as talos-mcp, same constraint on the endpoint being direct gRPC. The key is read from env only and deliberately has no flag — flags land in `ps` output |
| `TALOS_NODES` (talos-mcp only) | **Optional, unset by default.** Comma-separated node IPs; tool calls naming anything else are refused. A friendlier error, not a security boundary — the credential already resolves to one cluster server-side. Deliberately not wired into the jg-cluster-template pipeline: Omni clusters render `nodes: []`, so the list would be hand-written, and a stale entry blocks diagnostics exactly when they are needed |

## Persistent Memory

Two layers of persistence:

| Layer | Mechanism | Storage | Notes |
|-------|-----------|---------|-------|
| Auto-memory | Claude Code built-in | PVC at `/home/claude/.claude` | Survives container rebuild; `.md` files |
| Explicit memory | MCP Memory Server | PostgreSQL `knowledge` table | Enabled when `DATABASE_URL` is set; `remember()` / `recall()` / `forget()` tools |

The MCP server (`memory_mcp_server.py`) uses a **dedicated PostgreSQL instance** (separate from linebot), keyed by `agent_id = CLAUDE_USER_ID`.
