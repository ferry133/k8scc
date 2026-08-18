# Two targets are built from this file (see .github/workflows/build.yaml):
#
#   --target base     ghcr.io/ferry133/claude-code:<sha>          (unchanged)
#   --target factory  ghcr.io/ferry133/claude-code:factory-<sha>
#
# `base` is the image existing consumers pin by short SHA; nothing may be
# added to it on the factory's behalf. `factory` is `base` plus the
# cluster-build toolchain and the Omni COSI watch, on its own tag, so a
# factory change can never reach a running cluster that pinned a base tag.
# Building with no --target would produce the factory image under the base's
# tags, which is why the workflow names both explicitly.
FROM debian:12-slim AS base

LABEL org.opencontainers.image.source="https://github.com/ferry133/k8scc"
LABEL org.opencontainers.image.description="Claude Code CLI with ttyd web terminal"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    bash \
    openssh-client \
    procps \
    ca-certificates \
    dbus \
    libsecret-1-0 \
    gnome-keyring \
    python3 python3-pip \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# tmux keeps `claude` alive across ttyd reconnects -- ttyd forks a brand-new
# child process per WebSocket connection with no session persistence of its
# own, so an idle-dropped connection (e.g. Envoy Gateway's default 5m stream
# idle timeout) would otherwise kill and restart `claude` from scratch on
# every reconnect (see claude-session). history-limit matches ttyd's
# client-side --client-option scrollback=5000 (entrypoint.sh) so reattaching
# restores the same amount of visible history.
#
# default-terminal MUST stay xterm-256color, not tmux's own default
# (tmux-256color): ttyd's pty already reports xterm-256color (matching
# xterm.js), and that's the TERM Claude Code's TUI renders correctly under.
# tmux otherwise overrides TERM to tmux-256color for the child process,
# which Claude Code's box-drawing/layout doesn't handle -- confirmed live
# 2026-08-08 (corrupted borders, missing box edges) against cc.jiahd.cc.
RUN printf '%s\n' \
      'set-option -g history-limit 5000' \
      'set-option -g default-terminal "xterm-256color"' \
      > /etc/tmux.conf

# Network diagnostics toolkit — for remote-supporting client networks
# (jg-cluster-template deployments) where CC needs to inventory LAN
# devices and debug router/DHCP/VPN config without the client having
# any networking background. Packet-level tools (nmap, masscan,
# arp-scan, tcpdump, fping) need CAP_NET_RAW, granted at the pod
# securityContext, not here.
# avahi-browse/iw/nmcli deliberately omitted: the first needs a
# privileged avahi-daemon + system D-Bus session we don't want to run
# in this non-root container; the latter two manage host network
# interfaces via NetworkManager, which Talos nodes don't run.
RUN apt-get update && apt-get install -y \
    nmap \
    fping \
    masscan \
    arp-scan \
    iproute2 \
    net-tools \
    tcpdump \
    dnsutils \
    nbtscan \
    snmp \
    mtr-tiny \
    traceroute \
    ipcalc \
    netcat-openbsd \
    socat \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Raw-socket tools need CAP_NET_RAW to actually work when run as the
# non-root `claude` user. Kubernetes' securityContext.capabilities.add
# only puts NET_RAW in the container's capability *bounding* set for a
# non-root process, not its effective/ambient set -- these file
# capabilities are what actually activate it, scoped to just these
# binaries rather than the whole shell. fping already gets this from
# its own Debian postinst; the rest don't.
RUN setcap cap_net_raw+ep /usr/bin/nmap && \
    setcap cap_net_raw+ep /usr/sbin/arp-scan && \
    setcap cap_net_raw+ep /usr/bin/tcpdump && \
    setcap cap_net_raw+ep /usr/bin/masscan

# kubectl -- in-cluster troubleshooting from the web terminal. The binary
# alone grants nothing; cluster access comes from RBAC bound to the pod's
# ServiceAccount at deploy time. Pinned to the cluster's minor version
# (kubectl skew policy allows ±1 minor against the API server).
ARG KUBECTL_VERSION=v1.36.0
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" && \
    chmod +x /usr/local/bin/kubectl

# talosctl -- backs the talos-mcp sidecar's read-only Talos diagnostics.
# The binary alone grants nothing; the sidecar mounts an os:reader-scoped
# talosconfig at deploy time (see openspec/changes/insideman in jg-base).
# Pinned to the cluster's Talos version.
ARG TALOSCTL_VERSION=v1.13.2
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL -o /usr/local/bin/talosctl \
      "https://github.com/siderolabs/talos/releases/download/${TALOSCTL_VERSION}/talosctl-linux-${ARCH}" && \
    chmod +x /usr/local/bin/talosctl

