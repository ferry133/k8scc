## ADDED Requirements

### Requirement: Configuration comes from the environment and mounted files, and TLS is always verified

The netdev MCP server SHALL read its configuration from `UNIFI_CONTROLLER_URL`, `UNIFI_SITE` (default `default`), `UNIFI_CA_FILE`, `UNIFI_READER_KEY_FILE`, `UNIFI_OPERATOR_FILE`, `POD_IP`, `NETDEV_SNAPSHOT_DIR`, `NETDEV_SNAPSHOT_KEEP` (default 50), `NETDEV_HEALTH_TIMEOUT` (default 120), `NETDEV_WRITES_PER_HOUR` (default 12), `NETDEV_OUTSIDE_PROBES` (three comma-separated probes), `NETDEV_WITNESS_WAIT` (default 120), `IM_FAC_TICKET_REPO`, `IM_FAC_TICKET_TOKEN_FILE`, `NETDEV_READ_TIMEOUT` (default 15) and `CLAUDE_USER_ID`. It SHALL listen on `127.0.0.1:8766` over SSE. It SHALL verify the controller's TLS certificate against `UNIFI_CA_FILE` and SHALL offer no option to disable verification; an `http://` controller URL SHALL be refused unless `NETDEV_ALLOW_INSECURE_HTTP=1`, in which case the startup readings SHALL say so. No credential value SHALL appear in any startup reading, tool response, audit line or process argument.

#### Scenario: Plain http refused without the test flag
- **WHEN** `UNIFI_CONTROLLER_URL` is `http://…` and `NETDEV_ALLOW_INSECURE_HTTP` is unset
- **THEN** every tool responds that the controller URL is refused, and no request reaches the fake controller (positive control: with the flag set, requests arrive and the startup readings contain `insecure http accepted`)

#### Scenario: Credentials never surface
- **WHEN** the server starts with known dummy reader and operator values
- **THEN** the captured startup readings, every tool response in the test run and every audit line contain zero occurrences of those values

### Requirement: Three startup states decide which tools exist

With no usable reader credential every tool SHALL respond `not configured` naming the missing input. With a usable reader credential but an absent, empty or non-authenticating operator credential, or no ticket channel (`IM_FAC_TICKET_REPO` empty or its token file missing), the server SHALL register only the reader tools and SHALL name the reason in the startup readings. With all three usable the server SHALL also register the write tools. There SHALL be no state in which a write tool is registered but refuses every call for lack of configuration.

#### Scenario: Reader-only for each missing input
- **WHEN** the server starts three times — operator file absent, operator file present but the fake controller refuses its login, ticket repo unset
- **THEN** each time the tool list contains the ten reader tools and no write tool, and the startup readings name which input was missing (positive control: all present ⇒ write tools listed)

### Requirement: Reader tools strip secrets and name their path

`get_sites`, `get_controller_info`, `get_devices`, `get_clients`, `get_networks`, `get_firewall_rules`, `get_port_forwards`, `get_dhcp_reservations`, `get_wlans` and `get_lan_inventory` SHALL be read-only, SHALL replace the value of every key matching `^x_`, `_secret$`, `_key$`, `passphrase`, `password` or `psk` (case-insensitive, recursively) with `<stripped>`, SHALL begin the response with `[via integration]` or `[via legacy]`, and SHALL record what they returned in a session cache with a 600-second TTL for the write tools to address.

#### Scenario: Passphrase stripped at every exit
- **WHEN** `get_wlans` runs against a fixture containing `x_passphrase`
- **THEN** the response contains `<stripped>` and zero occurrences of the passphrase value; the same holds for the `will_change` of a later write to that WLAN and for the ticket body, while the snapshot file contains the value once (positive control)

#### Scenario: Path marker present
- **WHEN** any reader tool returns data from the fake controller
- **THEN** the first line is `[via integration]` or `[via legacy]` according to the route table

### Requirement: Every write is typed, reasoned, addressed from the cache, tokened, cooled and rate-limited

