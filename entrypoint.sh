#!/bin/bash
set -e

# Initialize persistent config directory on first run
mkdir -p /home/claude/.claude
if [ ! -f /home/claude/.claude/settings.json ]; then
    echo '{"env":{"DISABLE_AUTOUPDATER":"1"}}' > /home/claude/.claude/settings.json
fi

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

# Enable basic auth if credentials are provided
if [ -n "${TTYD_CREDENTIAL}" ]; then
    TTYD_ARGS+=("--credential" "${TTYD_CREDENTIAL}")
fi

exec ttyd "${TTYD_ARGS[@]}" /bin/bash