# Install MCP server deps. mcp pinned <2.0: the 2.0 release (2026-07)
# dropped mcp.server.fastmcp.FastMCP (both memory_mcp_server.py and
# talos_mcp_server.py use the FastMCP class) in favor of a new API this
# codebase hasn't migrated to yet.
RUN pip3 install --break-system-packages psycopg2-binary "mcp==1.29.0"

# Create D-Bus machine-id (required for session bus)
RUN mkdir -p /var/lib/dbus && dbus-uuidgen > /var/lib/dbus/machine-id

# Install ttyd (web terminal)
ARG TTYD_VERSION=1.7.7
RUN ARCH=$(dpkg --print-architecture) && \
    case "${ARCH}" in \
      amd64) TTYD_ARCH="x86_64" ;; \
      arm64) TTYD_ARCH="aarch64" ;; \
      *) echo "Unsupported architecture: ${ARCH}" && exit 1 ;; \
    esac && \
    wget -q -O /usr/local/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" && \
    chmod +x /usr/local/bin/ttyd

# Claude Code's OAuth login URL hard-wraps across terminal rows, so ttyd's
# stock client links only the first (truncated) line. Extract the embedded
# index.html from the ttyd binary and inject login-link-helper.js, which
# reassembles the full URL from the WebSocket stream and overlays a clickable
# sign-in button. Served via `ttyd --index` (see entrypoint.sh).
COPY login-link-helper.js patch-ttyd-index.py /usr/local/share/ttyd/
RUN python3 /usr/local/share/ttyd/patch-ttyd-index.py \
      /usr/local/bin/ttyd \
      /usr/local/share/ttyd/login-link-helper.js \
      /usr/local/share/ttyd/index.html && \
    grep -q login-link-helper /usr/local/share/ttyd/index.html

# Create non-root user and set correct ownership
#
# ~/.tmux.conf -> /etc/tmux.conf: the pod actually runs as uid 0 at deploy
# time (NAS export permissions -- see defaultPodOptions in the
# jg-cluster-template HelmRelease), but HOME is pinned to /home/claude via
# pod env regardless, so tmux looks for its per-user config here too.
# Symlinked rather than duplicated so the two settings above stay in sync.
RUN useradd -m -u 1000 -s /bin/bash claude && \
    mkdir -p /home/claude/workspace /home/claude/.claude && \
    ln -s /etc/tmux.conf /home/claude/.tmux.conf && \
    chown -R claude:claude /home/claude

# debian:12-slim leaves LANG unset, i.e. the C locale. tmux reads LC_ALL /
# LC_CTYPE / LANG to decide whether an attaching client can accept UTF-8, and
# with all three unset it flags the client non-UTF-8 and replaces every
# non-ASCII cell with "_" on its way out. Traditional Chinese — which the
# talos-mcp tool descriptions and claude-session's own banner are written in —
# therefore arrived at cc.jiahd.cc as rows of underscores (observed 2026-08-11,
# `tmux list-clients` on the live ttyd client read utf8=0). C.UTF-8 is present
# in this base image and needs no locales package.
ENV LANG=C.UTF-8

# Install Claude Code as claude user (native installer → ~/.local/bin/claude)
# Pinned like the other tools above. With no argument install.sh resolves
# "latest" at build time, which the GHA layer cache then freezes: two builds of
# the same commit could ship different CLIs, and a rebuild would keep serving
# the old one because this layer is a cache hit. An explicit version makes the
# build reproducible and busts the cache exactly when this ARG is bumped.
# install.sh takes [stable|latest|X.Y.Z] and passes it to `claude install`.
ARG CLAUDE_CODE_VERSION=2.1.228
USER claude
ENV PATH="/home/claude/.local/bin:${PATH}"
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "${CLAUDE_CODE_VERSION}"

USER root
COPY memory_mcp_server.py /usr/local/bin/memory_mcp_server.py
COPY talos_mcp_server.py /usr/local/bin/talos_mcp_server.py
COPY auth0_login.py /usr/local/bin/auth0_login.py
COPY claude-session /usr/local/bin/claude-session
RUN chmod +x /usr/local/bin/memory_mcp_server.py \
             /usr/local/bin/talos_mcp_server.py \
             /usr/local/bin/auth0_login.py \
             /usr/local/bin/claude-session

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER claude
WORKDIR /home/claude/workspace

EXPOSE 7681

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:7681/ || exit 1

