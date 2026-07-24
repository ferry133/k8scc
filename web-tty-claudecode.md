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
1. User opens https://cc.janncot.com
2. oauth2-proxy checks for valid session cookie
   └─ No cookie → redirect to Auth0 login page
   └─ Auth0 (Google / GitHub / etc.) → callback → oauth2-proxy issues cookie
3. oauth2-proxy forwards request to ttyd (localhost:7681)
   └─ Injects X-Auth-Request-Email: user@gmail.com header
4. ttyd spawns claude-session
   └─ HTTP headers become env vars (CGI-style):
      X-Auth-Request-Email → HTTP_X_AUTH_REQUEST_EMAIL
5. claude-session sets CLAUDE_USER_ID=user@gmail.com
6. MCP memory server uses CLAUDE_USER_ID as agent_id in PostgreSQL
7. Claude Code launches with per-user memory namespace
```

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

- **Cluster**: jcom (`janncot.com`)
- **Namespace**: `claudecode`
- **HelmRelease**: `cc` (single replica, `Recreate` strategy)
- **Image**: `ghcr.io/ferry133/claude-code:latest`
- **Ingress**: `cc.janncot.com` via Envoy Gateway (HTTPS)
- **Secrets**: `claude-code-secret` (SOPS encrypted) — `DATABASE_URL`, `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`, `OAUTH2_PROXY_COOKIE_SECRET`
- **Storage**:
  - `claude-config` PVC — 5Gi (sc-nas) → `/home/claude/.claude`
  - `claude-workspace` PVC — 20Gi (sc-nas) → `/home/claude/workspace`
  - `coding` NFS — `10.9.1.12:/volume2/coding` → `/home/claude/coding`

### Auth0 Configuration

- **Application type**: Regular Web Application
- **Allowed Callback URLs**: `https://cc.janncot.com/oauth2/callback`
- **Allowed Logout URLs**: `https://cc.janncot.com`
- **Allowed Web Origins**: `https://cc.janncot.com`
- **Connections**: Google, GitHub (or any configured social login)

To restrict access to specific emails or domains, configure Auth0 **Rules** or **Actions** in the Auth0 Dashboard.

---

## User Guide

### Logging In

1. Open **https://cc.janncot.com** in your browser
2. You will be redirected to Auth0 login — choose your Google (or GitHub) account
3. After successful login, the terminal opens automatically
4. A welcome message confirms your identity: `✓ 歡迎，your@email.com`
5. Claude Code launches immediately — no extra steps needed

> If you are already signed into the same browser session, login is automatic (SSO — no interaction required). To verify who is currently logged in, open **https://cc.janncot.com/oauth2/userinfo** in the same browser.

### Claude Account Sign-in (first use)

On first launch Claude Code asks you to sign in to your Claude account and shows a long OAuth URL. The URL wraps across several terminal lines, so the in-terminal link is unreliable — instead, an orange **「🔗 開啟 Claude 登入頁 / Open sign-in page」** button appears at the top-right of the page:

1. Click it — the sign-in page opens in a new tab; approve access there.
2. On the confirmation page, click **Copy code**.
3. Switch back to the terminal tab — the code is detected in your clipboard and submitted automatically (your browser may ask once for clipboard permission; if it was denied, click **「📋 貼上驗證碼 / Paste code」** instead).

The buttons disappear once you are signed in. (They are added by a patched ttyd client page that reassembles the wrapped URL; the clipboard is only read while a sign-in is pending, and only a code matching this exact sign-in attempt is ever accepted.)

### Using the Terminal

The terminal is a full bash session running as user `claude`. Claude Code is launched automatically on login.

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

- Open **https://cc.janncot.com/oauth2/sign_out** in your browser

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

Push to `main` branch of `ferry133/k8scc` — GitHub Actions builds and pushes `ghcr.io/ferry133/claude-code:latest` automatically (multi-arch: amd64 + arm64).

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
