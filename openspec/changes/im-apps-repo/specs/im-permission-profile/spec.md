## MODIFIED Requirements

### Requirement: Permission rules follow the tool surface

`claude-session` SHALL ensure, whenever the Talos MCP sidecar is registered (`TALOS_MCP_URL` set), that the agent's `settings.json` carries `permissions.allow` entries for all five Talos tools — the four read-only tools and `mcp__talos__reboot_node` — merging into any existing `permissions` object without removing or reordering entries that were already there, and SHALL remove `mcp__talos__reboot_node` from `permissions.ask` where an earlier image placed it, leaving every other `ask` entry in place. Whenever the apps-repo write path is configured (`IM_APPS_REPO` set and the write key mounted), it SHALL likewise ensure `permissions.allow` carries `Bash(git *)` and `Bash(sops *)`, with the same merge semantics, and SHALL add neither when the write path is absent.

#### Scenario: Fresh pod gets the profile
- **WHEN** `claude-session` runs on a pod whose `settings.json` is the initial minimal file
- **THEN** afterwards `permissions.allow` contains `mcp__talos__get_node_status`, `mcp__talos__get_etcd_members`, `mcp__talos__get_link_status`, `mcp__talos__get_service_logs`, `mcp__talos__reboot_node`, and no `permissions.ask` key is created

#### Scenario: Existing rules survive
- **WHEN** `settings.json` already carries `permissions.allow` with entries of its own
- **THEN** those entries remain first, the Talos entries are appended without duplicates, and a second run changes nothing

#### Scenario: Earlier profile is migrated
- **WHEN** `settings.json` carries `mcp__talos__reboot_node` under `permissions.ask` next to other `ask` entries (a pod that ran the previous image)
- **THEN** afterwards `mcp__talos__reboot_node` is under `allow` and not under `ask`, the other `ask` entries are untouched, and a second run changes nothing

#### Scenario: Sidecar absent, nothing written
- **WHEN** `TALOS_MCP_URL` is not set
- **THEN** `settings.json`'s `permissions` object is not touched

#### Scenario: Apps repo configured adds the git and sops rules
- **WHEN** `claude-session` runs with `IM_APPS_REPO` set and the write key mounted
- **THEN** `permissions.allow` contains `Bash(git *)` and `Bash(sops *)` after the existing entries, without duplicates, and a second run changes nothing

#### Scenario: Apps repo absent adds nothing
- **WHEN** `IM_APPS_REPO` is unset or the key is missing
- **THEN** `permissions.allow` contains neither `Bash(git *)` nor `Bash(sops *)` unless they were already there
