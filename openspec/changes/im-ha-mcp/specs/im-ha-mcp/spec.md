## ADDED Requirements

### Requirement: Home Assistant's MCP server is registered only when both the URL and the token are present

`claude-session` SHALL register a remote SSE MCP server named `ha` only when `HA_MCP_URL` is set to a non-empty value **and** the file named by `HA_MCP_TOKEN_FILE` (default `/etc/ha-mcp/token`) exists and is non-empty. An empty `HA_MCP_URL` SHALL be treated exactly as unset (silent, nothing written). A non-empty URL with a missing or 0-byte token file SHALL write nothing and print exactly one line beginning `⚠ Home Assistant 沒有設定：` naming the missing input.

#### Scenario: Unset or empty URL is silent
- **WHEN** `HA_MCP_URL` is unset, or set to the empty string
- **THEN** `.claude.json` carries no `ha` server, `settings.json` is not touched, and no line mentioning Home Assistant is printed

#### Scenario: URL without token fails closed in one line
- **WHEN** `HA_MCP_URL` is non-empty and the token file is absent (and, as a second case, present but 0 bytes)
- **THEN** no `ha` server is registered, `permissions` is not touched, and the startup output contains exactly one `⚠ Home Assistant 沒有設定：` line (negative control: a second such line fails the check)

#### Scenario: Both present registers once
- **WHEN** `HA_MCP_URL` is non-empty and the token file is non-empty
- **THEN** `claude mcp get ha` exits 0 and reports an SSE server at that URL, and a second run of `claude-session` leaves `.claude.json` byte-identical

### Requirement: The token enters only through the file and leaves no trace in argv, env or output

`claude-session` SHALL read the token from the file inside the process that writes `.claude.json`, SHALL NOT pass it as a command-line argument to any program, SHALL NOT export it into the environment, and SHALL NOT print it or any prefix of it. Trailing whitespace in the file SHALL be stripped.

#### Scenario: No trace of the token
- **WHEN** `claude-session` runs with a known dummy token in the file
- **THEN** the captured startup output and the process environment contain zero occurrences of the token's first 8 characters (positive control: `HA_MCP_URL` occurs at least once in the environment), and the registered header equals `Bearer <token>` with the trailing newline removed

### Requirement: A rotated token is re-registered; an unchanged one is not rewritten

`claude-session` SHALL record the sha256 of the token it last registered and SHALL rewrite the `ha` entry only when the current token's sha256 differs.

#### Scenario: Rotation rewrites once
- **WHEN** the token file's content changes between two runs
- **THEN** the second run rewrites the `ha` entry with the new bearer value and a third run with the same content changes nothing

### Requirement: The permission profile allows the whole `ha` server

Whenever the `ha` server is registered, `claude-session` SHALL merge the server-level rule `mcp__ha` into `permissions.allow` with the existing merge semantics (order kept, no duplicates, unparseable file left alone and reported).

#### Scenario: Server-level rule present
- **WHEN** the `ha` server is registered
- **THEN** `permissions.allow` contains `mcp__ha` after the entries that were already there, and a second run changes nothing

### Requirement: The managed policy states im's authority over devices and its limits

`im-policy.md` SHALL carry a section applicable only when `HA_MCP_URL` is set and no `⚠ Home Assistant 沒有設定` line was printed, stating that im operates exposed entities and creates, modifies and deletes automations and scenes on its own judgement and reports afterwards in `~/workspace/inbox/`; and that im does not enable or pair the HomeKit Bridge, does not bind the Google Assistant integration, does not add or remove Home Assistant users or integrations, does not upgrade Home Assistant, and does not touch homebridge. It SHALL state that automations live only in Home Assistant.

#### Scenario: Controls find every sentence
- **WHEN** `test/im-policy-controls.sh` runs against the shipped `im-policy.md`
- **THEN** it finds the applicability gate, the autonomy sentence, the report sentence, each of the five prohibitions and the single-home-for-automations sentence, and fails against a copy with any one of them removed
