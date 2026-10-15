# talos-operator-escalation Specification

## Purpose
The Talos MCP server's operator-credential contract: the gated `reboot_node` tool (two phases, call-time allowlist, server-side guards, audit), the direct-LAN read fallback with its path markers, and credential expiry handling. Referenced by jg-base's `talos-mcp-sidecar` spec.
## Requirements
### Requirement: Two-phase confirmed graceful reboot

The Talos MCP server SHALL expose `reboot_node(node, reason, confirm)` only while a non-empty, unexpired operator credential is configured. A call without `confirm` SHALL perform no action and return a single-use token bound to `node` with a 120-second expiry, the exact command that a confirmed call will run, and the per-node cooldown it will start (`cooldown_after`); a call with a valid token SHALL run `talosctl --endpoints <node> --nodes <node> reboot` with the operator credential and invalidate the token. The server-side guards below (reason, quorum, cooldown) apply to both phases. No other mutating verb SHALL be reachable through any tool argument.

#### Scenario: First call returns a token and does nothing
- **WHEN** the agent calls `reboot_node(node="10.9.9.23", reason="…")`
- **THEN** no Talos API call is made, and the response carries a token, the node, `expires_in: 120`, `cooldown_after: 600`, and the literal command the confirmed call will execute

#### Scenario: Confirmed call reboots once
- **WHEN** the agent calls `reboot_node(node="10.9.9.23", reason="…", confirm=<that token>)` within 120 seconds
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

### Requirement: Caller states a reason

`reason` SHALL be a required argument of `reboot_node` in the MCP tool schema; a call whose `reason` is empty or whitespace SHALL be refused before any lookup, and the stated text SHALL be written as `stated_reason` on every audit line the call produces (token, execute, refused). The server records it and does not judge it: the reader of the reason is the company, afterwards, in `kubectl logs -c talos-mcp`. (`reason` on a refused line remains the refusal cause; the two are different fields.)

#### Scenario: Empty reason is refused first
- **WHEN** `reboot_node` is called with `reason=""` or whitespace
- **THEN** the call is refused with "reason is required", no `kubectl` or `talosctl` call is made, and the audit line carries `"stated_reason": ""`

#### Scenario: Stated reason reaches both audit lines
- **WHEN** a token is issued and then confirmed, each call carrying a reason
- **THEN** both the `token` and the `execute` audit lines carry that text under `stated_reason`

### Requirement: Control-plane quorum guard

Both phases SHALL refuse when any control-plane node *other than the target* (label `node-role.kubernetes.io/control-plane`) does not report `Ready=True` in the same `kubectl get nodes` reading that produced the allowlist, naming that node in the refusal. The target's own readiness is not consulted — a NotReady target is usually why it is being rebooted — and worker nodes do not count.

#### Scenario: Another control plane NotReady
- **WHEN** a control-plane node other than `node` reports `Ready=False`
- **THEN** the call is refused before any Talos API call, the refusal names that node and says "quorum", and the audit line records `phase: refused`

#### Scenario: Target NotReady, peers Ready
- **WHEN** only `node` itself reports `Ready=False`, or the NotReady node is a worker
- **THEN** the call proceeds to the token or execute phase

#### Scenario: Guard re-read at execute
- **WHEN** a token was issued while every other control plane was Ready and one has since gone NotReady
- **THEN** the confirmed call is refused and no reboot runs

### Requirement: Per-node cooldown

