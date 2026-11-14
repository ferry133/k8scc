## ADDED Requirements

### Requirement: Configuration comes from the environment and mounted files, and TLS is always verified

The netdev MCP server SHALL read its configuration from `UNIFI_CONTROLLER_URL`, `UNIFI_SITE` (default `default`), `UNIFI_CA_FILE`, `UNIFI_READER_FILE`, `UNIFI_OPERATOR_FILE`, `POD_IP`, `NETDEV_SNAPSHOT_DIR`, `NETDEV_SNAPSHOT_KEEP` (default 50), `NETDEV_HEALTH_TIMEOUT` (default 120), `NETDEV_WRITES_PER_HOUR` (default 12), `NETDEV_OUTSIDE_PROBES` (three comma-separated probes), `NETDEV_WITNESS_WAIT` (default 120), `IM_FAC_TICKET_REPO`, `IM_FAC_TICKET_TOKEN_FILE`, `NETDEV_READ_TIMEOUT` (default 15), `CLAUDE_USER_ID`, `CLUSTER_NAME` (ticket title), and the tuning values `NETDEV_HEALTH_INTERVAL` (default 10), `NETDEV_WITNESS_POLL` (default 5), `NETDEV_PROBE_TIMEOUT` (default 5) and `IM_FAC_TICKET_API` (default `https://api.github.com`, held to the same https rule as the controller). It SHALL listen on `127.0.0.1:8766` over SSE. It SHALL verify the controller's TLS certificate against `UNIFI_CA_FILE` and SHALL offer no option to disable verification. When that file holds exactly one certificate that is not a CA (a console's factory certificate, self-signed for `unifi.local` and valid for no address the sidecar dials: FAC1, k8scc#67), it SHALL be the only trust anchor and the hostname SHALL NOT be checked. Otherwise (a CA, a chain, several certificates) the hostname SHALL be checked. The startup readings SHALL name the mode; an `http://` controller URL SHALL be refused unless `NETDEV_ALLOW_INSECURE_HTTP=1`, in which case the startup readings SHALL say so. The reader credential SHALL be the console's View Only local account, two lines (user, password), logged in over `/api/auth/login` (cookie + CSRF) on its own HTTP client, like the operator's and never sharing a client with it; a 401 SHALL log in again once. There SHALL be no API-key path: on FAC1 a View Only user cannot create a key, and the owner's key wrote (k8scc#67 finding 6, ferry133 2026-11-13). A set `UNIFI_READER_KEY_FILE` SHALL NOT be read, and SHALL be named in the not-configured answer when `UNIFI_READER_FILE` is missing. No credential value SHALL appear in any startup reading, tool response, audit line or process argument.

#### Scenario: Plain http refused without the test flag
- **WHEN** `UNIFI_CONTROLLER_URL` is `http://…` and `NETDEV_ALLOW_INSECURE_HTTP` is unset
- **THEN** every tool responds that the controller URL is refused, and no request reaches the fake controller (positive control: with the flag set, requests arrive and the startup readings contain `insecure http accepted`)

#### Scenario: Factory certificate pinned, nothing else trusted
- **WHEN** the fake controller is served over TLS with a self-signed `CN=unifi.local` certificate with no SAN and no basicConstraints, and `UNIFI_CA_FILE` is that certificate
- **THEN** the reader reads, and the startup readings say `TLS: pinned leaf` (controls: the earlier `verify=<file>` context fails with an IP-address mismatch; another such certificate as `UNIFI_CA_FILE` is refused before any request; a file of two certificates, one of them the served one, keeps the hostname check and is refused; a customer CA keeps the hostname check, refusing a leaf valid only for `unifi.local` and reading one valid for the address dialled)

#### Scenario: Credentials never surface
- **WHEN** the server starts with known dummy reader and operator values
- **THEN** the captured startup readings, every tool response in the test run and every audit line contain zero occurrences of those values, the reader's and the operator's cookies included

#### Scenario: Reader is a login, not a key
- **WHEN** the reader tools run with `UNIFI_READER_FILE` holding the dummy View Only account
- **THEN** the fake controller recorded one login for that user and every GET carried the reader's cookie and none the operator's; no request carried `X-API-Key` (positive controls: the fake answers a key-only GET 401 and a write with the reader's cookie 403, as FAC1's View Only account did)