ENTRYPOINT ["/entrypoint.sh"]

# ===========================================================================
# factory variant
# ===========================================================================
#
# base + the toolchain a cluster build shells out to + a long-lived Omni COSI
# watch. Everything below lands only on the `factory-*` tags; the `base`
# target above is untouched by it.

# The watch is cross-compiled from the build host rather than built under
# QEMU: the Omni client pulls in enough of Kubernetes and gRPC that an
# emulated arm64 compile costs minutes per build. Pinned to the same Go
# version github.com/siderolabs/omni/client@v1.10.3 requires (go >= 1.26.6),
# so the toolchain can't silently drift under the module.
FROM --platform=$BUILDPLATFORM golang:1.26.6-bookworm AS omni-watch-build

WORKDIR /src

COPY omni-machine-watch/go.mod omni-machine-watch/go.sum ./
RUN go mod download

COPY omni-machine-watch/ ./

# Run the tests here, natively, so a regression in the watch fails the image
# build instead of surfacing on a client's cluster. They drive an in-memory
# COSI state and need no network or Omni instance.
RUN go vet ./... && go test ./...

ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
      go build -trimpath -ldflags='-s -w' -o /out/omni-machine-watch . && \
    go list -m -f '{{.Version}}' github.com/siderolabs/omni/client > /out/omni-client-version

FROM base AS factory

LABEL org.opencontainers.image.description="Claude Code CLI with ttyd web terminal, cluster-build toolchain and an Omni COSI machine watch"

USER root

# Fail the build on a broken download instead of installing an empty file:
# the default /bin/sh (dash) has no pipefail, so `curl ... | tar` would
# succeed on a 404 body.
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# Toolchain pins.
#
# These must track the `.mise.toml` of the repo a factory run drives -- that
# file is the source of truth, and these are a copy of it. A drift shows up
# as a rendering or schema error partway through a client's cluster build,
# not as a clean failure at startup, so bump the two together.
#
# Pinned rather than resolved at build time for the same reason as
# CLAUDE_CODE_VERSION (8228f58): "latest" gets frozen into the GHA layer
# cache, so two builds of one commit can ship different tools and a rebuild
# keeps serving the stale one.
ARG OMNICTL_VERSION=v1.8.1
ARG GH_VERSION=2.87.3
ARG CLOUDFLARED_VERSION=2026.2.0
ARG AGE_VERSION=v1.3.1
ARG SOPS_VERSION=v3.12.1
ARG CUE_VERSION=v0.15.4
ARG TASK_VERSION=v3.48.0
ARG HELM_VERSION=v4.1.1
ARG HELMFILE_VERSION=1.3.2
ARG TALHELPER_VERSION=v3.1.16
ARG FLUX_VERSION=2.8.1
ARG KUSTOMIZE_VERSION=v5.7.1
ARG KUBECONFORM_VERSION=v0.7.0
ARG YQ_VERSION=v4.52.4
ARG JQ_VERSION=1.8.1
ARG UV_VERSION=0.10.7
ARG MAKEJINJA_VERSION=2.8.2

# kubectl and talosctl already exist in `base`, pinned to the cluster that
# hosts this pod. A factory run drives someone else's cluster, so these
# override them with the driven repo's pins. Both deltas are inside the
# supported skew either way (kubectl +/-1 minor; talosctl same minor), which
# is why the base pin was left alone rather than moved to match.
ARG FACTORY_KUBECTL_VERSION=v1.35.2
ARG FACTORY_TALOSCTL_VERSION=v1.13.8

# Single-file binaries.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL -o /usr/local/bin/omnictl \
      "https://github.com/siderolabs/omni/releases/download/${OMNICTL_VERSION}/omnictl-linux-${ARCH}" && \
    curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${ARCH}" && \
    curl -fsSL -o /usr/local/bin/sops \
      "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${ARCH}" && \
    curl -fsSL -o /usr/local/bin/yq \
      "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" && \
    curl -fsSL -o /usr/local/bin/jq \
      "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${ARCH}" && \
    curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${FACTORY_KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" && \
    curl -fsSL -o /usr/local/bin/talosctl \
      "https://github.com/siderolabs/talos/releases/download/${FACTORY_TALOSCTL_VERSION}/talosctl-linux-${ARCH}" && \
    chmod +x /usr/local/bin/omnictl /usr/local/bin/cloudflared /usr/local/bin/sops \
             /usr/local/bin/yq /usr/local/bin/jq /usr/local/bin/kubectl /usr/local/bin/talosctl

