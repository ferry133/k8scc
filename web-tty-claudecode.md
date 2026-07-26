# Web TTY Claude Code — Design & User Guide

## Overview

A browser-accessible Claude Code CLI terminal, secured by Auth0 OAuth2 login and backed by per-user persistent memory in PostgreSQL.

```
Browser → oauth2-proxy (Auth0 OIDC) → ttyd → bash → claude
```

---

## Architecture

### Components

| Component | Role |
|-----------|------|
| **ttyd** | Serves a web terminal over WebSocket on port 7681 |
| **oauth2-proxy** | OIDC reverse proxy on port 4180; gates access with Auth0 login |
| **claude-session** | Session startup script; sets up MCP memory and launches Claude Code |
| **memory_mcp_server.py** | MCP server providing `remember` / `recall` / `forget` tools backed by PostgreSQL |

### Request Flow

```
1. User opens https://cc.<domain>
2. oauth2-proxy (sidecar, :4180) checks for valid session cookie
   └─ No cookie → redirect to Auth0 login page
   └─ Auth0 (Google / GitHub / etc.) → callback → oauth2-proxy issues cookie
   └─ Email must be in the allowlist file (claudecode_allowed_emails)
3. oauth2-proxy forwards request to ttyd (127.0.0.1:7681)
   └─ Injects X-Forwarded-Email: user@gmail.com header
4. ttyd (run with --auth-header X-Forwarded-Email) requires that header
   └─ rejects requests without it; exposes its value to the session as
      TTYD_USER (ttyd does NOT do CGI-style HTTP_* header mapping)
5. claude-session sets CLAUDE_USER_ID from TTYD_USER
6. MCP memory server uses CLAUDE_USER_ID as agent_id in PostgreSQL
7. Claude Code launches with per-user memory namespace
```

> OIDC mode is opt-in per deployment: setting `TTYD_INTERFACE=127.0.0.1` +
> `TTYD_AUTH_HEADER=X-Forwarded-Email` (and fronting with oauth2-proxy)
> activates it; leaving them unset keeps plain ttyd with optional
> `TTYD_CREDENTIAL` basic auth. `TTYD_INTERFACE` must be an IP, not an
> interface name — ttyd resolves a name like `lo` to its first non-127
> address (on Talos, a 169.254.x.x).

### Account Layers

| Layer | Account | Notes |
|-------|---------|-------|
| **Claude Code license** | `jiahdadm@gmail.com` (Anthropic account) | All API usage billed here; one subscription shared |
| **Terminal access control** | Any Auth0-allowed Google / GitHub account | oauth2-proxy enforces login |
| **Memory isolation** | Each user's login email (e.g. `alice@gmail.com`) | Separate rows in PostgreSQL `knowledge` table |

Different Google accounts logging in get completely separate MCP memories. They share the same Claude Code license and the same pod, but their `remember` / `recall` data never mixes.

### Memory Persistence

| Type | Storage | Scope | Notes |
|------|---------|-------|-------|
| **MCP explicit memory** | PostgreSQL `knowledge` table | Per `CLAUDE_USER_ID` | `remember()` / `recall()` / `forget()` tools |
| **Claude auto-memory** | PVC at `/home/claude/.claude` | Shared across all users on this pod | `.md` files written by Claude Code automatically |
| **Workspace files** | PVC at `/home/claude/workspace` | Shared | Persists across container restarts |
| **Coding mount** | NFS `/home/claude/coding` | Shared (read-write) | NAS volume |

> **Note**: Auto-memory (`.claude/*.md`) is currently shared across all users because there is one pod with one PVC. Only MCP explicit memory is isolated per user email.

### Kubernetes Deployment

Deployed per cluster as the `claudecode/claude-code` extra (manifests in
`jg-base`, templated via `jg-cluster-template`, values in each cluster's
`cluster.yaml`). Reference deployment: jg-jiahd, `cc.jiahd.cc`.