### Requirement: Three startup states decide which tools exist

With no usable reader credential every tool SHALL respond `not configured` naming the missing input. With a usable reader credential but an absent, empty or non-authenticating operator credential, no ticket channel (`IM_FAC_TICKET_REPO` empty or its token file missing), `NETDEV_OUTSIDE_PROBES` not exactly three, `POD_IP` unset, or `NETDEV_SNAPSHOT_DIR` unset or not writable, the server SHALL register only the reader tools and SHALL name the reason in the startup readings. With all of them usable the server SHALL also register the write tools. There SHALL be no state in which a write tool is registered but refuses every call for lack of configuration.

#### Scenario: Reader-only for each missing input
- **WHEN** the server starts three times — operator file absent, operator file present but the fake controller refuses its login, ticket repo unset
- **THEN** each time the tool list contains the ten reader tools and no write tool, and the startup readings name which input was missing (positive control: all present ⇒ write tools listed); the same holds for two probes, `POD_IP` unset and the snapshot directory unset

### Requirement: Reader tools strip secrets and name their path

`get_sites`, `get_controller_info`, `get_devices`, `get_clients`, `get_networks`, `get_firewall_rules`, `get_port_forwards`, `get_dhcp_reservations`, `get_wlans` and `get_lan_inventory` SHALL be read-only, SHALL replace the value of every key matching `^x_`, `_secret$`, `_keys?$`, `passphrase`, `password`, `psk` or `preshared` (case-insensitive, recursively; `private_preshared_keys` matched none of the first six, k8scc#67 finding 2) with `<stripped>`, SHALL begin the response with `[via legacy]`, `[via legacy-root]`, `[via v2]` or, for `get_firewall_rules`, `[via legacy+v2]`, SHALL use no Integration API route (it refuses the View Only cookie: fleet-ops-b0 on FAC1, 2026-11-14), and SHALL record what they returned in a session cache with a 600-second TTL for the write tools to address.

#### Scenario: Passphrase stripped at every exit
- **WHEN** `get_wlans` runs against a fixture containing `x_passphrase`
- **THEN** the response contains `<stripped>` and zero occurrences of the passphrase value

#### Scenario: Secret stripped at every write exit, kept by the snapshot
- **WHEN** a confirmed write addresses an object carrying an `x_` field (no write tool addresses a WLAN, so the controls use a user row)
- **THEN** the token response, the result, every audit line and the ticket contain zero occurrences of the value and the snapshot file contains it once (positive control), and the PUT body carries the original value, not `<stripped>`

#### Scenario: Path marker present
- **WHEN** any reader tool returns data from the fake controller
- **THEN** the first line names the API according to the route table

#### Scenario: Zone-based firewall is read
- **WHEN** the legacy firewall rule and group collections answer empty and v2 `firewall-policies` holds policies, as on FAC1 (Network 11.0.99)
- **THEN** `get_firewall_rules` returns the policies with each side expanded (IP list, network subnets, or a zone's networks), naming as unresolved any side it cannot expand (a zone with no networks, an unread zone, a negated side, a target type it does not model) (negative controls: zones unreadable ⇒ only zone-given sides become unresolved; policies unreadable ⇒ an error, not an empty firewall)

### Requirement: Every write is typed, reasoned, addressed from the cache, tokened, cooled and rate-limited

Each write tool SHALL take typed arguments and a non-blank `reason`, SHALL compose the request body server-side from the object as the snapshot read it, unstripped, changing only the fields the tool defines (so a secret goes back as it was), SHALL refuse a target not returned by a reader in this session within the TTL, SHALL run in two phases (no `confirm` ⇒ a single-use token bound to the target, `expires_in: 120`, `will_change` as a stripped before/after diff, `cooldown_after: 600`, and the ticket number; valid token ⇒ consumed before acting), SHALL refuse the same target for 600 s after a write, and SHALL refuse once `NETDEV_WRITES_PER_HOUR` confirmed writes have run in the trailing hour. `confirm` SHALL be a string token, never a boolean.

#### Scenario: Blank reason refused before any request
- **WHEN** a write tool is called with a blank `reason`
- **THEN** it is refused and the fake controller recorded zero requests

#### Scenario: Unread target refused
- **WHEN** `block_client` names a MAC no reader returned in this session
- **THEN** it is refused with "read it first" (positive control: after `get_clients`, a token is issued)

#### Scenario: Body changes only the defined fields
- **WHEN** a confirmed `set_dhcp_reservation` runs
- **THEN** the PUT body the fake controller recorded equals the cached object with only `use_fixedip` and `fixed_ip` changed, and equals the `after` side of `will_change` field by field except that a field `after` shows as `<stripped>` carries its original value

#### Scenario: Token single-use, bound and timed
- **WHEN** a token is reused, used for another target, or used after 120 s
- **THEN** the call is refused and the fake controller shows exactly one write for the first use

#### Scenario: Cooldown and hourly ceiling
- **WHEN** a second write targets the same object within 600 s, and when the hourly ceiling has been reached
- **THEN** each is refused naming the seconds or count remaining (negative control: another object inside the cooldown window is accepted while under the ceiling)

### Requirement: Snapshot, apply, verify, revert from inside — with outside probes and FAC as witness

Before the token phase the server SHALL probe the three outside endpoints and open a ticket on the im→FAC channel carrying the cluster, the target's collection and id, the stripped diff and a deadline — never a snapshot or a secret; a failed probe or a failed ticket SHALL refuse the write. It SHALL wait up to `NETDEV_WITNESS_WAIT` for a `pre-ack:` comment; absence SHALL be recorded as `fac_witness: unmeasured` and SHALL NOT block. Before applying it SHALL GET the whole target object and the list of `_id`s in its collection into `NETDEV_SNAPSHOT_DIR` (a failed GET aborts). After applying, within `NETDEV_HEALTH_TIMEOUT`, it SHALL verify the controller API, the Kubernetes API server (`kubectl get --raw /readyz`), the fields the tool sets reading back equal to `after` (not the whole object: fields such as `last_seen` change on their own; a command has no read-back), the three probes (two or more lost ⇒ failure; one lost ⇒ `outbound: degraded`), and — when a pre-ack arrived — a `post-ack:` comment (absent with probes answering ⇒ failure, `fac_witness: lost`). Any failure SHALL PUT the snapshot back through the controller and re-check. Because the legacy PUT merges (k8scc#67 finding 4), a field the tool set that the snapshot did not have can survive that PUT: it SHALL count as reverted only where a server table names it inert in the state read back (today `users.fixed_ip` with `use_fixedip` not true), and SHALL otherwise end the revert as `revert-failed` naming the field. A write the controller itself refuses (an HTTP status or `rc` not ok) SHALL end as `aborted` with nothing to revert; a transport error SHALL be treated as possibly applied. A command with no inverse (`restart_device`, `power_cycle_port`) SHALL end as `revert-failed` when health fails, saying it cannot be undone. For a write that created an object in a collection whose DELETE is not known to remove it (today every collection but `portforwards`: `DELETE rest/user` answered 404 on FAC1, finding 5), the revert SHALL send nothing and end `revert-failed`. Otherwise the revert SHALL DELETE only the `_id` the create response returned, and only when it is the sole `_id` in the collection absent from the snapshot's list; when any other new `_id` is present the server SHALL delete nothing and the result SHALL be `revert-failed`. The ticket SHALL be closed with a release comment carrying `outbound`, `fac_witness` and `result`; a failed revert SHALL be `result: revert-failed` and the tool response SHALL instruct im to write a dated inbox report immediately. No comment the server writes itself (release, `revert-failed`, or any other) SHALL begin with `pre-ack:` or `post-ack:`.

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
- **THEN** the server DELETEs only the `_id` the create response returned, which is the sole `_id` absent from the snapshot's list; the collection reads back equal to that list and the release reads `result: reverted` (negative control: health passing ⇒ no DELETE)

#### Scenario: A second new object blocks the delete
- **WHEN** a confirmed `set_port_forward` creates an object, another object then appears in the same collection on the fake controller (added by someone else), and the health check fails
- **THEN** the fake controller recorded zero DELETE requests, the release reads `result: revert-failed`, and the response carries the inbox instruction naming both new `_id`s

#### Scenario: A merged PUT's residue is judged, not ignored
- **WHEN** `set_dhcp_reservation` gives `u-tv` a `fixed_ip` it did not have, health fails, and the fake controller merges PUTs as FAC1 did
- **THEN** after the revert `fixed_ip` is still on the object with `use_fixedip` false, and the result is `reverted` naming it as left behind but switched off (negative controls: with no inert rule the same state is `revert-failed`; a port forward that gains `enabled` it lacked is `revert-failed`)

#### Scenario: Snapshot GET failure aborts
- **WHEN** the fake controller returns an error for the snapshot GET
- **THEN** no PUT is sent, the ticket is closed with `result: aborted`, and the response names the GET error

#### Scenario: Revert failure is reported, not swallowed
- **WHEN** the fake controller stops answering after accepting the write
- **THEN** the audit line is `revert-failed`, the ticket stays open with a `revert-failed` comment, and the response contains the inbox instruction with the snapshot digest

### Requirement: A target on im's own LAN path is refused, named, and fails closed on missing data

At each write the server SHALL compute from the session's reader data the networks containing `POD_IP`, the controller address and the API server endpoints; the switch port or AP the node is attached to; and every legacy firewall rule (groups expanded) and every zone-based policy (v2 `firewall-policies`: IP lists, network subnets, a zone's networks) whose expanded source/destination covers pod↔controller or pod↔API server by CIDR containment; a side it cannot expand covers. The policies SHALL be a required input: on a zone-based console the legacy rules are empty and the policies are the firewall (k8scc#67 finding 1). Zones are optional; unread, a side given only by its zone covers. A target in that set SHALL be refused naming the intersecting element before any ticket or token. If any of those inputs cannot be read, every write SHALL be refused with the reader error. `restart_device` SHALL refuse the gateway device (a device at a network's gateway address or the controller's, or whose type is `ugw`, `udm` or `uxg`, since legacy `stat/device` may report a gateway's WAN address). A switch the node is attached to SHALL be refused for every port while the controller does not say which port the node is on. A reservation SHALL be refused when its address is the node's, the controller's or an API server's. No tool SHALL exist for VLAN create/delete, gateway restart/firmware, WLAN passphrases or deletion, WAN settings, default firewall policies, site settings, or user/API-key management.

#### Scenario: Own network refused
- **WHEN** a write targets the network object containing `POD_IP`
- **THEN** it is refused naming that network, with no ticket and no PUT (positive control: an unrelated network reaches the token phase)

#### Scenario: Firewall group expansion
- **WHEN** a firewall rule's destination group contains the controller address
- **THEN** a write to that rule is refused (negative control: a group without it proceeds)

#### Scenario: Zone policy on im's path refused
- **WHEN** the legacy rules are empty and a policy's sides cover pod↔controller (Internal zone → Gateway zone, or LAN network → the controller's IP)
- **THEN** class A refuses that policy as a target, naming it (no shipped write tool addresses a firewall object; this guards the next one) (negative control: an IoT→IoT policy and a node→IoT policy pass)

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

For every write call the server SHALL write one JSON line to stdout with `ts`, `tool`, `target`, `phase` (`token`, `execute`, `refused`, `reverted`, `revert-failed`, `aborted`), `stated_reason`, `reason` or `rc`, `user`, `snapshot_digest`, `diff_digest`, `health` (the list of health checks that passed; empty before any check ran), `outbound`, `fac_witness` and `ticket`, and SHALL write nothing else to stdout — the HTTP server's access log included.

#### Scenario: Lines match phases
- **WHEN** a write goes token → execute → reverted on the fake controller because two probes were lost
- **THEN** stdout holds exactly three JSON lines for that target with those phases, identical `stated_reason`, `snapshot_digest` equal to the sha256 of the snapshot file, `health` empty on the `token` line and listing `controller`, `apiserver`, `readback` but not `probes` on the `execute` and `reverted` lines

#### Scenario: The access log does not reach stdout
- **WHEN** the server runs as its container does and receives HTTP requests
- **THEN** stdout holds no line (the access lines are on stderr), while the same server run as a plain FastMCP app puts each request's access line on stdout (positive control; measured by hand 2026-10-26, not in the automated controls)