# Tarballs. Layouts differ per project, so each gets its own strip depth.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz --strip-components=2 -C /usr/local/bin "gh_${GH_VERSION}_linux_${ARCH}/bin/gh" && \
    curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${ARCH}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin age/age age/age-keygen && \
    curl -fsSL "https://github.com/cue-lang/cue/releases/download/${CUE_VERSION}/cue_${CUE_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin cue && \
    curl -fsSL "https://github.com/go-task/task/releases/download/${TASK_VERSION}/task_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin task && \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "linux-${ARCH}/helm" && \
    curl -fsSL "https://github.com/helmfile/helmfile/releases/download/v${HELMFILE_VERSION}/helmfile_${HELMFILE_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin helmfile && \
    curl -fsSL "https://github.com/budimanjojo/talhelper/releases/download/${TALHELPER_VERSION}/talhelper_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin talhelper && \
    curl -fsSL "https://github.com/fluxcd/flux2/releases/download/v${FLUX_VERSION}/flux_${FLUX_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin flux && \
    curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin kustomize && \
    curl -fsSL "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin kubeconform && \
    chmod +x /usr/local/bin/gh /usr/local/bin/age /usr/local/bin/age-keygen /usr/local/bin/cue \
             /usr/local/bin/task /usr/local/bin/helm /usr/local/bin/helmfile /usr/local/bin/talhelper \
             /usr/local/bin/flux /usr/local/bin/kustomize /usr/local/bin/kubeconform

# makejinja needs Python >= 3.12 and debian:12 ships 3.11, so it cannot go
# through the system pip3 the MCP servers use. uv installs it into its own
# venv against a managed CPython it downloads -- which is also the Python
# version the driven repo pins, so the renderer runs on the interpreter it
# was pinned against rather than on whatever the base image happens to have.
ARG UV_PYTHON_VERSION=3.14
RUN ARCH=$(dpkg --print-architecture) && \
    case "${ARCH}" in \
      amd64) UV_TRIPLE="x86_64-unknown-linux-gnu" ;; \
      arm64) UV_TRIPLE="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported architecture: ${ARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_TRIPLE}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "uv-${UV_TRIPLE}/uv" "uv-${UV_TRIPLE}/uvx" && \
    chmod +x /usr/local/bin/uv /usr/local/bin/uvx

# UV_* are set for this layer only: pointing a non-root runtime user's uv at
# /usr/local would just fail on write. The installed shims keep working
# because they reference these paths absolutely.
#
# The version is asserted here, from the installed distribution's own
# metadata, because `makejinja --version` cannot be trusted: its cli.py uses
# @click.version_option(None), whose auto-detection resolves to the wrong
# distribution and prints rich-click's version (1.9.8 against makejinja
# 2.8.2). A probe that reports the wrong number is worse than no probe --
# this one fails the build instead.
RUN UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_TOOL_DIR=/usr/local/share/uv/tools \
    UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python \
    uv tool install --python "${UV_PYTHON_VERSION}" "makejinja==${MAKEJINJA_VERSION}" && \
    chmod -R a+rX /usr/local/share/uv && \
    installed=$(/usr/local/share/uv/tools/makejinja/bin/python \
      -c 'from importlib.metadata import version; print(version("makejinja"))') && \
    echo "makejinja installed: ${installed} (expected ${MAKEJINJA_VERSION})" && \
    [ "${installed}" = "${MAKEJINJA_VERSION}" ]

# Long-lived COSI watch on MachineStatuses.omni.sidero.dev. Not started by
# entrypoint.sh: it needs an Omni credential, and the terminal container must
# not hold one (same split as the talos-mcp sidecar). Run it as its own
# container with `command: ["/usr/local/bin/omni-machine-watch"]`, or from a
# shell that has OMNI_ENDPOINT/OMNI_SERVICE_ACCOUNT_KEY in its environment.
COPY --from=omni-watch-build /out/omni-machine-watch /usr/local/bin/omni-machine-watch

# Every added binary is made to AGREE with the ARG that installed it, not
# merely to run. Printing whatever a tool says and failing only on a nonzero
# exit reports "the install command did not error" -- which reads identically
# to "the requested version is present" and is not the same claim. A check
# that cannot fail in the direction you care about is indistinguishable from
# one that passed.
#
# Each expected string below was checked against that tool's real output in
# an earlier build of this file, so a mismatch means the pin moved, not that
# the probe is guessing. Matching is word-boundaried (grep -wF, with and
# without a leading v). Dots are translated to underscores on both sides
# first, so that they count as word characters: without that, grep -w treats
# "." as a boundary and a truncated pin like 1.35 matches inside 1.35.2 --
# a check that passes on a version nobody asked for.
#
# Runs once per target platform under emulation, so it also proves the arm64
# binaries execute.
#
# makejinja is the one documented exception -- see the install layer above.
COPY --from=omni-watch-build /out/omni-client-version /usr/local/share/omni-client-version

