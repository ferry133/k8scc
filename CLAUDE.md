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

Every binary is made to **agree with the ARG that installed it** in the final build layer, per target platform under emulation. Printing a tool's version and failing only on a nonzero exit reports *"the install did not error"*, which reads identically to *"the requested version is present"* and is not the same claim — a check that cannot fail in the direction you care about is indistinguishable from one that passed.

Matching translates `.` to `_` on both sides before `grep -wF`, so dots count as word characters. Without that, `grep -w` treats `.` as a boundary and a truncated pin like `1.35` matches inside `1.35.2` — the build then passes on a version nobody asked for.

`test/assert-controls.sh` drives that function — **extracted from the Dockerfile, not copied**, so it cannot keep passing after the original changes — against outputs recorded from real builds *and* against pins that must be rejected (moved pin, truncated pin, nonzero exit, missing binary). It runs in CI before anything is built.

The confirmed versions are written to **`/usr/local/share/factory-toolchain.json`** inside the image. Anything downstream that needs to know what the build installed should read that rather than infer it from this Dockerfile or from an image label — it is the record of an execution *inside* the image. It is **not** a statement about what a running container will execute; see the ops variant's section below for why that distinction bites.

One exception, because it bites: **`makejinja --version` reports the wrong number.** `cli.py` uses `@click.version_option(None)`, whose auto-detection resolves to the wrong distribution and prints rich-click's version (`1.9.8`) for makejinja `2.8.2`. It is asserted from the installed distribution's metadata instead, both at install time and in the final layer. Do not "fix" a future version bump by trusting that flag.

`omni-machine-watch -version` reports the Omni client module it was **linked against**, read from the embedded build info, and the expected value is written by the builder stage from `go list -m` — so no copy of that version exists to drift.

### Short-SHA tags stay on one digest — a commit is never built twice

**Decision (ferry133, 2026-09-13, k8scc#3): keep the SLSA provenance attestation, and get digest stability by not rebuilding an already-published commit.** The workflow resolves `<short-sha>` and `factory-<short-sha>` against GHCR before building and skips the build when both already exist, so a given commit's tags are written exactly once.

So `<short-sha>` and `<short-sha>@sha256:…` mean the same thing permanently, and a digest a consumer pinned keeps its tag — which also ends the orphaning problem, because GHCR retention deletes *untagged* versions and there is no longer an untagged version to delete. `latest` and `factory-latest` still move on every build, by design; that is why consumers pin short SHAs.

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
