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
    && rm -rf /var/lib/apt/lists/*

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
    && rm -rf /var/lib/apt/lists/*

# Install MCP server deps
RUN pip3 install --break-system-packages psycopg2-binary mcp

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
COPY auth0_login.py /usr/local/bin/auth0_login.py
COPY claude-session /usr/local/bin/claude-session
RUN chmod +x /usr/local/bin/memory_mcp_server.py \
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
