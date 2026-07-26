#!/bin/bash
set -e

# Start D-Bus session daemon (required for Claude Code credential storage)
if command -v dbus-daemon &>/dev/null && [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    DBUS_ADDR=$(dbus-daemon --session --fork --print-address 2>/dev/null)
    if [ -n "$DBUS_ADDR" ]; then
        export DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR"
    fi
fi

# Start GNOME Keyring daemon (provides secret-service for Claude Code OAuth tokens)
if command -v gnome-keyring-daemon &>/dev/null; then
    eval $(printf '' | gnome-keyring-daemon --daemonize --unlock --components=secrets 2>/dev/null) || true
    export GNOME_KEYRING_CONTROL SSH_AUTH_SOCK 2>/dev/null || true
fi

TTYD_ARGS=(
    "--port" "7681"
    "--writable"
    "--max-clients" "5"
    "--client-option" "copyOnSelect=false"
    "--client-option" "cursorBlink=true"
    "--client-option" "cursorStyle=block"
    "--client-option" "fontSize=15"
    "--client-option" "scrollback=5000"
)

# Patched client page: makes the wrapped Claude OAuth login URL clickable
# (built by patch-ttyd-index.py in the Dockerfile)
if [ -f /usr/local/share/ttyd/index.html ]; then
    TTYD_ARGS+=("--index" "/usr/local/share/ttyd/index.html")
fi

# Enable basic auth if credentials are provided
if [ -n "${TTYD_CREDENTIAL}" ]; then
    TTYD_ARGS+=("--credential" "${TTYD_CREDENTIAL}")
fi

# Bind to a specific address (e.g. "127.0.0.1" when an auth proxy fronts ttyd
# in the same network namespace -- keeps :7681 off the LAN on hostNetwork
# pods). Prefer an IP over an interface name: ttyd resolves a name like "lo"
# to its first non-127 address, which on Talos is a 169.254.x.x on lo.
if [ -n "${TTYD_INTERFACE}" ]; then
    TTYD_ARGS+=("--interface" "${TTYD_INTERFACE}")
fi

# Require + trust a reverse-proxy auth header (e.g. X-Forwarded-Email from
# oauth2-proxy). ttyd rejects requests missing it and exposes its value to
# the session as TTYD_USER (not as an HTTP_* CGI-style var).
if [ -n "${TTYD_AUTH_HEADER}" ]; then
    TTYD_ARGS+=("--auth-header" "${TTYD_AUTH_HEADER}")
fi

exec ttyd "${TTYD_ARGS[@]}" /usr/local/bin/claude-session
