#!/bin/bash
set -e

# --- secret-service startup (k8scc#19) -----------------------------------------
# D-Bus session bus + GNOME Keyring, for Claude Code's OAuth token storage.
# Every outcome prints one `entrypoint:` line to the pod log, because "came up"
# and "never came up" used to be byte-identical there (stderr to /dev/null,
# `|| true`, and an `eval` of nothing). No outcome stops the container: a
# terminal without a keyring still works -- Claude Code then keeps its token in
# ~/.claude/.credentials.json -- so this is a record, not a new way to die.
# test/entrypoint-controls.sh extracts this function and drives it with fake
# daemons; keep the markers.
start_secret_service() {
    local err rc out
    err=$(mktemp)

    if [ -n "${DBUS_SESSION_BUS_ADDRESS:+set}" ]; then
        echo "entrypoint: dbus: session bus already set in the environment, not starting one" >&2
    elif ! command -v dbus-daemon >/dev/null 2>&1; then
        echo "entrypoint: dbus: dbus-daemon not installed, no session bus" >&2
    else
        out=$(dbus-daemon --session --fork --print-address 2>"$err") && rc=0 || rc=$?
        if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
            export DBUS_SESSION_BUS_ADDRESS="$out"
            echo "entrypoint: dbus: session bus up at $out" >&2
        else
            echo "entrypoint: dbus: session bus NOT started (exit $rc, $([ -n "$out" ] && echo "printed an address" || echo "printed no address"); stderr: $(head -c 300 "$err" | tr '\n' ' '))" >&2
        fi
    fi

    if ! command -v gnome-keyring-daemon >/dev/null 2>&1; then
        echo "entrypoint: keyring: gnome-keyring-daemon not installed" >&2
    else
        out=$(printf '' | gnome-keyring-daemon --daemonize --unlock --components=secrets 2>"$err") && rc=0 || rc=$?
        if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
            # The daemon prints NAME=value lines meant for eval.
            eval "$out"
            [ -n "${GNOME_KEYRING_CONTROL+x}" ] && export GNOME_KEYRING_CONTROL
            [ -n "${SSH_AUTH_SOCK+x}" ] && export SSH_AUTH_SOCK
            echo "entrypoint: keyring: secrets component up (GNOME_KEYRING_CONTROL=${GNOME_KEYRING_CONTROL:-<not set by the daemon>})" >&2
            [ -n "${DBUS_SESSION_BUS_ADDRESS:+set}" ] || \
                echo "entrypoint: keyring: up, but there is no session bus, so secret-service cannot be reached" >&2
        else
            echo "entrypoint: keyring: NOT started (exit $rc, $([ -n "$out" ] && echo "printed output" || echo "printed nothing"); stderr: $(head -c 300 "$err" | tr '\n' ' '))" >&2
        fi
    fi

    rm -f "$err"
    return 0
}
# --- end secret-service startup -------------------------------------------------
start_secret_service

TTYD_ARGS=(
    "--port" "7681"
    "--writable"
    "--max-clients" "5"
    "--client-option" "copyOnSelect=false"
    "--client-option" "cursorBlink=true"
    "--client-option" "cursorStyle=block"
    "--client-option" "fontSize=15"
    "--client-option" "scrollback=10000"
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
