## ADDED Requirements

### Requirement: Permission rules follow the tool surface

`claude-session` SHALL ensure, whenever the Talos MCP sidecar is registered (`TALOS_MCP_URL` set), that the agent's `settings.json` carries `permissions.allow` entries for all five Talos tools — the four read-only tools and `mcp__talos__reboot_node` — merging into any existing `permissions` object without removing or reordering entries that were already there, and SHALL remove `mcp__talos__reboot_node` from `permissions.ask` where an earlier image placed it, leaving every other `ask` entry in place.

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

### Requirement: No human prompt is relied upon

The reboot SHALL be within the agent's own authority: `mcp__talos__reboot_node` is listed under `allow`, never `ask`, and the repository's `CLAUDE.md` SHALL state that im is the company's tool, that only company staff reach the ttyd, that the reboot's gates are the sidecar's own (stated reason, call-time allowlist, control-plane quorum, per-node cooldown, two-phase token), and that im reports afterwards to the company through the inbox or the health mail (ferry133 ruling, 2026-10-15).

#### Scenario: Tools run without a prompt
- **WHEN** the agent calls any `mcp__talos__*` tool in a ttyd session
- **THEN** no permission prompt is shown; whether the reboot runs is decided by the sidecar's gates alone

#### Scenario: Documentation names the authority and the report
- **WHEN** the repository's `CLAUDE.md` is read
- **THEN** it says the reboot is within im's authority, lists the server-side gates, and says where im reports afterwards (the instructions im itself reads inside the pod are not shipped by this image; whoever owns them carries the same sentence)
