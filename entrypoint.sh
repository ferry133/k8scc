#!/bin/bash
set -e

# Initialize persistent config directory on first run
mkdir -p /home/claude/.claude
if [ ! -f /home/claude/.claude/settings.json ]; then
    echo '{"env":{"DISABLE_AUTOUPDATER":"1"}}' > /home/claude/.claude/settings.json
fi

# Auth0 Device Flow: get user identity before starting the session
CLAUDE_USER_ID="claude_code"
if [ -n "${AUTH0_DOMAIN}" ] && [ -n "${AUTH0_CLIENT_ID}" ]; then
    _uid=$(python3 /usr/local/bin/auth0_login.py)
    if [ $? -eq 0 ] && [ -n "$_uid" ]; then
        CLAUDE_USER_ID="$_uid"
    else
        echo "Auth0 登入失敗，以匿名模式繼續" >&2
    fi
fi
export CLAUDE_USER_ID

# Inject MCP memory server into settings.json if DATABASE_URL is configured
if [ -n "${DATABASE_URL}" ]; then
    python3 - <<'PYEOF'
import json, os
p = "/home/claude/.claude/settings.json"
with open(p) as f:
    s = json.load(f)
s.setdefault("mcpServers", {})["memory"] = {
    "command": "python3",
    "args": ["/usr/local/bin/memory_mcp_server.py"],
    "env": {
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "CLAUDE_USER_ID": os.environ.get("CLAUDE_USER_ID", "claude_code"),
    },
}
with open(p, "w") as f:
    json.dump(s, f, indent=2)
PYEOF
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