After a confirmed reboot is issued, the server SHALL refuse both phases for the same node for 600 seconds, counted from the moment the command was issued (so a second call during the first's `--wait` is inside the window). The refusal SHALL state the cooldown length and the seconds remaining; the token response SHALL carry the length as `cooldown_after`. Other nodes are unaffected. For other nodes the record lives in the sidecar's memory, so a sidecar restart starts with no history. For the node the sidecar runs on (`POD_IP`), the window SHALL instead be counted from that node's last boot (`/proc/uptime`), because the own-node reboot restarts the sidecar and erases the in-memory record; any boot counts, and a boot clock that cannot be read SHALL refuse the own node (fail closed) with the read error as the reason.

#### Scenario: Second reboot inside the window
- **WHEN** `reboot_node` is called for a node this sidecar rebooted fewer than 600 seconds ago
- **THEN** the call is refused with "cooldown is 600 s" and the seconds left, and no Talos API call is made

#### Scenario: Window over, or another node
- **WHEN** 600 seconds have passed, or the call names a different node
- **THEN** the call proceeds

#### Scenario: Own node after its own reboot
- **WHEN** `reboot_node` names `POD_IP` and that node booted fewer than 600 seconds ago, whatever the sidecar remembers
- **THEN** the call is refused in both phases, stating the seconds since boot and the seconds left, and no Talos API call is made

#### Scenario: Own node's boot clock unreadable
- **WHEN** `reboot_node` names `POD_IP` and `/proc/uptime` cannot be read or parsed
- **THEN** the call is refused naming the read error, and calls naming other nodes are unaffected

### Requirement: Own node is rebooted without waiting

When `node` equals the sidecar's own host address (`POD_IP`, since the pod is hostNetwork), the confirmed call SHALL issue the reboot with `--wait=false` and return immediately with a statement that this pod will restart with the node, and both the token response and the execute response SHALL say that im itself goes down with the node — on a single-node cluster the whole service — and that nobody can see the result until the pod is back; otherwise it SHALL use `--wait=true` with a 600-second timeout (`--timeout=600s`). The flag is spelled out because `talosctl reboot --wait` defaults to true (measured on talosctl 1.12): a bare `reboot` waits, so "without `--wait`" cannot mean omitting it.

#### Scenario: Rebooting the node this pod runs on
- **WHEN** a confirmed `reboot_node` targets `POD_IP`
- **THEN** the command runs with `--wait=false` and no `--timeout`, the response says the pod itself will restart and that im goes down with it (as did the token response), and no timeout error is produced

#### Scenario: Rebooting another node waits for it
- **WHEN** a confirmed `reboot_node` targets a different node
- **THEN** the command runs with `--wait=true --timeout=600s` and returns talosctl's sequence output when the node is back, or a timeout error after 600 seconds

### Requirement: Every mutating call is audited to the sidecar's stdout

Each `reboot_node` call — token issue, execute, or refusal — SHALL write one JSON line to stdout containing timestamp, tool, node, phase, the caller's `stated_reason`, reason (for refusals), exit code (for executes) and the `CLAUDE_USER_ID` of the sidecar's environment (empty when the pod spec does not set it — root's decision for the `talos-mcp` container, since the ttyd session's identity lives in `app` and does not cross), so that `kubectl logs -c talos-mcp` is a complete record of mutating activity, including the direct-LAN path that Omni never sees.

#### Scenario: Refused call is still logged
- **WHEN** a call is refused for any reason
- **THEN** a line with `"phase":"refused"` and the reason appears in the sidecar's stdout

#### Scenario: Executed call records the exit code
- **WHEN** a confirmed call runs talosctl
- **THEN** a line with `"phase":"execute"` and talosctl's exit code appears in the sidecar's stdout

### Requirement: Read tools fall back to the direct LAN path once

Each read-only tool SHALL first call through the Omni-proxied reader credential; when that attempt fails with a transport-class error (timeout, `Unavailable`, `no route to host`, `context deadline exceeded`) and an operator credential is configured, it SHALL retry exactly once directly against the named node with `--endpoints <node>` and the operator credential. Every read response that reached a Talos API SHALL begin with `[via Omni]` or `[via direct LAN]`, immediately after the expiry warning line when one is present. A refusal made before any path is attempted (reader credential not configured, `TALOS_NODES` miss) carries no marker — nothing was *via* anything, and a marker there would be a reading of a path that was never taken.

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

At startup the server SHALL read the operator credential's certificate expiry (`talosctl config info`, the `Certificate expires: … (YYYY-MM-DD)` line); an expired credential (date on or before today) SHALL be treated as absent with the expiry date recorded as the reason; a credential whose expiry cannot be read SHALL likewise be treated as absent with the read error as the reason (fail closed); and a credential expiring in fewer than 30 days SHALL cause every tool response to begin with a warning line naming the date.

#### Scenario: Near expiry warns on every response
- **WHEN** the operator credential expires in fewer than 30 days
- **THEN** every tool response starts with a line containing the expiry date and `talos-operator-credential rotate`

#### Scenario: Expired reads as absent
- **WHEN** the operator credential's certificate has expired
- **THEN** `reboot_node` is not registered and the fallback-unavailable message names the expiry date

#### Scenario: Unreadable expiry reads as absent
- **WHEN** the operator credential file is non-empty but `talosctl config info` fails or prints no expiry line
- **THEN** `reboot_node` is not registered and the fallback-unavailable message carries talosctl's error

