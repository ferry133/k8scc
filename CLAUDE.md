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
- One workflow, three images into one GHCR repository, distinguished by tag
  prefix: base (`<sha>`), factory (`factory-<sha>`, from the same `Dockerfile`)
  and ops (`ops-<sha>`, from `Dockerfile.ops`). See the per-variant sections
  below — each has its own reason for the prefix and its own build gate.

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

Included: `nmap`, `fping`, `masscan`, `arp-scan`, `iproute2` (`ip`/`ss`/`bridge`), `net-tools`, `tcpdump`, `dnsutils` (`dig`/`nslookup`/`host`), `nbtscan`, `snmp` (`snmpwalk`), `nfs-common` (`showmount`), `mtr-tiny`, `traceroute`, `ipcalc`, `netcat-openbsd`, `socat`.

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

## Talos MCP Sidecar (isolated cluster diagnostics, plus one gated reboot)

A second container, `talos-mcp` (same image, `command` override — see below), gives the agent Talos diagnostics for the client's own cluster without any credential crossing the remote-operator boundary. Two credentials can be mounted into it, and only into it. The design lives in `jg-base`: `openspec/changes/insideman` (the reader) and `openspec/changes/im-onsite-operator-tools` (the operator; this repo's sub-change of the same name, archived 2026-10-16 Taipei (the folder carries the UTC date), left its contract in `openspec/specs/talos-operator-escalation`, `im-permission-profile` and `operator-inbox`, and its decisions in `openspec/changes/archive/2026-10-15-im-onsite-operator-tools/design.md`).

### Reader credential (always, fleet-wide)

- **Credential**: a dedicated **Omni service account holding the Omni `Reader` role**, plus the talosconfig `omnictl talosconfig -c <cluster>` issues for that identity — one per client, bootstrapped once at onboarding. The Talos-level ceiling is a function of the identity's *Omni* role, not a per-request flag: minting a narrower cert with `talosctl config new --roles=os:reader` is a dead end, because it requires the caller to already hold `os:admin` and no Omni-obtainable identity ever does (not an Admin-role user, not an Admin-role SA, not `--break-glass`). Enforcement is server-side: any mutating or admin-only RPC (reboot, config apply, upgrade, cert minting) is rejected regardless of what the calling code requests.
- **Credential shape**: three values, not one. The talosconfig file carries *no key material* — a fresh container has no locally-registered PGP key, so `OMNI_SERVICE_ACCOUNT_KEY` and `OMNI_ENDPOINT` must also be in the container's env; talosctl's own auth library reads them directly. `OMNI_ENDPOINT` must be a direct gRPC path, never one behind a Cloudflare Tunnel — the Tunnel breaks the gRPC trailers the Talos siderov1 proxy depends on (confirmed 2026-07-30 against jg-jiahd).
- **Isolation**: both credentials are mounted only into the `talos-mcp` container's own filesystem/env — never the `app` container the terminal user/agent shell runs in. The two containers share a pod (`hostNetwork: true`, so the same network namespace, and the same ServiceAccount) but not a filesystem.
- **Transport**: `talos_mcp_server.py` runs as a long-lived process (not a stdio subprocess like `memory`), exposing MCP over SSE bound to `127.0.0.1:8765`. Started via a `command` override (`python3 /usr/local/bin/talos_mcp_server.py`) that bypasses `entrypoint.sh` entirely — this container never runs ttyd. Startup readings (reader ok/missing, operator state, whether `reboot_node` registered, `kubectl auth can-i list nodes`) go to stderr.
- **Registration**: `claude-session` registers it in `~/.claude/.claude.json` as a remote MCP server (`--transport sse`) when `TALOS_MCP_URL` is set — a different registration shape than `memory`'s local stdio entry, since a remote/sidecar server needs a URL, not a `command`/`args` pair to spawn. The same gate writes the permission profile (below).

### Operator credential (optional, per client: `TALOS_OPERATOR_CONFIG`)

Why it exists: 2026-10-11 on jg-jiahd, `im` diagnosed the fault correctly and could not act. SideroLink was down, so the Omni-proxied path timed out, and the Bash classifier blocked `talosctl` outright in auto mode. One credential closes both gaps: an `os:operator` certificate minted directly against the cluster's Talos CA (fleet-ops skill `talos-operator-credential`), which does not pass through Omni.

