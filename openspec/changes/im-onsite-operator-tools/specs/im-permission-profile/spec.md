## ADDED Requirements

### Requirement: Permission rules follow the tool surface

`claude-session` SHALL ensure, whenever the Talos MCP sidecar is registered (`TALOS_MCP_URL` set), that the agent's `settings.json` carries `permissions.allow` entries for the four read-only Talos tools and a `permissions.ask` entry for `mcp__talos__reboot_node`, merging into any existing `permissions` object without removing or reordering entries the customer added.

#### Scenario: Fresh pod gets the profile
- **WHEN** `claude-session` runs on a pod whose `settings.json` is the initial minimal file
- **THEN** afterwards `permissions.allow` contains `mcp__talos__get_node_status`, `mcp__talos__get_etcd_members`, `mcp__talos__get_link_status`, `mcp__talos__get_service_logs`, and `permissions.ask` contains `mcp__talos__reboot_node`

#### Scenario: Existing customer rules survive
- **WHEN** `settings.json` already carries `permissions.allow` with the customer's own entries
- **THEN** those entries remain, the Talos entries are appended without duplicates, and a second run changes nothing

#### Scenario: Sidecar absent, nothing written
- **WHEN** `TALOS_MCP_URL` is not set
- **THEN** `settings.json`'s `permissions` object is not touched

### Requirement: Reboot always asks the person at the terminal

The mutating tool SHALL never be auto-approved: it is listed under `ask`, never `allow`, and documentation shipped with the image SHALL state that the approval comes from whoever is at the ttyd session — the customer — not from the operator company.

#### Scenario: Read tools run without a prompt
- **WHEN** the agent calls any `mcp__talos__get_*` tool in a ttyd session
- **THEN** no permission prompt is shown

#### Scenario: Reboot prompts
- **WHEN** the agent calls `mcp__talos__reboot_node` in a ttyd session
- **THEN** a permission prompt is shown and the call proceeds only on an explicit approval at that terminal
