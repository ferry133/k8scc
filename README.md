# k8scc — Claude Code Web Terminal

Containerised [Claude Code](https://claude.ai/code) CLI served through a browser-based terminal ([ttyd](https://github.com/tsl0922/ttyd)).

**Image**: `ghcr.io/ferry133/claude-code`

---

## Architecture

```
Browser
  │  HTTPS
  ▼
[Ingress / Reverse Proxy]   ← optional: ttyd basic auth via TTYD_CREDENTIAL
  │
  ▼
ttyd :7681
  │  spawns
  ▼
claude-session (bash script)
  ├─ reads HTTP_X_AUTH_REQUEST_EMAIL  → sets CLAUDE_USER_ID
  ├─ initialises ~/.claude/settings.json
  ├─ injects MCP memory server        ← only when DATABASE_URL is set
  └─ exec claude
```

### Key components

| File | Role |
|------|------|
| `Dockerfile` | Image build: debian:12-slim + ttyd + GNOME Keyring + Claude Code |
| `entrypoint.sh` | Container entry: starts D-Bus + GNOME Keyring, launches ttyd |
| `claude-session` | Shell session init: resolves user identity, writes settings, execs `claude` |
| `memory_mcp_server.py` | MCP server: PostgreSQL-backed `remember` / `recall` / `forget` tools |
| `auth0_login.py` | Auth0 Device Flow helper (optional; not used by default) |

### Ports

| Port | Description |
|------|-------------|
| 7681 | ttyd web terminal (HTTP) |

### Non-root user

All Claude Code processes run as `claude` (uid 1000). Workspace is `/home/claude/workspace`.

---

## Configuration

All configuration is via environment variables passed to the container.

| Variable | Required | Description |
|----------|----------|-------------|
| `TTYD_CREDENTIAL` | No | ttyd basic auth in `user:password` format. If unset, the terminal is unauthenticated. |
| `DATABASE_URL` | No | PostgreSQL DSN. Enables the MCP memory server when set. |
| `CLAUDE_USER_ID` | No | Agent identity used as `agent_id` in the DB. Defaults to `claude_code`. Overridden by `HTTP_X_AUTH_REQUEST_EMAIL` when oauth2-proxy is in front. |
| `AUTH0_DOMAIN` | No | Auth0 domain for Device Flow login (requires `auth0_login.py`). |
| `AUTH0_CLIENT_ID` | No | Auth0 client ID for Device Flow login. |

### User identity resolution (in `claude-session`)

1. If `HTTP_X_AUTH_REQUEST_EMAIL` is present (set by oauth2-proxy), use it as `CLAUDE_USER_ID`.
2. Otherwise fall back to the `CLAUDE_USER_ID` env var, then to `claude_code`.

---

## Persistence

| Layer | Mechanism | Mount path | Notes |
|-------|-----------|------------|-------|
| Claude settings & auto-memory | Claude Code built-in | `/home/claude/.claude` | Mount a PVC here to survive container rebuilds |
| Explicit memory | MCP Memory Server | PostgreSQL `knowledge` table | Enabled when `DATABASE_URL` is set |

### PostgreSQL schema (for MCP memory)

```sql
CREATE TABLE knowledge (
    id           SERIAL PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    fact         TEXT NOT NULL,
    confidence   FLOAT NOT NULL DEFAULT 0.8,
    source_count INT   NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, fact)
);
```

---

## CI/CD

GitHub Actions workflow: [`.github/workflows/build.yaml`](.github/workflows/build.yaml)

- **Triggers**: push to `main`, `workflow_dispatch`
- **Platforms**: `linux/amd64`, `linux/arm64`
- **Tags**: `latest` (main branch) + short SHA
- **Registry**: GitHub Container Registry (`ghcr.io/ferry133/claude-code`)

### Required secret

`GHCR_TOKEN` — Classic PAT with `write:packages` scope.

> Fine-grained PATs and `GITHUB_TOKEN` will not work because the `claude-code` GHCR package is bound to the `ferry133/jg-jiahd` repo (where it was originally created).

Add it at: **`ferry133/k8scc` → Settings → Secrets and variables → Actions**

---

## Kubernetes deployment

A typical HelmRelease snippet:

```yaml
env:
  - name: TTYD_CREDENTIAL
    valueFrom:
      secretKeyRef:
        name: cluster-secrets
        key: TTYD_CREDENTIAL
  - name: DATABASE_URL          # optional
    valueFrom:
      secretKeyRef:
        name: cluster-secrets
        key: DATABASE_URL
volumeMounts:
  - name: claude-data
    mountPath: /home/claude/.claude
volumes:
  - name: claude-data
    persistentVolumeClaim:
      claimName: claude-data
```

---

## Local run

```bash
docker run --rm -it \
  -p 7681:7681 \
  -e TTYD_CREDENTIAL="admin:changeme" \
  ghcr.io/ferry133/claude-code
```

Open `http://localhost:7681` in a browser.