- **Absent, empty (0 bytes), unreadable or expired ⇒ exactly the reader-only server.** 0 bytes is what a Secret key rendered from an unset value mounts as; the root spec makes empty and absent the same thing. Expiry is read once at startup from `talosctl config info` (`Certificate expires: … (YYYY-MM-DD)`; a date ≤ today is expired). A credential whose expiry cannot be read is **not used** — fail closed, with the reason carried in the message that would have used it — rather than a tool that works until the day it silently does not. Fewer than 30 days left ⇒ every tool response starts with a warning line naming the date and `talos-operator-credential rotate`.
- **Read fallback, once.** Each read tool tries the Omni path first. On a transport-class failure (timeout, `Unavailable`, `no route to host`, `context deadline exceeded`) it retries exactly once with `--endpoints <node>` and the operator credential. Any other failure (`PermissionDenied`, a bad argument) is returned as-is — that is not the road. Every read response that reached a Talos API begins `[via Omni]` or `[via direct LAN]` (after the expiry warning line when there is one). A refusal before any path — unconfigured reader, `TALOS_NODES` miss — carries no marker, because nothing was *via* anything. Without a usable operator credential the Omni error is followed by `direct-LAN fallback not available on this cluster (<reason>)`, where the reason names the expiry date or the empty file.
- **`reboot_node(node, reason, confirm="")`** — registered only while the operator credential is usable; there is no "present but refuses" state. Six gates, all server-side and none of them a human prompt (ferry133, 2026-10-15: im is the company's tool, only company staff reach the ttyd, and a reboot is within im's own authority):
  1. **Allowlist at call time**: `kubectl get nodes -o json` InternalIPs through the pod's own ServiceAccount (same pod, same SA as `app`; `get nodes` needs no new RBAC). `TALOS_NODES` is not consulted. `kubectl` failure ⇒ refused with its error text — fail closed, never a static list.
  2. **A stated reason**: `reason` is required in the MCP schema; empty or whitespace ⇒ refused before any lookup. It is written on every audit line as `stated_reason` and never judged by the server — the reader is the company, afterwards.
  3. **Control-plane quorum**: from the same `get nodes` reading, any *other* control-plane node (label `node-role.kubernetes.io/control-plane`) not `Ready=True` ⇒ refused, naming it. The target's own readiness is not consulted (a NotReady target is usually why it is being rebooted) and workers do not count. Re-read at execute.
  4. **Per-node cooldown**: for 600 s after a reboot was issued — one `--wait` window — the same node is refused in both phases, with the seconds left. For other nodes the record is in memory, so a sidecar restart for any other reason starts clean (the audit log still shows two `execute` lines close together). **The own node is counted from its last boot instead** — `/proc/uptime`, which is not namespaced and reads the host's clock — because the own-node reboot restarts the sidecar and erases the record it would have checked: fleet-ops-77 measured exactly that hole on a throwaway 3-CP cluster, 2026-10-15. Any boot counts, not only one this tool ordered; an unreadable clock refuses (fail closed). The startup `reboot_node guards:` line prints the uptime it read; that `/proc/uptime` is the host's on a Talos pod is root 7.4's reading, not yet taken.
  5. **Two phases**: without `confirm` → no action, and the response is a single-use token bound to the node, `expires_in: 120`, `cooldown_after: 600`, and `will_run`, the literal argv. This is the agent's own second look at `will_run` against its reason, not a hand-off to a person. With a valid token → it runs, the token consumed *before* the run. Replay, another node, or more than 120 s ⇒ refused with the reason. The response states talosctl's exit code and carries **both** streams: `reboot --wait` reports its sequence on stderr and exits 0 with an empty stdout, so a stdout-only success path answered `(empty response)` for a reboot that had happened (jg-jiahd, 2026-10-15, k8scc#22). Both phases also say that a node which comes back SchedulingDisabled should be checked for *who* cordoned it before it is uncordoned: on jg-jiahd it had been cordoned by hand 4 min before the token, that cordon survived the reboot, and nothing in the tool uncordons. Whether Talos undoes its *own* graceful-reboot cordon at boot is unmeasured (root 5.5); the note asserts neither. `confirm: bool` was rejected as a design: an LLM fills in `true`.
  6. **The verb is literal**: `node` and `confirm` never become shell words (`subprocess` list form — measured, the shim logs one field per argv element), and `node` must equal an allowlisted IP exactly, so `10.9.9.23 --mode powercycle` is refused before talosctl sees it.

  Own node: `POD_IP` (downward API `status.podIP`; hostNetwork ⇒ the node's address). `node == POD_IP` ⇒ `--wait=false`, and both phases say that im goes down with the node — on a single-node cluster the whole service — and that nobody sees the result until the pod is back; any other node ⇒ `--wait=true --timeout=600s`. ⚠️ **`talosctl reboot --wait` defaults to true** (talosctl 1.12), so "without `--wait`" has to be spelled `--wait=false` — a bare `reboot` would wait on its own death.
- **Audit**: every `reboot_node` call — token, execute, refused — writes one JSON line to the sidecar's stdout (`ts`, `tool`, `node`, `phase`, `stated_reason`, `reason` or `rc`, `user`), i.e. `kubectl logs -c talos-mcp`. Stdout carries nothing else: `__main__` points `sys.stdout` at stderr, so uvicorn's access log lands there (k8scc#46, the same swap as netdev-mcp). The direct-LAN path never passes Omni, so this is its only record. `user` is the sidecar's own `CLAUDE_USER_ID` env — **not** the ttyd session's identity, which lives in `app` and does not cross. jg-base's HelmRelease deliberately does not set it on the `talos-mcp` container (root decision, 2026-10-13), so today the field is empty.
- **Nobody is prompted; the reboot is within im's own authority.** ferry133, 2026-10-15: im is the company's tool for maintaining the customer's cluster on its own, and only company employees ever reach the ttyd — never the customer. So im diagnoses, states its reason, runs the reboot through the gates above, and reports afterwards to the company (a dated note in `~/workspace/inbox/`, or the cluster's health mail). `claude-session` writes `permissions.allow` for all five `mcp__talos__*` tools whenever `TALOS_MCP_URL` is set, and moves its own earlier `mcp__talos__reboot_node` entry out of `ask` — pods that ran the previous image carry it on the PVC. It merges rather than overwrites: existing entries keep their order, a second run is byte-identical (the file is not even rewritten), and an unparseable file is left alone and the terminal told. What `ask` does in auto mode (root tasks 5.6 in `jg-base`) no longer bears on this profile. The same three statements reach im *inside* the pod through `im-policy.md`, shipped as `/etc/claude-code/CLAUDE.md` — Claude Code's managed policy memory on Linux, loaded first, for every user, not droppable by personal settings (root 7.3b). The repo copy is deliberately not named `CLAUDE.md`, so a session working in this repo does not take im's orders as its own; `test/im-policy-controls.sh` checks the COPY line and the three statements, each with a control.

### Inbox convention (`~/workspace/inbox/`)

Company colleagues who are not at the ttyd drop hand-over notes for im there (`kubectl cp` or `kubectl exec` into the `app` container), and im leaves its own reports there for them. The directory is on the workspace PVC (`im-claude-workspace` in jg-base), so it survives pod restarts. On every ttyd session start `claude-session` counts the files not ending in `.read` and prints `📥 inbox: N file(s) — read ~/workspace/inbox/ before anything else` — one line, contents never opened, nothing at all when the directory is empty, absent, or fully read. The agent renames a file to `<name>.read` once it has acted on it. The line is followed by an 8-second, Enter-skippable pause, because tmux clears the screen the moment it attaches and a line shown for one frame reads the same as no line.

### Apps repo (`IM_APPS_REPO`, openspec `im-apps-repo`)

im gets one private repo per cluster (`<cluster>-apps`), reconciled by the cluster's Flux under a restricted ServiceAccount; the root change is `fleet-ops/openspec/changes/standalone-app-repos`. This repo's sub-change, archived 2026-10-18, left its contract in `openspec/specs/im-apps-repo` and `im-permission-profile`, and its decisions in `openspec/changes/archive/2026-10-18-im-apps-repo/design.md`. This repo's share is the write path:

- **Image (`base`)**: `sops`, pinned like `omnictl` (version ARG before the first `FROM`, per-arch sha256 in the Dockerfile), and `/etc/ssh/github-known-hosts` — committed as `github-known-hosts`, built from `api.github.com/meta`, with the three fingerprints pinned separately in the Dockerfile and compared at build (`known_hosts_match`, driven by `test/assert-controls.sh`, extracted not copied). No `age` CLI: im encrypts to the tenant's public key and never holds a private one.
- **`claude-session` (`configure_apps_repo`)**: only when `IM_APPS_REPO` (`owner/repo`) is non-empty **and** the key at `IM_APPS_KEY` (default `~/.ssh/im-apps.key`) exists and passes ssh's own permission rule (group/other bits refused only when the caller owns the key — today im is root and Secret volumes are root-owned, so 0400/0600 both pass). Then it writes `~/.ssh/config` (alias `github.com-im-apps`, `IdentitiesOnly yes`, the pinned known_hosts, `StrictHostKeyChecking yes`; first line marks it as this script's, and a file without the mark is never overwritten), sets `user.name im`, `user.email im@<repo half>` and **`core.sshCommand = ssh -F ~/.ssh/config`** in `~/.gitconfig`, and merges `Bash(git *)`/`Bash(sops *)` into `permissions.allow`. Any precondition missing ⇒ nothing written and one `⚠ apps repo 沒有設定：…` line; `IM_APPS_REPO` unset ⇒ silent.
- ⚠️ **The `-F` is load-bearing.** OpenSSH reads the user config from the passwd entry's home, not `$HOME`; the pod runs as uid 0 with `HOME=/home/claude`, so without `-F` the alias is never read while git happily reads `~/.gitconfig` — an identity with no key. The controls exercise exactly that: with `HOME` redirected, a bare `ssh -G <alias>` must miss the key and git's own `core.sshCommand` must find it.
- **Policy**: `im-policy.md` "你的 apps repo" — manifests only, every Secret sops-encrypted before commit, no logs/IPs in commits, persistent changes through the repo rather than `kubectl apply`, reconcile with two `kubectl annotate … --overwrite`, GitRepository first, then Kustomization (k8scc#39: the source pulls only hourly, so the Kustomization alone re-applies the old artifact).
- **Not here**: mounting the key and setting `IM_APPS_REPO`/`IM_APPS_KEY` are jg-base's HelmRelease (jg-base#183). It mounts the whole Secret as a directory rather than by `subPath`, since a subPath mount never receives a rotated key and `~/.ssh` has to stay writable; that is why the key path is an env var.

### Home Assistant MCP (`HA_MCP_URL`, openspec `im-ha-mcp`)

im reaches the customer's Home Assistant through HA's own MCP server, as a non-admin HA user `im`. The root change is `fleet-ops/openspec/changes/im-home-automation` (HA as a jg-extras extra, coexisting with homebridge); this repo's sub-change is `openspec/changes/im-ha-mcp`. This repo's share is the registration and the policy:

- **`claude-session` (`configure_ha_mcp`)**: only when `HA_MCP_URL` is non-empty **and** the token file at `HA_MCP_TOKEN_FILE` (default `/etc/ha-mcp/token`) exists and is non-empty after stripping. Then it writes the `ha` entry into `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json` and merges the **server-level** rule `mcp__ha` into `permissions.allow`. URL set but token missing, empty or unreadable, or a `.claude.json` that is not valid JSON ⇒ nothing written and exactly one `⚠ Home Assistant 沒有設定：…` line; `HA_MCP_URL` unset or empty ⇒ silent.
- ⚠️ **Not `claude mcp add --header`.** That puts the token in argv, where `ps` shows it. python writes the entry `claude mcp add --scope local` would have written — measured on Claude Code 2.1.261 (darwin-x64 build of the image's pin, 2026-10-26): `projects[<realpath cwd>].mcpServers.ha = {"type": "sse", "url": …, "headers": {"Authorization": "Bearer …"}}` (`--transport http` writes the same shape with `"type": "http"`, and `claude mcp get ha` answers rc 0 with `Type: http`, while an unregistered name answers rc 1 — both measured on that same 2.1.261 darwin build; the linux build inside the image is unmeasured), in `$CLAUDE_CONFIG_DIR/.claude.json` when that is set (jg-base sets `/home/claude/.claude`, the PVC) and `$HOME/.claude.json` otherwise, mode 0600; `claude mcp get ha` read a python-written entry back. The token is read by python from the file and goes only into that entry — no argv, no env, no output.
- ⚠️ **Never show `claude mcp get ha`'s output.** It prints the header in full (measured). It also exits 0 for a registered server that cannot be reached, and 1 only for one that is not registered — so it says nothing about health. `claude mcp list` is **unmeasured**: on an unauthenticated machine it answers `Not logged in`, and after it ran `mcp get ha` answered the same, so that session had no positive control left. The policy tells im to run neither, to never read or print the `ha` entry, and to check HA by calling one `mcp__ha__*` tool.
- **Rotation without a record of its own**: the current entry is compared with the one that would be written, and the file is rewritten only when they differ. An unchanged token leaves the file untouched (second run is a no-op, mtime included); a rotated one is rewritten once; an entry removed by hand comes back on the next session. (The design first planned a sha256 file of the last token; comparing the entry itself also covers the hand-removed case and is one file fewer.)
- **Why `mcp__ha` and not a list**: HA's tool names follow its Assist expose settings and its version, so an enumerated list goes stale the day the company exposes another kind of entity, and stale looks like "im is asked every time" — in auto mode the same as the tool being gone. Claude Code 2.1.261's `doctor` reports a malformed rule (`Bash(unclosed`) as invalid and does not report `mcp__ha`; whether that rule really goes unprompted needs a live HA and is root 7.3/7.5's reading, not yet taken.
- **Why this token sits in `app` while the Talos and UniFi credentials do not** (root D3): it belongs to a non-admin HA user that cannot touch integrations or users, it is revocable in seconds from HA's UI, and `app` is cluster-admin anyway — the isolation in this pod guards against accidental disclosure, not a determined operator. It lands on the PVC in `.claude.json`, which `claudecode-state-backup` encrypts with age off-site; the root accepted that.
- **No reverse cleanup** (D-f): once HA is removed (URL empty), the `ha` entry and `mcp__ha` stay. The rule opens nothing without a server; the entry makes `claude` report one unreachable MCP server per session. Same decision as talos and git/sops. Clearing it is `claude mcp remove ha`, by a person.
- **Policy**: `im-policy.md` "你的家庭自動化" — im operates exposed entities and writes automations and scenes on its own judgement, reports afterwards in `~/workspace/inbox/`, keeps automations in HA only, and does not do five things: HomeKit Bridge enable/pairing, Google Assistant account linking, HA user/integration add/remove, HA upgrades, homebridge. `test/im-policy-controls.sh` drops each statement in turn and must go red each time.
- **Transport is `http` (streamable HTTP).** fleet-ops-65 measured HA 2026.10.4 on jg-jcc2 (2026-10-26), with the `mcp_server` integration set up: `POST /api/mcp` initialize ⇒ 200 `application/json`, `GET /api/mcp` ⇒ 405, without a token ⇒ 401; the legacy `GET /mcp_server/sse` ⇒ 200 `text/event-stream`. So `type: sse` works only with `/mcp_server/sse`; the URL im gets is `/api/mcp` (root ruling on jgct#245: through the tenant's public HTTPRoute), so the entry says `http`. The first image wrote `sse`; such an entry differs from the one wanted and is rewritten once, like a rotation. Without the `mcp_server` integration every path is 404 — it is an onboarding step (fleet-ops `ha-im-token`), not im's.
- **Not here**: the HA extra (jg-extras#13), `ha_im_token` and the `HA_MCP_URL` derivation (jgct#237), mounting the token and setting the env on `app` (jg-base#221), and creating the HA user and token (fleet-ops skill `ha-im-token`).

### Netdev MCP Sidecar (`NETDEV_MCP_URL`, openspec `im-netdev-mcp`)

A third container, `netdev-mcp` (same image, `command: python3 /usr/local/bin/netdev_mcp_server.py`), gives im the customer's UniFi controller: ten reader tools, and eight write tools guarded by seven server-side gates and an inward dead-man. The root change is `fleet-ops/openspec/changes/im-unifi-management`; this repo's sub-change is `openspec/changes/im-netdev-mcp`, whose spec `netdev-mcp-server` is the contract jg-base#222 builds the container from (reader-only overlay: jg-base#231). **Every write cell was measured against fakes only** — no real controller, no real GitHub (root §6 on FAC1).

- **Credentials stay in the sidecar**, the same split as talos-mcp. The reader's Integration API key comes from `UNIFI_READER_KEY_FILE` at startup (never env or argv) and is sent as `X-API-Key` on reads only. The operator account (`UNIFI_OPERATOR_FILE`, two lines: user, password) logs in once at startup over the legacy API (`POST /api/auth/login`, cookie + `X-CSRF-Token`), on **its own HTTP client**, so the reader key never rides a write and the operator cookie never rides a read; a 401 later re-logs in once. The ticket token (`IM_FAC_TICKET_TOKEN_FILE`) is read the same way. None of the three, nor the cookie or CSRF value, appears in any reading, response, audit line or ticket (a control asserts all five).
- **TLS is always verified**, against `UNIFI_CA_FILE` (the console's self-signed certificate or its CA, exported at onboarding by fleet-ops skill `unifi-im-credential`). There is no off switch, on purpose. `http://` is refused unless `NETDEV_ALLOW_INSECURE_HTTP=1`, which exists for the controls' fakes (controller and ticket API alike), and the startup readings say when it was accepted. A CA file that cannot be loaded is an error answer, never a fallback.
- **Three startup states**: reader input missing (URL, key, or CA for https) ⇒ every tool answers `not configured`, naming what is missing, without a request. Reader usable but any write input not usable ⇒ the ten reader tools only. The write inputs are: operator file present and its login accepted, ticket repo + token, exactly three outside probes, `POD_IP`, and a writable `NETDEV_SNAPSHOT_DIR`. Everything usable ⇒ the eight write tools too. **There is no state in which a write tool is registered but refuses for lack of configuration**, so jg-base#231's reader-only overlay runs this image as a reader without change. The `netdev-mcp:` lines on stderr cover:
  - the URL refusal, or `insecure http accepted (test only)`
  - controller host and site
  - reader, operator and ticket channel states
  - probes, `POD_IP` and snapshot dir
  - `write tools: registered — …` or `not registered — <every missing input>`
  - the route tables
  - `controller version: …` (the line daily-check keys on)
  - with writes: the probe baseline, and what class A read (own networks, controller, API server, attachment)
- **Ten reader tools**: `get_controller_info`, `get_sites`, `get_devices`, `get_clients`, `get_networks`, `get_firewall_rules` (each rule with `src_expanded`/`dst_expanded`: groups and networks resolved to addresses, `any` for an unconstrained side, `*_unresolved` naming ids the controller did not return), `get_port_forwards`, `get_dhcp_reservations` (users with `use_fixedip`; the cache keeps every user it read, since a reservation is set on one this view filters out), `get_wlans`, `get_lan_inventory` (devices and clients as one table; the nmap half is im's, in `app`). The first line of every answer is `[via integration]` or `[via legacy]`.
- **Eight write tools**, each `(…typed args, reason, confirm="")`:
  - `set_dhcp_reservation(mac, ip)` / `clear_dhcp_reservation(mac)`: PUT the user, changing only `use_fixedip`/`fixed_ip`.
  - `block_client(mac)` / `unblock_client(mac)`: `cmd/stamgr`; the revert is the inverse command.
  - `set_port_forward(name, proto, wan_port, lan_ip, lan_port)`: PUT when the name exists, else POST; a create is reverted by DELETE.
  - `delete_port_forward(id)`: the revert re-creates the object from the snapshot, under a new `_id`.
  - `restart_device(mac)` / `power_cycle_port(switch_mac, port)`: `cmd/devmgr`. **These cannot be undone**, so a failed health check ends in `revert-failed`, and the response says so.
  - Not shipped: `set_dns_forward`. Whether UniFi exposes conditional forwarding through an API is root spike 1.1's reading. No reading, no tool.
  - No tool exists for VLANs, the gateway, WLAN passphrases or deletion, WAN, firewall policy, site settings or accounts. A control asserts the tool list and argument names.
- **Which API serves what is data**: `ROUTES` (reads) and `WRITES`/`LOGIN_PATH` (writes, all legacy). Every value is **provisional**: UniFi's community-documented paths, not FAC1's reading (root spike 1.1, sub-change 1.2). If the reader key is refused on a legacy path, the read answers `error: … 401` naming the path, never an empty list.
- **The seven gates**, all server-side, none a human prompt. Gates ②, ③ and ⑦ are checked again at execute.
  1. **① A non-blank `reason`**, checked before any request is made. It is written on every audit line as `stated_reason`.
  2. **② The target comes from the session cache** (`_SEEN`, 600 s, stripped), found by `_id`, MAC or name. The PUT body is the **fresh unstripped object** from the snapshot, with only the tool's own fields changed. So secrets go back as they were, never as `<stripped>`, and the model never supplies JSON. A create needs its collection to have been read within the TTL.
  3. **③ Class A**, read fresh at every write: networks, clients, devices, firewall rules and groups from the reader; the API server from `KUBERNETES_SERVICE_HOST` plus `kubectl get endpoints kubernetes`; the controller address from the URL's host.
     - Refused: a target on im's own path (the node's own client, its switch or AP, a network or rule carrying pod↔controller or pod↔API server traffic).
     - Refused: a reservation handing out one of those addresses.
     - Refused: the gateway. In UniFi's notation, `ip_subnet` is `gateway/prefix`, so its host part is the gateway; the console's own address counts too.
     - **Any port of the node's switch is refused.** Integration clients name the uplink device but not the port, so this fails closed until spike 1.1 shows a port field.
     - Any input unreadable ⇒ every write refuses with the reader's own error.
  4. **④ Two phases.** Without `confirm`:
     - W1: all three outside probes must answer, or the write is refused with no ticket.
     - The ticket goes out. If it cannot be opened, the write is refused.
     - W3: wait up to `NETDEV_WITNESS_WAIT` for FAC's `pre-ack:`. If one arrives the witness is `armed`; if not it is `unmeasured`, which is a reading, not a gate.
     - Then a single-use token bound to tool, target and the `will_change` digest (`expires_in: 120`, `cooldown_after: 600`, ticket number).

     With `confirm`: the token must be known, issued for this tool and target, not expired, and for the same `will_change`. It is consumed before acting. A token that expires unused closes its ticket as `not-executed` **on its own timer** (`TOKEN_TTL` + 1 s, a daemon thread), not at the next call. FAC reads a deadline passing without a release as "escalate", which for a write that never ran would be a false page (fleet-ops-65 measured the earlier behaviour on k8scc#45). The ticket body states this rule.
  5. **⑤ Snapshot.** The whole collection is read once: the object plus every `_id`. It is written unstripped to `NETDEV_SNAPSHOT_DIR` (mode 0600, `O_EXCL`, newest `NETDEV_SNAPSHOT_KEEP` kept), and its sha256 goes on the audit line. Abort if any of these happen: the GET fails, the file cannot be written, or the object's own fields moved since the token. An abort closes the ticket `result: aborted`, and nothing is written. A write the controller itself refuses (an HTTP status, `rc != ok`) is also `aborted`. A transport error is treated as "maybe applied" and goes through health and revert.
  6. **⑥ Health and revert**, every `NETDEV_HEALTH_INTERVAL` until `NETDEV_HEALTH_TIMEOUT`.
     - The checks: controller `/info`, `kubectl get --raw /readyz`, read-back of **the tool's own fields** (not the whole object; `last_seen` and similar change on their own; there is none for a command), probes (≥2 of 3 lost is a failure, 1 lost is `outbound: degraded`), and FAC's `post-ack:` when armed (absent ⇒ `fac_witness: lost`).
     - **The first round where all pass ends it.** A breakage that shows only later is FAC's post-ack to catch.
     - On failure, revert from inside: PUT the snapshot; or DELETE the created `_id`, but **only when it is the sole new `_id` against the snapshot's list** (otherwise delete nothing and report `revert-failed`, naming every new id); or POST the deleted object back and read it back (an accepted POST that stored nothing is `revert-failed`, not `reverted` — fleet-ops-65, k8scc#45); or send the inverse command. Every revert except the inverse command reads back.
     - A revert that fails returns an explicit `inbox` instruction carrying the snapshot digest. The sidecar cannot write `~/workspace/inbox/`, so this depends on im, not on a mechanism (root D6).
  7. **⑦ Cooldown and ceiling.** Each target is refused for 600 s after its execute. `NETDEV_WRITES_PER_HOUR` confirmed writes are allowed in the trailing hour. Both live in memory, so a sidecar restart clears them; the audit still shows the lines close together. This is the same trade as `reboot_node`, and here there is no own-node hole: class A refuses whatever would cut the sidecar off.
- **The ticket (im→FAC channel)**: a GitHub issue on `IM_FAC_TICKET_REPO`, over plain REST with `httpx` (the base image has no `gh`).
  - Title: `[netdev] <CLUSTER_NAME> <tool> <target>`.
  - Body: cluster, target, collection and id, deadline, `stated_reason`, and the stripped `will_change`. Never a snapshot or a secret.
  - Release: a closing comment carrying `result`, `outbound` and `fac_witness`. `revert-failed` leaves the issue open.
  - **Every comment the server writes starts with `netdev-mcp `**, and `Tickets.comment` refuses any other text. Witness detection also skips this server's own comment ids. Every agent shares ferry133's GitHub identity, so the prefix is the only thing that tells a witness apart.
  - The minimal token scope is root spike 1.5, not yet measured.
- **Audit = stdout, and nothing else.** One JSON line per write phase: `token`, `execute` (written after health, carrying its `health` list), `refused`, `reverted`, `revert-failed`, `aborted`. Fields: `ts`, `tool`, `target`, `phase`, `stated_reason`, `reason` or `rc`, `user` (the sidecar's `CLAUDE_USER_ID`), `snapshot_digest`, `diff_digest`, `health`, `outbound`, `fac_witness`, `ticket`. ⚠️ **uvicorn's access log is a `StreamHandler` on `ext://sys.stdout`.** Measured 2026-10-26: run as a plain FastMCP app, two POSTs put two `INFO: … "POST /messages/…"` lines on stdout. `__main__` therefore keeps the real stdout for `_audit` and points `sys.stdout` at stderr before uvicorn configures logging. Measured afterwards: stdout 0 lines, both access lines on stderr. talos-mcp makes the same swap since k8scc#46. `test/test_mcp_stdout.py` runs both servers as real processes. As shipped, with the mutating tool registered, one MCP session calls it so that it is refused before acting; stdout must then be exactly that one `refused` audit line, and the access lines must be on stderr. As a positive control, the module's `app` run without `__main__` puts the access line on stdout. On 2026-10-26, four mutations each turned it red: the swap removed, or its two lines swapped (`_AUDIT_OUT` taken after `sys.stdout` already points at stderr), in each server. fleet-ops-65 found that the swapped-lines mutation passed an earlier version that only checked for an empty stdout (k8scc#48).
- **Secrets stripped at every exit but one**: `_strip` replaces the value of every key matching `^x_`, `_secret$`, `_key$`, `passphrase`, `password` or `psk` (case-insensitive, recursive) with `<stripped>`. It applies to reader responses, the cache, `will_change`, ticket bodies and audit lines. The snapshot keeps the values (controlled: exactly one occurrence in the file, and zero in every other exit).
- **httpx is not a new package**: `mcp==1.29.0` requires it (`pip show mcp`, 2026-10-26, spike 1.1 of the sub-change). httpx logs each request at INFO; the server sets that logger to WARNING.
- **Controls**: `test/test_netdev_mcp_server.py` imports the server for real against four fakes:
  - a controller answering from `test/netdev-mcp-fixtures/controller.json` (legacy login with cookie + CSRF, PUT/POST/DELETE/cmd, every request and body recorded, an `on_write` hook for "and then the probes drop", a `dead` switch);
  - FAC (GitHub issues; it can pre-ack, post-ack after the write, or stay silent);
  - three local TCP listeners as the probes;
  - a kubectl shim, `test/netdev-mcp-shims/kubectl`.

  **The fixtures are hand-written from UniFi's documented field names, not recorded from a console.** `u-tv.x_note` is invented: no write tool addresses a WLAN, so the secret the write exits must strip sits on a user row. The fakes bind without `getfqdn()`, because `HTTPServer.server_bind` does a reverse lookup that took 35 s on a macOS laptop. The 2026-10-26 mutations, each of which turned the suite red:
  - reader: strip only `x_` keys; accept `http://` without the flag; treat unresolved or negated rule sides as harmless.
  - writer, at d80108b: no snapshot (11 red); token not consumed (1); class A let through (2); probes all down but no revert (6); any comment counts as witness (1); the create revert deletes every new id (1). fleet-ops-65's acceptance review found four survivors or defects. After the fix, each turned red: the put revert never compares (1); the changed-since-token check is off (1); no expiry timer (1); the delete revert without read-back (1).

### Tool surface and controls

- **Tools**: `get_node_status`, `get_etcd_members`, `get_link_status`, `get_service_logs` (read-only; each shells out to the bundled `talosctl`), plus `reboot_node` while the operator credential is usable. No other mutating verb is reachable through any tool argument.
- **Controls**: `test/test_talos_mcp_server.py` imports the server for real against the Dockerfile's `mcp` pin (CI reads the pin from the Dockerfile rather than carrying a copy), with fake `talosctl`/`kubectl` from `test/talos-mcp-shims/` ahead on PATH (the fake `reboot` reports on stderr like the real one — a stdout fake kept k8scc#22 green). Every case carries a negative control, and `will_run` is compared to the argv the shim actually recorded, element by element. `test/claude-session-controls.sh` runs `claude-session` as shipped, with `HOME` redirected and fake `claude`/`tmux`. Both run in CI before anything is built. Three deliberate mutations (never fall back; token not consumed; own node waits) each turned the server controls red on 2026-10-13 — the first all-green run was checked that way, not taken on trust.

## Factory Variant (`factory-*` tags)

The `Dockerfile` builds **two** targets. Building it with no `--target` gives you the factory image, so both the workflow and any local build must name one.

| Target | Tags | Contents |
|--------|------|----------|
| `base` | `<short-sha>`, `latest` | What every existing consumer pins. Unchanged by the factory work. |
| `factory` | `factory-<short-sha>`, `factory-latest` | `base` + cluster-build toolchain + `omni-machine-watch` |

Same GHCR repository (`ghcr.io/ferry133/claude-code`), not a sibling package: a new package would be created private and bound to `k8scc`, so every consumer would need a pull secret and a visibility flip before it could pull anything. The tag prefix carries the distinction instead. Consumers pin short SHAs (not `latest`), so `factory-<sha>` can never collide with a tag someone is already pulling.

### Toolchain

Beyond `base`, the factory target adds `gh` `cloudflared` `age`/`age-keygen` `cue` `makejinja` `task` `helm` `helmfile` `talhelper` `flux` `kustomize` `kubeconform` `yq` `jq` `uv`, and re-pins `kubectl`/`talosctl` to the driven repo's versions rather than the hosting cluster's. `omnictl` lives in `base` since k8scc#18 (pinned to the fleet's self-hosted Omni server — v1.12.4 since k8scc#36, with jg-base#202 — with per-arch sha256 ARGs) and is asserted again in the factory's final layer; its version ARG is declared once before the first `FROM`, so the two stages cannot disagree. `sops` follows the same pattern since `im-apps-repo` (base installs it with per-arch sha256, factory only asserts it).

Two rules govern the pins:

- **They are a copy of the driven repo's `.mise.toml`, and copies drift.** Bump them together. A mismatch surfaces as a cue schema or makejinja rendering error partway through a client's cluster build, not as a clean startup failure.
- **`helm` `talhelper` `flux` `kustomize` `kubeconform` `yq` `jq` are not in the issue's list.** They are there because the named tools shell out to them — `task configure`/`bootstrap` targets in the driven repo reference `talhelper` 16×, `sops` 17×, `yq`/`kubeconform`/`flux` 8× each. Shipping `task` without them delivers a tool that cannot run.

`makejinja` cannot use the system `pip3`: it requires Python ≥ 3.12 and `debian:12` ships 3.11. It is installed with `uv` against a managed CPython matching the driven repo's own Python pin.

Every binary is made to **agree with the ARG that installed it** in the final build layer, per target platform under emulation. Printing a tool's version and failing only on a nonzero exit reports *"the install did not error"*, which reads identically to *"the requested version is present"* and is not the same claim — a check that cannot fail in the direction you care about is indistinguishable from one that passed.

Matching translates `.` to `_` on both sides before `grep -wF`, so dots count as word characters. Without that, `grep -w` treats `.` as a boundary and a truncated pin like `1.35` matches inside `1.35.2` — the build then passes on a version nobody asked for.

`test/assert-controls.sh` drives that function — **extracted from the Dockerfile, not copied**, so it cannot keep passing after the original changes — against outputs recorded from real builds *and* against pins that must be rejected (moved pin, truncated pin, nonzero exit, missing binary). It runs in CI before anything is built.

The confirmed versions are written to **`/usr/local/share/factory-toolchain.json`** inside the image. Anything downstream that needs to know what the build installed should read that rather than infer it from this Dockerfile or from an image label — it is the record of an execution *inside* the image. It is **not** a statement about what a running container will execute; see the ops variant's section below for why that distinction bites.

One exception, because it bites: **`makejinja --version` reports the wrong number.** `cli.py` uses `@click.version_option(None)`, whose auto-detection resolves to the wrong distribution and prints rich-click's version (`1.9.8`) for makejinja `2.8.2`. It is asserted from the installed distribution's metadata instead, both at install time and in the final layer. Do not "fix" a future version bump by trusting that flag.

`omni-machine-watch -version` reports the Omni client module it was **linked against**, read from the embedded build info, and the expected value is written by the builder stage from `go list -m` — so no copy of that version exists to drift.

### Short-SHA tags stay on one digest — a commit is never built twice

**Decision (ferry133, 2026-09-13, k8scc#3): keep the SLSA provenance attestation, and get digest stability by not rebuilding an already-published commit.** The workflow resolves `<short-sha>` and `factory-<short-sha>` against GHCR before building and skips the build when both already exist, so a given commit's tags are written exactly once.

So `<short-sha>` and `<short-sha>@sha256:…` mean the same thing permanently, and a digest a consumer pinned keeps its tag — which also ends the orphaning problem, because GHCR retention deletes *untagged* versions and there is no longer an untagged version to delete. `latest` and `factory-latest` still move on every build, by design; that is why consumers pin short SHAs.

**Every bump PR says why its digest changed** (k8scc#15). The digest always changes, so "bump im pin to <sha>" told a reader nothing — `3a23d9d` changed no image-definition file and still replaced 20 of 22 layers, because `debian:12-slim` had moved underneath it. `.github/scripts/bump_explain.py` writes three independent readings into the jg-base PR body: did the image definition change (`git diff` over the `Dockerfile` and every file the `base` stage COPYs, read from the Dockerfile), how many layers per platform were replaced (manifest against manifest), and did the base move (layer 0 against layer 0, and against upstream today). A reading it cannot take prints `not measured (...)`, never "no". All bumps share **one branch, `bump/claudecode`**: consecutive merges force-push it and rewrite the open PR's title and body instead of opening one PR each, so the PR's head moves under review — the body says so, and mergers pin `--match-head-commit`. **The branch only moves forward** (k8scc#31): before the force-push, `.github/scripts/bump_order.py` compares this build's commit, in k8scc's history, with jg-base main's pin and with the pin the branch already carries — it pushes only when newer than main *and* newer than the branch (or the branch is absent, which it is after every merge); older, same, or *could not tell* leaves it alone — because builds of consecutive merges run side by side and finish in any order.

**The escape hatch costs the invariant, deliberately loudly:** `workflow_dispatch` with `force_rebuild: true` rebuilds and moves the tags. Use it only to replace a bad publish, and expect any digest pinned to those tags to become untagged.

**Why not reproducible builds instead.** Three things move the digest on a rebuild, and the third cannot be configured away:

- `docker/metadata-action` stamps `org.opencontainers.image.created` (a fresh timestamp every run) and `org.opencontainers.image.version` (the primary tag, which differs between a branch build and a `main` build). That alone changes the config blob — **even when every layer is identical**.
- GitHub Actions cache is branch-scoped, so a `main` build cannot read a feature branch's cache. Measured 2026-08-18 on `170830f`: base layers hit `main`'s own cache and stayed byte-identical; the six factory layers were rebuilt and their digests changed.
- **The provenance attestations.** The published index holds four manifests — two platform images and two in-toto SLSA v1 attestations — and `tag@sha256:…` names the *index*, so anything that moves an attestation moves the pinned digest. Measured 2026-09-13 on `ca8865a`: the predicate carries `runDetails.metadata.startedOn` / `finishedOn` at nanosecond precision **and** the whole 9.5KB GitHub push event payload (`internalParameters.github_event_payload`, including `repository.updated_at`). Deterministic labels and `SOURCE_DATE_EPOCH` / `rewrite-timestamp` do not touch any of that.

**So the trade-off is real and this is the side that was chosen:** digest stability via reproducibility would require `provenance: false`, i.e. giving up the attestation. Not rebuilding gets the same stability and keeps it. Anyone tempted to "fix" this by normalising timestamps should read the third bullet first — that remedy would ship, pass review, and not work.

(k8scc is a public repo, so the event payload inside the attestation is publicly readable. Checked 2026-09-13: the addresses it carries are already in the repo's own git history, so it discloses nothing new.)

### `omni-machine-watch`

A long-lived Go process holding a COSI watch (`safe.StateWatchKind` on `MachineStatuses.omni.sidero.dev`) — `omnictl` stays for cluster-creation calls but cannot serve this, as `omnictl get --watch` prints a human table and dies with the command. Source in `omni-machine-watch/`, built against `github.com/siderolabs/omni/client`.

- **Output**: one JSON object per line on stdout (`created`/`updated`/`destroyed`/`bootstrapped`/`reconnect`/`stopped`), operational noise on stderr. `bootstrapped` marks the boundary between existing contents and live changes — that is what a "wait until the machine appears" caller keys on.
- **Lifetime is the caller's**: SIGINT/SIGTERM cancels the context, which tears down the subscription and exits 0.
- **Reconnects with backoff.** The example in the Omni source returns on `state.Errored`; a factory run outlives its watch being dropped by a restart or LB timeout, so this re-establishes and re-bootstraps instead of going silently quiet.
- **Not started by `entrypoint.sh`.** It needs an Omni credential and the terminal container must not hold one — the same split as the `talos-mcp` sidecar. Run it as its own container (`command: ["/usr/local/bin/omni-machine-watch"]`) or from a shell that already has the credential in its environment.
- **`go build` in `omni-machine-watch/` drops a ~100MB binary next to the source.** Nothing in the build needs it there — the builder stage writes to `/out` — and this repo carries no ignore rule for it (`.gitignore` is excluded globally on the author's machine). Build to a path outside the tree, or delete it before staging.
- **Testable without an Omni**: `stream()` takes a `state.CoreState`, so `main_test.go` drives it against an in-memory COSI state. This is not decoration — a live watch on an idle instance emits no changes, so "no `updated` events" there cannot distinguish a quiet cluster from a broken post-bootstrap stream. `go test` runs in the builder stage, so a regression fails the image build.

## Ops Variant (`ops-*` tags) — a third image, from `Dockerfile.ops`

Pre-baked tools for jg-base's four base workloads, which `apk add` on **every
execution** today (k8scc#11). On an appliance node measured at ~100 KB/s that
install was over 11 minutes while the work itself was seconds — and a backup
Job with `concurrencyPolicy: Forbid` that runs long enough makes the *previous
day's* archive read as today's.

**Image layers are cached by containerd; `apk add` is not.** Same bytes, paid
once per node instead of once per scheduling. That is the whole mechanism.

| Target | Tags | Base |
|--------|------|------|
| `ops` (in `Dockerfile.ops`) | `ops-<short-sha>`, `ops-latest` | `alpine:3.20` |

Same GHCR repository and the same reasoning as the factory variant: a sibling
package is created private and bound to `k8scc`, so every consumer would need a
pull secret and a visibility flip before it could pull anything. The tag prefix
carries the distinction. A clearer package name is available any time someone
is willing to do that flip — it is a one-time manual step, not a blocker worth
paying up front.

**A separate Dockerfile, not a third target in the main one.** The main file's
last stage is `factory`, and a build with no `--target` gets the last stage;
appending an alpine stage there would silently change what a targetless build
produces. That is the same trap the `target: base` comment in the workflow
exists for.

### Why one image and not two

Measured from Alpine's own APKINDEX (v3.20), summing the compressed download of
each dependency closure — the number that matters on a 100 KB/s link, because
it is what crosses the wire either way. **Re-measured 2026-09-22** after
`postgresql16-client` was dropped; the earlier figures are not carried over,
because a number quoted after its inputs changed has a citation and no
measurement behind it.

| set | packages | download | at 100 KB/s |
|---|---|---|---|
| backup, as its `apk add` line reads today | `bash age aws-cli kubectl postgresql16-client` | 53.4 MB | 8.9 min |
| backup, as `backup.sh` actually uses | `bash age aws-cli kubectl` | 51.5 MB | 8.6 min |
| daily-check | `bash curl jq msmtp ca-certificates bind-tools openssl coreutils aws-cli` | 39.9 MB | 6.6 min |
| lan-address | `kubectl` | 16.9 MB | 2.8 min |
| **union, as shipped here (no psql)** | | **59.3 MB** | **9.9 min** |

The union costs **7.8 MB more than `backup` alone already pulls**, because
`kubectl`, `aws-cli` and `python3` dominate and are shared. A second
kubectl-only image would save 42.4 MB once, on one node, and only when that
node runs none of the CronJobs — against a second tag to pin, bump and scan
forever. Hence one image. A narrower tag stays additive if a node profile ever
makes that 42.4 MB matter.

⚠️ **Note the direction.** Dropping psql moved that margin from +7.4 MB to
+7.8 MB — slightly *against* the one-image case, not for it. The conclusion
holds anyway; the number was re-read rather than assumed to have improved in
the convenient direction.

(8.6 min for the backup set is also an independent corroboration of the ">11
min" measured on the appliance: same order, different source, neither one an
estimate.)

### `postgresql16-client` is not in the image, and is dead in jg-base too

`backup.sh` never runs a database client locally. It builds `$cmd` as a
single-quoted string, and the only thing that executes it is
`kubectl -n "$ns" exec "deploy/${deploy}" -- sh -c "$cmd"` — so `pg_dump` runs
inside the database's own pod, at a version matching its server by
construction. The script says so three lines above the install: *"The dump runs
inside the database's own container, so nothing is installed here for it"*.
Command-position search for `pg_dump|psql|mariadb-dump|mysqldump`: zero hits,
and no branch falls back to a local dump.

(Found by `jgb-handler [20db54]` while verifying k8scc#11's coverage condition;
confirmed here against jg-base `5c83d74` before removal.)

It is 1.5 MB, so this is **not** a size fix. A package the image carries and
nothing uses is a claim that something uses it — and it would appear under
`packages` in `ops-toolchain.json`, where the next reader takes it as evidence
that dumps happen here. Same shape as everything else on this page, at 1.5 MB
rather than for free. jg-base's own `apk add` line still lists it; that
cleanup is jg-base's, tracked with `jg-base#124`.

### `kubectl` comes from upstream, not from `apk`

Alpine v3.20 ships kubectl **1.30.9**; jg-jiahd's API server measured
**v1.36.0** (2026-09-22). Six minors against a supported skew of one — so both
`apk add kubectl` sites are out of skew today and nothing says so. It is
pinned by `ARG KUBECTL_VERSION` to the same number the base image pins, and
**verified against `dl.k8s.io`'s published `.sha256`** rather than by matching
a version string: a printed version can agree with the ARG for reasons that
have nothing to do with the bytes.

This also removes daily-check's runtime `curl dl.k8s.io` — a second download on
the critical path that additionally hardcodes `linux/amd64`.

### What the image guarantees, and what it only claims

`/usr/local/share/ops-toolchain.json`, written by an execution *inside* the
image, per platform:

- `packages` — the apk-resolved version of every requested package. Checked
  against the installed set, because `apk add` can exit 0 while a name resolves
  to something else.
- `binaries.kubectl` — the pinned version, checksum-verified above.
- `commands` — **ran in this layer, on this platform, and printed something.**
- `present_only` — `host`, `nc`, `arping`: an executable file on PATH, *not*
  executed. `nc` needs a peer, `arping` needs `CAP_NET_RAW`, and bind-tools'
  `host` has no version flag. The two lists are named differently on purpose —
  a probe that quietly degrades into a presence check reads like a probe that
  passed.

**It is a record written by an execution, not an execution** — and the two stop
agreeing the moment anything downstream replaces a binary. A later layer, or a
volume mount over `/usr/local/bin`, leaves the JSON reporting the version that
was installed at build time, **and it still reads exactly like a reading**.
(`jgb-handler [20db54]`, 2026-09-22, while writing `jg-base#130` against it.)

So the two uses are different questions. A guard that asks "what is this
container about to run" must **call the binary** (`kubectl version --client`);
the JSON answers "what did the build intend", which is worth having and is not
the same thing. The two disagreeing is itself a finding worth reporting.

The same applies verbatim to `factory-toolchain.json` above.

A consumer that wants to assert what the image was *built* to carry should
read that file. The reverse guard — "jg-base's scripts call nothing the image
lacks" — cannot live here, because jg-base's scripts are not visible at build
time. It belongs in jg-base.

`test/assert-ops-controls.sh` drives `probe()` and `present()` — **extracted
from `Dockerfile.ops`, not copied**, so they cannot keep passing after the
originals change — against inputs that must be accepted and inputs that must be
rejected, including the easy-to-miss one: exit 0 with no output. It runs in CI
before anything is built. Writing it found a real hole: `grep -m1 .` accepts a
whitespace-only line, so a command that "ran and printed nothing" was passing.

### ⚠️ One image means `backup` silently changes `date`, `ls`, `wc`, `tr`

A consequence of the one-image decision that nobody listed when it was made,
and that no script will announce:

| | `date` / `ls` / `wc` / `tr` come from |
|---|---|
| backup's container today (`bash age aws-cli kubectl postgresql16-client`) | **busybox applets** |
| this image (carries `coreutils`, which daily-check needs) | **GNU coreutils** |

Measured, not inferred: `coreutils` is absent from the closure of backup's own
`apk add` line and present in this image's, and this image's own verify layer
prints `date (GNU coreutils) 9.5`. Alpine puts GNU at `/usr/bin` and busybox at
`/bin`, and `/usr/bin` comes first on PATH.

**Today there is no difference** — `jgb-handler [20db54]` checked every `date`
call site in `backup.sh` (2026-09-22): all four are `-u '+fmt'` or
`-d @<epoch>`, the spellings both implementations accept. **"Today there is no
difference" and "there will be no difference" are two statements.**

The direction matters. GNU accepts a superset, so nothing breaks on the way in
— but it means **a future GNU-only flag would work here and fail nowhere**,
until something runs the script in a plain alpine container again. That is the
same shape as a guard that only exists on the machine that tests it, inverted:
the permissive environment is the one doing the validating.

This is not hypothetical for this script. It died once on exactly this seam:
`date -d '30 days ago'` is GNU-only, busybox rejected it, and the fallback
turned "I cannot compute the cutoff" into "delete everything before today" —
jgt-appliance, 2026-08-16, the first two archives that system ever wrote were
pruned the day after it wrote them. The surviving comment in `backup.sh` says
`-d @<epoch>` is the one spelling both accept. **Keep it that way, and do not
let this image's tolerance be the reason a GNU-only spelling survives review.**

### Verifying that something is *gone* needs a stronger method, not a weaker one

Earned here on 2026-09-22 while confirming `postgresql16-client` had been
removed. The first check was `grep -ci postgresql` over the build log, expecting
0. It returned **6** — every hit a git commit message carried inside the SLSA
provenance payload, because the commit was titled `drop postgresql16-client`.
The grep was measuring whether a *word* appeared in a *narrative*, not whether
a package was installed.

**What caught it was the number coming back higher than the previous build's,
not the method.** Had it returned 0, a check that reads commit messages would
have been reported as confirmation of removal — and that report reads exactly
like a real one.

Generalised by `jgb-handler [20db54]`, who had the sentence this repo was
missing:

> **A check expecting 0 also returns 0 when the method is broken. A check
> expecting non-zero at least shows a crack when it breaks.**

So an absence is confirmed against **state**, never against narrative output:
here, the `packages` map in `/usr/local/share/ops-toolchain.json`, which the
image writes about itself. Logs, `kubectl` event streams and object references
are all narrative; reaching for the most convenient output and treating it as
authoritative is the same mistake in three costumes.

### The gate treats `ops-<sha>` separately, and that is load-bearing

The build gate tests base and factory as "both or neither". `ops-<sha>` is
gated on its own, because folding a third tag into that test would make every
commit published before `Dockerfile.ops` existed read as *half*-published — and
half-published rebuilds base and factory, **moving the very tags jg-base pins**
(k8scc#3). The absence of `ops-<sha>` must only ever cause the ops image to be
built. Verified by running the gate against `35f411b` before the first ops
build: `build=false`, `build-ops=true`.

## Runtime Configuration

| Env Var | Description |
|---------|-------------|
| `DATABASE_URL` | Optional PostgreSQL DSN; enables MCP memory server when set |
| `TTYD_INTERFACE` | Optional bind address for ttyd (e.g. `127.0.0.1` when an oauth2-proxy sidecar fronts it in the same netns) |
| `TTYD_AUTH_HEADER` | Optional trusted header ttyd requires (e.g. `X-Forwarded-Email` from oauth2-proxy); its value surfaces to `claude-session` as `TTYD_USER` |
| `CLAUDE_USER_ID` | Resolved in `claude-session` from `TTYD_USER` if present, else this var, else `claude_code`; used as `agent_id` in the memory DB |
| `TALOS_MCP_URL` | Optional; set only when the `talos-mcp` sidecar exists. Registers it as a remote MCP server (see above) |
| `IM_APPS_REPO` | Optional, `owner/repo`; empty is the same as unset. With the write key present at `IM_APPS_KEY`, `claude-session` configures the apps-repo ssh alias, git identity and `git`/`sops` permissions (see "Apps repo" above); without the key, one warning line and nothing written |
| `IM_APPS_KEY` | Optional path of the apps-repo write key; unset or empty means `~/.ssh/im-apps.key` |
| `HA_MCP_URL` | Optional; Home Assistant's MCP endpoint (`https://ha.<tenant>.<domain>/api/mcp`, through the tenant's public HTTPRoute — jgct#245; not reachable until jg-base#229 fixes the certificate); empty is the same as unset. With the token file present, `claude-session` registers `ha` and allows `mcp__ha` (see "Home Assistant MCP" above); without it, one warning line and nothing written |
| `HA_MCP_TOKEN_FILE` | Optional path of the HA long-lived token for the non-admin user `im`; unset or empty means `/etc/ha-mcp/token`. Read by python only, never through argv or env |
| `NETDEV_MCP_URL` | Optional; set only when the `netdev-mcp` sidecar exists (`http://127.0.0.1:8766/sse`); empty is the same as unset. Registers `netdev` as a remote SSE server without a header and allows `mcp__netdev` |
| `UNIFI_CONTROLLER_URL` (netdev-mcp only) | `https://<console>`; `http://` only with `NETDEV_ALLOW_INSECURE_HTTP=1` (the controls) |
| `UNIFI_SITE` (netdev-mcp only) | Legacy site name, default `default`; the Integration API site id is looked up from it (`internalReference`, then `name`) |
| `UNIFI_CA_FILE` (netdev-mcp only) | The console's certificate or CA; TLS is verified against it, with no off switch. Required for https |
| `UNIFI_READER_KEY_FILE` (netdev-mcp only) | File holding the Integration API key; absent, unreadable or empty ⇒ `not configured` |
| `NETDEV_READ_TIMEOUT` (netdev-mcp only) | Seconds per controller request, default 15 |
| `NETDEV_MCP_PORT` (netdev-mcp only) | Listen port on 127.0.0.1, default 8766 |
| `NETDEV_ALLOW_INSECURE_HTTP` (netdev-mcp only) | `1` accepts an `http://` controller and ticket API; for the fakes in the controls only, and said in the startup readings |
| `UNIFI_OPERATOR_FILE` (netdev-mcp only) | The local operator account, two lines (user, password); logged in once at startup. Absent, empty, malformed or refused ⇒ no write tools |
| `IM_FAC_TICKET_REPO` (netdev-mcp only) | `owner/repo` of the private im→FAC ticket repo; unset ⇒ no write tools |
| `IM_FAC_TICKET_TOKEN_FILE` (netdev-mcp only) | File holding the ticket token (issues read/write on that repo; minimal scope is root spike 1.5); missing or empty ⇒ no write tools |
| `IM_FAC_TICKET_API` (netdev-mcp only) | Default `https://api.github.com`; the controls point it at the fake FAC. Leave unset |
| `NETDEV_OUTSIDE_PROBES` (netdev-mcp only) | Exactly three, comma-separated: `https://host/path` (GET, 2xx/3xx) or `host:port` (TCP connect). Not three ⇒ no write tools |
| `POD_IP` (netdev-mcp only) | Downward API `status.podIP` (hostNetwork ⇒ the node's address); class A starts from it. Unset ⇒ no write tools |
| `NETDEV_SNAPSHOT_DIR` (netdev-mcp only) | Writable directory on a small PVC mounted on `netdev-mcp` only (snapshots keep secrets and must outlive a sidecar restart; root D5). Unset or not writable ⇒ no write tools |
| `NETDEV_SNAPSHOT_KEEP`, `NETDEV_HEALTH_TIMEOUT`, `NETDEV_HEALTH_INTERVAL`, `NETDEV_WRITES_PER_HOUR`, `NETDEV_WITNESS_WAIT`, `NETDEV_WITNESS_POLL`, `NETDEV_PROBE_TIMEOUT` (netdev-mcp only) | Defaults 50, 120 s, 10 s, 12, 120 s, 5 s, 5 s |
| `CLUSTER_NAME` (netdev-mcp only) | Ticket title and body; jg-base has it as a Flux substitution |
| `CLAUDE_USER_ID` (netdev-mcp only) | Recorded as `user` on audit lines; the sidecar's own env, like talos-mcp |
| `TALOSCONFIG` (talos-mcp only) | Path to the mounted talosconfig (`/etc/talos-mcp/talosconfig`). Routing metadata only — see `OMNI_SERVICE_ACCOUNT_KEY` |
| `OMNI_SERVICE_ACCOUNT_KEY` (talos-mcp only) | The Reader-role Omni SA's actual bearer credential; read straight from env by talosctl's auth library |
| `OMNI_ENDPOINT` (talos-mcp only) | Direct gRPC endpoint for the client's Omni. Must bypass any Cloudflare Tunnel (gRPC trailers) |
| `OMNI_ENDPOINT`, `OMNI_SERVICE_ACCOUNT_KEY` (omni-machine-watch) | Same pair as talos-mcp, same constraint on the endpoint being direct gRPC. The key is read from env only and deliberately has no flag — flags land in `ps` output |
| `TALOS_NODES` (talos-mcp only) | **Optional, unset by default.** Comma-separated node IPs; read-tool calls naming anything else are refused. A friendlier error, not a security boundary — the credential already resolves to one cluster server-side — and **not consulted by `reboot_node`**, whose allowlist comes from the API server at call time. Deliberately not wired into the jg-cluster-template pipeline: Omni clusters render `nodes: []`, so the list would be hand-written, and a stale entry blocks diagnostics exactly when they are needed |
| `TALOS_OPERATOR_CONFIG` (talos-mcp only) | **Optional.** Path to the direct-LAN `os:operator` talosconfig (`/etc/talos-mcp/operator-talosconfig`, mounted from the Secret key `operatorConfig`). Absent, 0 bytes, unreadable expiry or expired ⇒ reader-only behaviour, with the reason named in responses. Present and valid ⇒ read fallback over the direct LAN and `reboot_node` registered |
| `POD_IP` (talos-mcp only) | Downward API `status.podIP`. hostNetwork makes it the node's own address, so a confirmed `reboot_node` of that node is issued with `--wait=false` |
| `TALOS_MCP_READ_TIMEOUT` (talos-mcp only) | Seconds per talosctl read attempt, default 15; a timeout is a transport-class failure and triggers the direct-LAN retry |
| `CLAUDE_USER_ID` (talos-mcp only) | Recorded as `user` in `reboot_node` audit lines. The sidecar's own env — the ttyd session's identity never crosses from `app`. jg-base leaves it unset on this container, so the field is empty |

## Persistent Memory

Two layers of persistence:

| Layer | Mechanism | Storage | Notes |
|-------|-----------|---------|-------|
| Auto-memory | Claude Code built-in | PVC at `/home/claude/.claude` | Survives container rebuild; `.md` files |
| Explicit memory | MCP Memory Server | PostgreSQL `knowledge` table | Enabled when `DATABASE_URL` is set; `remember()` / `recall()` / `forget()` tools |

The MCP server (`memory_mcp_server.py`) uses a **dedicated PostgreSQL instance** (separate from linebot), keyed by `agent_id = CLAUDE_USER_ID`.
