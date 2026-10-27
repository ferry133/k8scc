# im-permission-profile Specification

## Purpose
What `claude-session` writes into im's Claude Code permission profile when the Talos MCP sidecar is present: all five `mcp__talos__*` tools allowed, nobody prompted (ferry133 ruling 2026-10-15), merged idempotently into existing settings. Root: jg-base `im-onsite-operator-tools`.
## Requirements
### Requirement: Permission rules follow the tool surface

`claude-session` SHALL ensure, whenever the Talos MCP sidecar is registered (`TALOS_MCP_URL` set), that the agent's `settings.json` carries `permissions.allow` entries for all five Talos tools — the four read-only tools and `mcp__talos__reboot_node` — merging into any existing `permissions` object without removing or reordering entries that were already there, and SHALL remove `mcp__talos__reboot_node` from `permissions.ask` where an earlier image placed it, leaving every other `ask` entry in place. Whenever the apps-repo write path is configured (`IM_APPS_REPO` set and the write key mounted), it SHALL likewise ensure `permissions.allow` carries `Bash(git *)` and `Bash(sops *)`, with the same merge semantics, and SHALL add neither when the write path is absent. Whenever Home Assistant's MCP server is registered (`HA_MCP_URL` non-empty and the token file present and non-empty), it SHALL likewise ensure `permissions.allow` carries the server-level rule `mcp__ha`, with the same merge semantics, and SHALL not add it otherwise.

#### Scenario: Fresh pod gets the profile
- **WHEN** `claude-session` runs on a pod whose `settings.json` is the initial minimal file
- **THEN** afterwards `permissions.allow` contains `mcp__talos__get_node_status`, `mcp__talos__get_etcd_members`, `mcp__talos__get_link_status`, `mcp__talos__get_service_logs`, `mcp__talos__reboot_node`, and no `permissions.ask` key is created

#### Scenario: Existing rules survive
- **WHEN** `settings.json` already carries `permissions.allow` with entries of its own
- **THEN** those entries remain first, the Talos entries are appended without duplicates, and a second run changes nothing

#### Scenario: Earlier profile is migrated
- **WHEN** `settings.json` carries `mcp__talos__reboot_node` under `permissions.ask` next to other `ask` entries (a pod that ran the previous image)
- **THEN** afterwards `mcp__talos__reboot_node` is under `allow` and not under `ask`, the other `ask` entries are untouched, and a second run changes nothing

#### Scenario: Sidecar absent and no apps repo, nothing written
- **WHEN** `TALOS_MCP_URL` is not set and the apps-repo write path is not configured
- **THEN** `settings.json`'s `permissions` object is not touched

#### Scenario: Sidecar absent, apps repo present
- **WHEN** `TALOS_MCP_URL` is not set but `IM_APPS_REPO` is set and the write key is mounted
- **THEN** `permissions.allow` gains only `Bash(git *)` and `Bash(sops *)`; no `mcp__talos__*` entry is added and `permissions.ask` is not touched

#### Scenario: Apps repo configured adds the git and sops rules
- **WHEN** `claude-session` runs with `IM_APPS_REPO` set and the write key mounted
- **THEN** `permissions.allow` contains `Bash(git *)` and `Bash(sops *)` after the existing entries, without duplicates, and a second run changes nothing

#### Scenario: Apps repo absent adds nothing
- **WHEN** `IM_APPS_REPO` is unset or the key is missing
- **THEN** `permissions.allow` contains neither `Bash(git *)` nor `Bash(sops *)` unless they were already there

#### Scenario: Home Assistant registered adds the server rule
- **WHEN** `claude-session` runs with `HA_MCP_URL` non-empty and a non-empty token file, and nothing else configured
- **THEN** `permissions.allow` gains only `mcp__ha`; no `mcp__talos__*`, `Bash(git *)` or `Bash(sops *)` entry is added, and a second run changes nothing

#### Scenario: Home Assistant absent adds nothing
- **WHEN** `HA_MCP_URL` is unset or empty, or the token file is missing or 0 bytes
- **THEN** `permissions.allow` does not contain `mcp__ha` unless it was already there

### Requirement: No human prompt is relied upon

The reboot SHALL be within the agent's own authority: `mcp__talos__reboot_node` is listed under `allow`, never `ask`, and the repository's `CLAUDE.md` SHALL state that im is the company's tool, that only company staff reach the ttyd, that the reboot's gates are the sidecar's own (stated reason, call-time allowlist, control-plane quorum, per-node cooldown, two-phase token), and that im reports afterwards to the company through the inbox or the health mail (ferry133 ruling, 2026-10-15).

#### Scenario: Tools run without a prompt
- **WHEN** the agent calls any `mcp__talos__*` tool in a ttyd session
- **THEN** no permission prompt is shown; whether the reboot runs is decided by the sidecar's gates alone

#### Scenario: Documentation names the authority and the report
- **WHEN** the repository's `CLAUDE.md` is read
- **THEN** it says the reboot is within im's authority, lists the server-side gates, and says where im reports afterwards (the instructions im itself reads inside the pod are not shipped by this image; whoever owns them carries the same sentence)