- **Namespace**: `claudecode`
- **HelmRelease**: one per entry in `claude_instances` (single replica, `Recreate`, hostNetwork)
- **Image**: `ghcr.io/ferry133/claude-code:<git-short-sha>` (pinned, not `:latest`)
- **Route**: `<instance>.<domain>` via Envoy Gateway → oauth2-proxy `:4180` (OIDC mode) or ttyd `:7681` (basic-auth mode)
- **Runs as root** (`runAsUser: 0`): the NAS NFS coding export allows root only; `HOME=/home/claude` pinned via env
- **kubectl + RBAC**: image ships kubectl; the pod uses the shared SA `claude-code` bound to **cluster-admin** (jg-base `rbac.yaml`) — in-pod kubectl hits the in-cluster API endpoint directly, independent of Omni (this is the cluster's rescue path when Omni is down)
- **Login persistence**: `CLAUDE_CONFIG_DIR=/home/claude/.claude` keeps Claude onboarding/credentials on the PVC — sign in once, survives pod restarts and image updates
- **Secrets**: `claude-code-secret` — `TTYD_CREDENTIAL`, `DATABASE_URL`, `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`, `OAUTH2_PROXY_COOKIE_SECRET`, `ALLOWED_EMAILS` (newline-separated, rendered from `claudecode_allowed_emails`)
- **Storage**:
  - `claude-config` PVC — 5Gi (sc-nas) → `/home/claude/.claude` (+ keyring dir via subPath)
  - `claude-workspace` PVC — 20Gi (sc-nas) → `/home/claude/workspace`
  - `coding` NFS — `${NAS_SERVER}:${NAS_CODING_PATH}` → `/home/claude/coding`

### Auth0 Configuration

- **Application type**: Regular Web Application (an existing app can be shared across sites)
- **Allowed Callback URLs**: `https://<instance>.<domain>/oauth2/callback`
- **Allowed Logout URLs**: `https://<instance>.<domain>`
- **Allowed Web Origins**: `https://<instance>.<domain>`
- **Connections**: Google, GitHub (or any configured social login)

Access is restricted by the `claudecode_allowed_emails` allowlist enforced
by oauth2-proxy (`--authenticated-emails-file`) — no Auth0 Rules/Actions
needed. An account that passes Auth0 but is not in the allowlist gets 403.

---

## User Guide

### Logging In

1. Open **https://cc.<domain>** (e.g. `cc.jiahd.cc`) in your browser
2. You will be redirected to Auth0 login — choose your Google (or GitHub) account
3. After successful login, the terminal opens automatically
4. A welcome message confirms your identity: `✓ 歡迎，your@email.com`
5. Claude Code launches immediately — no extra steps needed

> If you are already signed into the same browser session, login is automatic (SSO — no interaction required). To verify who is currently logged in, open **https://cc.<domain>/oauth2/userinfo** in the same browser.

### Claude Account Sign-in (first use)

On first launch Claude Code asks you to sign in to your Claude account and shows a long OAuth URL. The URL wraps across several terminal lines, so the in-terminal link is unreliable — instead, an orange **「🔗 開啟 Claude 登入頁 / Open sign-in page」** button appears at the top-right of the page:

1. Click it — the sign-in page opens in a new tab; approve access there.
2. On the confirmation page, click **Copy code**.
3. Switch back to the terminal tab — the code is detected in your clipboard and submitted automatically (your browser may ask once for clipboard permission; if it was denied, click **「📋 貼上驗證碼 / Paste code」** instead).

The buttons disappear once you are signed in. (They are added by a patched ttyd client page that reassembles the wrapped URL; the clipboard is only read while a sign-in is pending, and only a code matching this exact sign-in attempt is ever accepted.)

### Using the Terminal

The terminal session runs as root (see Kubernetes Deployment) with `HOME=/home/claude`. Claude Code is launched automatically on login; `kubectl` is available with cluster-admin.

Common operations:

```bash
# Check current user identity
echo $CLAUDE_USER_ID

# Navigate workspace
cd ~/workspace

# Access shared coding files
cd ~/coding

# Exit Claude Code back to shell
/exit     # or Ctrl+C
```

### MCP Memory Tools

When `DATABASE_URL` is configured, Claude Code has access to three memory tools:

| Tool | Description |
|------|-------------|
| `remember("fact")` | Store a fact in your personal memory |
| `recall("topic")` | Search your memory for relevant facts |
| `forget("fact")` | Delete a fact from your memory |

Memory is scoped to your login email — other users cannot see or modify your memories.

Example usage inside Claude Code:
```
remember("Our Kubernetes cluster runs on Talos Linux with Flux GitOps")
recall("kubernetes")
forget("outdated fact")
```

### Signing Out

To sign out and invalidate your session:

- Open **https://cc.<domain>/oauth2/sign_out** in your browser

This clears the oauth2-proxy session cookie. The next visit will require Auth0 login again.

### Workspace Persistence

| Location | Persists across restarts? | Notes |
|----------|--------------------------|-------|
| `~/workspace/` | ✓ Yes | PVC on NAS |
| `~/.claude/` | ✓ Yes | Claude config + auto-memory PVC |
| `~/coding/` | ✓ Yes | NFS mount |
| MCP memory | ✓ Yes | PostgreSQL |
| In-progress terminal state | ✗ No | Lost on pod restart |

---

## Operations

### Rebuilding the Image

Push to `main` branch of `ferry133/k8scc` — GitHub Actions builds and pushes `ghcr.io/ferry133/claude-code:<git-short-sha>` and `:latest` automatically (multi-arch: amd64 + arm64). Deployments pin the short-sha tag; bump it in the instances helmrelease template and re-render.

### Forcing a Pod Restart

```bash
kubectl rollout restart deployment/cc -n claudecode
```

### Checking Logs

```bash
# oauth2-proxy logs
kubectl logs -n claudecode deployment/cc -c oauth2-proxy

# ttyd + claude-session logs
kubectl logs -n claudecode deployment/cc -c app
```

### PostgreSQL Memory Table

```sql
-- View all memories for a user
SELECT * FROM knowledge WHERE agent_id = 'your@email.com' ORDER BY created_at DESC;

-- Clear all memories for a user
DELETE FROM knowledge WHERE agent_id = 'your@email.com';
```
