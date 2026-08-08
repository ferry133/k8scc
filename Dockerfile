FROM debian:12-slim

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
RUN echo "set-option -g history-limit 5000" > /etc/tmux.conf

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
RUN useradd -m -u 1000 -s /bin/bash claude && \
    mkdir -p /home/claude/workspace /home/claude/.claude && \
    chown -R claude:claude /home/claude

# Install Claude Code as claude user (native installer → ~/.local/bin/claude)
USER claude
ENV PATH="/home/claude/.local/bin:${PATH}"
RUN curl -fsSL https://claude.ai/install.sh | bash

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