Each write tool SHALL take typed arguments and a non-blank `reason`, SHALL compose the request body server-side from the cached object changing only the fields the tool defines, SHALL refuse a target not returned by a reader in this session within the TTL, SHALL run in two phases (no `confirm` ⇒ a single-use token bound to the target, `expires_in: 120`, `will_change` as a stripped before/after diff, `cooldown_after: 600`, and the ticket number; valid token ⇒ consumed before acting), SHALL refuse the same target for 600 s after a write, and SHALL refuse once `NETDEV_WRITES_PER_HOUR` confirmed writes have run in the trailing hour. `confirm` SHALL be a string token, never a boolean.

#### Scenario: Blank reason refused before any request
- **WHEN** a write tool is called with a blank `reason`
- **THEN** it is refused and the fake controller recorded zero requests

#### Scenario: Unread target refused
- **WHEN** `block_client` names a MAC no reader returned in this session
- **THEN** it is refused with "read it first" (positive control: after `get_clients`, a token is issued)

#### Scenario: Body changes only the defined fields
- **WHEN** a confirmed `set_dhcp_reservation` runs
- **THEN** the PUT body the fake controller recorded equals the cached object with only `use_fixedip` and `fixed_ip` changed, and equals the `after` side of `will_change` field by field

#### Scenario: Token single-use, bound and timed
- **WHEN** a token is reused, used for another target, or used after 120 s
- **THEN** the call is refused and the fake controller shows exactly one write for the first use

#### Scenario: Cooldown and hourly ceiling
- **WHEN** a second write targets the same object within 600 s, and when the hourly ceiling has been reached
- **THEN** each is refused naming the seconds or count remaining (negative control: another object inside the cooldown window is accepted while under the ceiling)

### Requirement: Snapshot, apply, verify, revert from inside — with outside probes and FAC as witness

Before the token phase the server SHALL probe the three outside endpoints and open a ticket on the im→FAC channel carrying the cluster, the target's collection and id, the stripped diff and a deadline — never a snapshot or a secret; a failed probe or a failed ticket SHALL refuse the write. It SHALL wait up to `NETDEV_WITNESS_WAIT` for a `pre-ack:` comment; absence SHALL be recorded as `fac_witness: unmeasured` and SHALL NOT block. Before applying it SHALL GET the whole target object and the list of `_id`s in its collection into `NETDEV_SNAPSHOT_DIR` (a failed GET aborts). After applying, within `NETDEV_HEALTH_TIMEOUT`, it SHALL verify the controller API, the Kubernetes API server (`kubectl get --raw /readyz`), the object reading back equal to `after`, the three probes (two or more lost ⇒ failure; one lost ⇒ `outbound: degraded`), and — when a pre-ack arrived — a `post-ack:` comment (absent with probes answering ⇒ failure, `fac_witness: lost`). Any failure SHALL PUT the snapshot back through the controller — or, for a write that created an object, DELETE the `_id` absent from the snapshot's list — and re-check. The ticket SHALL be closed with a release comment carrying `outbound`, `fac_witness` and `result`; a failed revert SHALL be `result: revert-failed` and the tool response SHALL instruct im to write a dated inbox report immediately. No comment the server writes itself (release, `revert-failed`, or any other) SHALL begin with `pre-ack:` or `post-ack:`.

#### Scenario: Unhealthy baseline refuses
- **WHEN** one of the three probe listeners is down before the write
- **THEN** the write is refused, no ticket is opened and no PUT occurs (positive control: all three up ⇒ token issued and ticket opened)

#### Scenario: FAC silent, im reverts from inside
- **WHEN** the fake FAC pre-acks, the confirmed write runs, and two probe listeners are then stopped while the fake controller keeps answering
- **THEN** within `NETDEV_HEALTH_TIMEOUT` the server PUTs the snapshot, the ticket is closed with `outbound: lost`, `result: reverted`, and the audit shows `token`, `execute`, `reverted` for that target (negative control: probes kept up ⇒ `result: applied`, no snapshot PUT)

