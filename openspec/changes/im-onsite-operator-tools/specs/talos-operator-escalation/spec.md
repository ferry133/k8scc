## ADDED Requirements

### Requirement: Two-phase confirmed graceful reboot

The Talos MCP server SHALL expose `reboot_node(node, confirm)` only while a non-empty, unexpired operator credential is configured. A call without `confirm` SHALL perform no action and return a single-use token bound to `node` with a 120-second expiry plus the exact command that a confirmed call will run; a call with a valid token SHALL run `talosctl --endpoints <node> --nodes <node> reboot` with the operator credential and invalidate the token. No other mutating verb SHALL be reachable through any tool argument.

#### Scenario: First call returns a token and does nothing
- **WHEN** the agent calls `reboot_node(node="10.9.9.23")`
- **THEN** no Talos API call is made, and the response carries a token, the node, `expires_in: 120`, and the literal command the confirmed call will execute

#### Scenario: Confirmed call reboots once
- **WHEN** the agent calls `reboot_node(node="10.9.9.23", confirm=<that token>)` within 120 seconds
- **THEN** exactly one `talosctl … reboot` is executed against that node, the response contains talosctl's output and exit code, and the same token is refused if presented again

#### Scenario: Token bound to node and time
- **WHEN** a token issued for one node is presented with a different node, or after 120 seconds
- **THEN** the call is refused with a reason naming the mismatch or expiry, and no Talos API call is made

#### Scenario: Argument injection cannot change the verb
- **WHEN** `node` or `confirm` contains shell metacharacters, flags, or additional words
- **THEN** the value is passed as a single argv element to `talosctl` or rejected by the allowlist; the subcommand remains the literal `reboot`

### Requirement: Node allowlist is derived at call time from the cluster

Every `reboot_node` call SHALL resolve the set of permitted nodes at that moment from the Kubernetes API (`get nodes`, InternalIP addresses) using the pod's own ServiceAccount, and SHALL refuse any `node` outside that set. If the lookup fails the call SHALL be refused (fail closed) with the lookup error, never falling back to a static list.

#### Scenario: Node in cluster is accepted
- **WHEN** `node` equals an InternalIP of a node currently in the cluster
- **THEN** the call proceeds to the token or execute phase

#### Scenario: Node outside cluster is refused
- **WHEN** `node` is any address not currently an InternalIP of a cluster node
- **THEN** the call is refused before any Talos API call and the audit line records `phase: refused`

#### Scenario: API server unreachable fails closed
- **WHEN** `kubectl get nodes` fails
- **THEN** the call is refused with the kubectl error text; no reboot is attempted

### Requirement: Own node is rebooted without waiting

When `node` equals the sidecar's own host address (`POD_IP`, since the pod is hostNetwork), the confirmed call SHALL issue the reboot without `--wait` and return immediately with a statement that this pod will restart with the node; otherwise it SHALL use `--wait` with a 600-second timeout.

#### Scenario: Rebooting the node this pod runs on
- **WHEN** a confirmed `reboot_node` targets `POD_IP`
- **THEN** the command runs without `--wait`, the response says the pod itself will restart, and no timeout error is produced

#### Scenario: Rebooting another node waits for it
- **WHEN** a confirmed `reboot_node` targets a different node
- **THEN** the command runs with `--wait` and returns talosctl's sequence output when the node is back, or a timeout error after 600 seconds

### Requirement: Every mutating call is audited to the sidecar's stdout

Each `reboot_node` call — token issue, execute, or refusal — SHALL write one JSON line to stdout containing timestamp, tool, node, phase, reason (for refusals), exit code (for executes) and the `CLAUDE_USER_ID` of the session, so that `kubectl logs -c talos-mcp` is a complete record of mutating activity, including the direct-LAN path that Omni never sees.

#### Scenario: Refused call is still logged
- **WHEN** a call is refused for any reason
- **THEN** a line with `"phase":"refused"` and the reason appears in the sidecar's stdout

#### Scenario: Executed call records the exit code
- **WHEN** a confirmed call runs talosctl
- **THEN** a line with `"phase":"execute"` and talosctl's exit code appears in the sidecar's stdout

### Requirement: Read tools fall back to the direct LAN path once

Each read-only tool SHALL first call through the Omni-proxied reader credential; when that attempt fails with a transport-class error (timeout, `Unavailable`, `no route to host`, `context deadline exceeded`) and an operator credential is configured, it SHALL retry exactly once directly against the named node with `--endpoints <node>` and the operator credential. The first line of every read response SHALL be `[via Omni]` or `[via direct LAN]`.

#### Scenario: Omni path works
- **WHEN** the Omni-proxied call succeeds
- **THEN** the response begins `[via Omni]` and no direct attempt is made

#### Scenario: Omni path down, direct path answers
- **WHEN** the Omni-proxied call fails with a transport-class error and an operator credential is configured
- **THEN** the response begins `[via direct LAN]` and carries the node's answer

#### Scenario: Permission errors are not retried
- **WHEN** the Omni-proxied call fails with `PermissionDenied` or an argument error
- **THEN** no direct attempt is made and the error is returned as-is

#### Scenario: No operator credential names the gap
- **WHEN** the Omni-proxied call fails with a transport-class error and no operator credential is configured
- **THEN** the response carries the Omni error and the sentence "direct-LAN fallback not available on this cluster (no operator credential)"

### Requirement: Operator credential expiry is surfaced

At startup the server SHALL read the operator credential's certificate expiry; an expired credential SHALL be treated as absent with the expiry date recorded as the reason, and a credential expiring in fewer than 30 days SHALL cause every tool response to begin with a warning line naming the date.

#### Scenario: Near expiry warns on every response
- **WHEN** the operator credential expires in fewer than 30 days
- **THEN** every tool response starts with a line containing the expiry date and `talos-operator-credential rotate`

#### Scenario: Expired reads as absent
- **WHEN** the operator credential's certificate has expired
- **THEN** `reboot_node` is not registered and the fallback-unavailable message names the expiry date