RUN manifest=/usr/local/share/factory-toolchain.json; \
    records=$(mktemp); \
    assert() { \
      name="$1"; want="${2#v}"; shift 2; \
      rc=0; out=$("$@" 2>&1) || rc=$?; \
      if [ "$rc" -ne 0 ]; then \
        printf '%-18s FAILED (exit %s): %s\n' "$name" "$rc" "$out"; \
        return 1; \
      fi; \
      line=$(printf '%s\n' "$out" | grep -m1 . || true); \
      u=$(printf '%s' "$out" | tr . _); w=$(printf '%s' "$want" | tr . _); \
      if printf '%s' "$u" | grep -qwF "$w" || printf '%s' "$u" | grep -qwF "v$w"; then \
        printf '%-18s %s\n' "$name" "$line"; \
        printf '%s\t%s\n' "$name" "$want" >> "$records"; \
      else \
        printf '%-18s FAILED: expected %s, got: %s\n' "$name" "$want" "$line"; \
        return 1; \
      fi; \
    }; \
    smoke() { \
      name="$1"; shift; \
      rc=0; out=$("$@" 2>&1) || rc=$?; \
      if [ "$rc" -ne 0 ]; then \
        printf '%-18s FAILED (exit %s): %s\n' "$name" "$rc" "$out"; \
        return 1; \
      fi; \
      printf '%-18s %s\n' "$name" "$(printf '%s\n' "$out" | grep -m1 . || true)"; \
    }; \
    assert omnictl     "${OMNICTL_VERSION}"           omnictl --version && \
    assert gh          "${GH_VERSION}"                gh --version && \
    assert cloudflared "${CLOUDFLARED_VERSION}"       cloudflared --version && \
    assert age         "${AGE_VERSION}"               age --version && \
    assert age-keygen  "${AGE_VERSION}"               age-keygen --version && \
    assert sops        "${SOPS_VERSION}"              bash -c 'sops --version --disable-version-check || sops --version' && \
    assert cue         "${CUE_VERSION}"               cue version && \
    assert task        "${TASK_VERSION}"              task --version && \
    assert helm        "${HELM_VERSION}"              helm version --short && \
    assert helmfile    "${HELMFILE_VERSION}"          helmfile version && \
    assert talhelper   "${TALHELPER_VERSION}"         talhelper --version && \
    assert flux        "${FLUX_VERSION}"              flux --version && \
    assert kustomize   "${KUSTOMIZE_VERSION}"         kustomize version && \
    assert kubeconform "${KUBECONFORM_VERSION}"       kubeconform -v && \
    assert yq          "${YQ_VERSION}"                yq --version && \
    assert jq          "${JQ_VERSION}"                jq --version && \
    assert uv          "${UV_VERSION}"                uv --version && \
    assert kubectl     "${FACTORY_KUBECTL_VERSION}"   kubectl version --client=true && \
    assert talosctl    "${FACTORY_TALOSCTL_VERSION}"  bash -c 'talosctl version --client | tr "\n" " "' && \
    assert omni-client "$(cat /usr/local/share/omni-client-version)" omni-machine-watch -version && \
    assert makejinja   "${MAKEJINJA_VERSION}" \
      /usr/local/share/uv/tools/makejinja/bin/python \
        -c 'from importlib.metadata import version; print(version("makejinja"))' && \
    smoke makejinja-cli bash -c 'makejinja --help >/dev/null && echo "entry point ok"' && \
    smoke watch-guard \
      bash -c 'out=$(omni-machine-watch 2>&1 || true); \
               case "$out" in *"no endpoint"*|*OMNI_SERVICE_ACCOUNT_KEY*) printf %s "$out" ;; \
                              *) printf "unexpected: %s" "$out"; exit 1 ;; esac' && \
    python3 -c 'import json,sys; print(json.dumps(dict(l.split(chr(9)) for l in sys.stdin.read().splitlines()), indent=2, sort_keys=True))' \
      < "$records" > "$manifest" && \
    rm -f "$records" && \
    chmod 0444 "$manifest" && \
    echo "--- $manifest ---" && cat "$manifest"

SHELL ["/bin/sh", "-c"]

USER claude