#### Scenario: Witness unmeasured does not block
- **WHEN** the fake FAC never comments and the probes stay up
- **THEN** the write proceeds and the release reads `fac_witness: unmeasured`, `result: applied`

#### Scenario: Witness armed and lost reverts
- **WHEN** the fake FAC pre-acks but never post-acks while the probes stay up
- **THEN** the write is reverted and the release reads `fac_witness: lost`, `result: reverted`

#### Scenario: Server's own comments never count as witness
- **WHEN** the fake FAC never comments and the server writes its own comments on the ticket (release, `revert-failed`)
- **THEN** none of those comments begins with `pre-ack:` or `post-ack:` and `fac_witness` stays `unmeasured` (positive control: a fixture comment beginning with `pre-ack:` from the same GitHub identity arms it)

#### Scenario: Created object reverted by delete
- **WHEN** a confirmed `set_port_forward` creates a new object and the health check then fails
- **THEN** the server DELETEs the one `_id` absent from the snapshot's `_id` list, the collection reads back equal to that list, and the release reads `result: reverted` (negative control: health passing ⇒ no DELETE)

#### Scenario: Snapshot GET failure aborts
- **WHEN** the fake controller returns an error for the snapshot GET
- **THEN** no PUT is sent, the ticket is closed with `result: aborted`, and the response names the GET error

#### Scenario: Revert failure is reported, not swallowed
- **WHEN** the fake controller stops answering after accepting the write
- **THEN** the audit line is `revert-failed`, the ticket stays open with a `revert-failed` comment, and the response contains the inbox instruction with the snapshot digest

### Requirement: A target on im's own LAN path is refused, named, and fails closed on missing data

At each write the server SHALL compute from the session's reader data the networks containing `POD_IP`, the controller address and the API server endpoints; the switch port or AP the node is attached to; and every firewall rule whose expanded source/destination (groups expanded, CIDR containment) covers pod↔controller or pod↔API server. A target in that set SHALL be refused naming the intersecting element before any ticket or token. If any of those inputs cannot be read, every write SHALL be refused with the reader error. `restart_device` SHALL refuse the gateway device. No tool SHALL exist for VLAN create/delete, gateway restart/firmware, WLAN passphrases or deletion, WAN settings, default firewall policies, site settings, or user/API-key management.

#### Scenario: Own network refused
- **WHEN** a write targets the network object containing `POD_IP`
- **THEN** it is refused naming that network, with no ticket and no PUT (positive control: an unrelated network reaches the token phase)

#### Scenario: Firewall group expansion
- **WHEN** a firewall rule's destination group contains the controller address
- **THEN** a write to that rule is refused (negative control: a group without it proceeds)

#### Scenario: Gateway restart refused
- **WHEN** `restart_device` names the gateway's MAC
- **THEN** it is refused (positive control: an AP's MAC reaches the token phase)

#### Scenario: Missing reader data fails closed
- **WHEN** `get_networks` cannot be read at write time
- **THEN** every write tool refuses with the reader error

#### Scenario: Forbidden verbs have no tool
- **WHEN** the registered tool list is enumerated with all three inputs usable
- **THEN** it contains exactly the ten reader tools and the write tools listed in the proposal, and none whose name or arguments address the forbidden objects

### Requirement: Every write phase leaves one audit line on stdout

For every write call the server SHALL write one JSON line to stdout with `ts`, `tool`, `target`, `phase` (`token`, `execute`, `refused`, `reverted`, `revert-failed`, `aborted`), `stated_reason`, `reason` or `rc`, `user`, `snapshot_digest`, `diff_digest`, `health` (the list of health checks that passed; empty before any check ran), `outbound` and `fac_witness`, and SHALL write nothing else to stdout.

#### Scenario: Lines match phases
- **WHEN** a write goes token → execute → reverted on the fake controller because two probes were lost
- **THEN** stdout holds exactly three JSON lines for that target with those phases, identical `stated_reason`, `snapshot_digest` equal to the sha256 of the snapshot file, `health` empty on the `token` line and listing `controller`, `apiserver`, `readback` but not `probes` on the `execute` and `reverted` lines
