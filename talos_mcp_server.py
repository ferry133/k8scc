#!/usr/bin/env python3
"""MCP Talos Server — Talos diagnostics for a single client cluster, plus one
gated mutating tool when an operator credential is mounted.

Runs as its own sidecar container (see openspec/changes/insideman and
openspec/changes/im-onsite-operator-tools in jg-base), isolated from the `app`
container the terminal user/agent shell runs in. Both credentials are mounted
only here; the `app` container never sees either.

Start: python3 talos_mcp_server.py  (HTTP/SSE transport, binds 127.0.0.1 only)

Requires: TALOSCONFIG (path to the Omni-issued os:reader talosconfig), plus
          OMNI_SERVICE_ACCOUNT_KEY and OMNI_ENDPOINT — the talosconfig carries
          no key material of its own, and talosctl's auth library reads those
          two directly from the environment. When any of the three is empty or
          absent the server still starts and serves, but every tool returns a
          "not configured on this cluster" message — the base `im` instance
          ships this sidecar fleet-wide, including clusters whose Omni
          credential was never populated.

Optional: TALOS_OPERATOR_CONFIG — path to a second talosconfig holding an
          os:operator certificate minted directly against the cluster's Talos
          CA (fleet-ops skill `talos-operator-credential`). It does not go
          through Omni. When it is present, non-empty and unexpired:
            * the four read tools retry ONCE over the direct LAN path after an
              Omni-path transport failure (timeout / Unavailable / no route to
              host / context deadline exceeded), and every read response that
              reached a Talos API says which path answered;
            * `reboot_node` is registered — a two-phase, token-confirmed,
              graceful reboot, allowlisted at call time against the cluster's
              own node list and audited to stdout.
          Absent, empty (0 bytes — a Secret key rendered from an unset value),
          unreadable or expired: behaviour is exactly the reader-only server,
          and the gap is named in the response that would have used it.

          POD_IP — the node this pod sits on (downward API status.podIP; the
          pod is hostNetwork so it is the node's own address). A confirmed
          reboot of that node is issued with --wait=false, because the waiter
          reboots with it. talosctl's --wait defaults to TRUE, so "without
          --wait" has to be spelled out; a bare command would wait and die.

          TALOS_NODES (comma-separated node IPs) — rejects read calls naming
          a node outside the list. A nicer error message, NOT a security
          boundary, and NOT consulted by reboot_node (its allowlist comes from
          the API server at call time). Left unset by the jg-cluster-template
          pipeline on purpose: a hand-written list goes stale on node
          replacement and then blocks diagnostics during the very incident
          this sidecar exists for.

          TALOS_MCP_READ_TIMEOUT — seconds per talosctl read attempt (15).

          CLAUDE_USER_ID — recorded in audit lines. This is the sidecar's own
          environment, set by the pod spec; it is NOT the ttyd session's
          identity, which lives in the `app` container and never crosses over.
"""
import datetime as _dt
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

# Resolved on PATH so the controls in test/ can stand a shim in front of the
# real binary; in the image both live in /usr/local/bin, which PATH covers.
TALOSCTL = shutil.which("talosctl") or "/usr/local/bin/talosctl"
KUBECTL = shutil.which("kubectl") or "/usr/local/bin/kubectl"

TALOSCONFIG = os.environ.get("TALOSCONFIG", "")
OPERATOR_CONFIG = os.environ.get("TALOS_OPERATOR_CONFIG", "")
POD_IP = os.environ.get("POD_IP", "").strip()
KNOWN_NODES = [ip.strip() for ip in os.environ.get("TALOS_NODES", "").split(",") if ip.strip()]
MCP_PORT = int(os.environ.get("TALOS_MCP_PORT", "8765"))
READ_TIMEOUT = int(os.environ.get("TALOS_MCP_READ_TIMEOUT", "15"))
USER_ID = os.environ.get("CLAUDE_USER_ID", "")

TOKEN_TTL = 120            # seconds a reboot token stays valid
REBOOT_WAIT_TIMEOUT = 600  # seconds to wait for another node to come back
REBOOT_NOWAIT_TIMEOUT = 30 # seconds for the fire-and-forget own-node case
REBOOT_COOLDOWN = 600      # seconds after an execute before the same node may be rebooted again:
                           # one --wait window, so a second call during the first's wait is inside it
EXPIRY_WARN_DAYS = 30

# Transport-class failures of the Omni-proxied path: the road, not the
# request. Anything else (PermissionDenied, a bad argument) is returned as-is
# — retrying it over another road would answer a question nobody asked.
TRANSPORT_ERROR_MARKERS = ("unavailable", "no route to host", "context deadline exceeded")

# Patch point for the controls in test/ (token expiry without sleeping).
_now = time.time

# The own node's boot clock. /proc/uptime is not namespaced by anything a
# kubelet sets up, so inside this container it reads the host's seconds since
# boot. The cooldown on the own node comes from here, not from memory: an
# own-node reboot restarts this process, so an in-memory record of it never
# outlives the event it records (fleet-ops-77, throwaway 3-CP, 2026-10-15).
# Patch point for the controls in test/.
UPTIME_FILE = "/proc/uptime"


def _missing_config() -> list[str]:
    missing = []
    if not TALOSCONFIG or not os.path.isfile(TALOSCONFIG) or os.path.getsize(TALOSCONFIG) == 0:
        missing.append("talosconfig file (TALOS_MCP_CONFIG_B64)")
    if not os.environ.get("OMNI_SERVICE_ACCOUNT_KEY", "").strip():
        missing.append("Omni SA key (TALOS_MCP_SA_KEY)")
    if not os.environ.get("OMNI_ENDPOINT", "").strip():
        missing.append("Omni endpoint (TALOS_MCP_OMNI_ENDPOINT)")
    return missing


# Checked once at startup, deliberately: OMNI_SERVICE_ACCOUNT_KEY arrives via
# env from a secretKeyRef, so a later secret edit cannot reach a running
# container anyway — a call-time re-check would report the mounted file as
# fixed while the env half stayed stale, which is worse than a consistent
# "restart the pod". This sidecar ships in the base `im` instance on every
# cluster, so on clusters without Omni credentials the server must still come
# up (probes pass, claude-session's TALOS_MCP_URL connects) and every tool
# answers with the message below instead of a bare talosctl auth error.
MISSING_CONFIG = _missing_config()


@dataclass(frozen=True)
class Result:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout: int = 0

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    @property
    def transport_error(self) -> bool:
        if self.timed_out:
            return True
        haystack = (self.stderr + "\n" + self.stdout).lower()
        return any(marker in haystack for marker in TRANSPORT_ERROR_MARKERS)

    def text(self) -> str:
        if self.timed_out:
            return f"error: talosctl timed out after {self.timeout}s"
        output = self.stdout
        if self.rc != 0:
            output += f"\n(exit {self.rc}) {self.stderr}"
        return output.strip() or "(empty response)"

    def transcript(self) -> str:
        """Everything the command said, with its exit code stated — for a verb
        whose progress goes to stderr and whose success is rc 0 with an empty
        stdout. `talosctl reboot --wait` is one: measured live on jg-jiahd
        (2026-10-15, k8scc#22), text() answered "(empty response)" for a reboot
        that had happened, and the agent had to go and look."""
        if self.timed_out:
            return f"error: talosctl timed out after {self.timeout}s"
        parts = [f"talosctl exit {self.rc}"]
        parts += [stream.strip() for stream in (self.stderr, self.stdout) if stream.strip()]
        return "\n".join(parts)


def _exec(argv: list[str], timeout: int) -> Result:
    """Run argv as given — every value is one argv element, never a shell word."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(-1, "", f"{os.path.basename(argv[0])} timed out after {timeout}s", timed_out=True, timeout=timeout)
    except OSError as exc:
        return Result(-1, "", f"cannot run {argv[0]}: {exc}")
    return Result(result.returncode, result.stdout, result.stderr)


# --- operator credential -----------------------------------------------------

@dataclass(frozen=True)
class OperatorState:
    status: str                      # absent | empty | unreadable | expired | ok
    reason: str                      # fills "direct-LAN fallback not available on this cluster (…)"
    not_after: "_dt.date | None" = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _operator_state(path: str, today: _dt.date) -> OperatorState:
    if not path:
        return OperatorState("absent", "no operator credential")
    if not os.path.isfile(path):
        return OperatorState("absent", f"no operator credential: {path} is not a file")
    if os.path.getsize(path) == 0:
        # A Secret key rendered from an unset value mounts as a 0-byte file;
        # the root spec says empty and absent are the same thing.
        return OperatorState("empty", f"no operator credential: {path} is empty")
    info = _exec([TALOSCTL, f"--talosconfig={path}", "config", "info"], timeout=15)
    match = re.search(r"^Certificate expires:.*\((\d{4}-\d{2}-\d{2})\)", info.stdout, re.M)
    if not info.ok or not match:
        # Fail closed: a credential whose expiry cannot be read is not used,
        # and the reason says so, instead of a tool that works until the day
        # it silently does not.
        detail = info.text().replace("\n", " ")
        return OperatorState("unreadable", f"operator credential expiry could not be read ({detail})")
    not_after = _dt.date.fromisoformat(match.group(1))
    if not_after <= today:
        return OperatorState("expired", f"operator credential expired on {not_after.isoformat()}", not_after)
    return OperatorState("ok", "", not_after)


_TODAY = _dt.datetime.now(_dt.timezone.utc).date()
OPERATOR = _operator_state(OPERATOR_CONFIG, _TODAY)

EXPIRY_WARNING = ""
if OPERATOR.ok and OPERATOR.not_after is not None:
    _days_left = (OPERATOR.not_after - _TODAY).days
    if _days_left < EXPIRY_WARN_DAYS:
        EXPIRY_WARNING = (
            f"⚠ operator credential expires {OPERATOR.not_after.isoformat()} "
            f"({_days_left} day(s) left) — run `talos-operator-credential rotate` "
            "(fleet-ops skill) before then; after that date reboot_node disappears "
            "and reads lose their direct-LAN fallback"
        )


def _respond(*lines: str) -> str:
    """Every tool response: optional expiry warning first, then the body lines."""
    out = [EXPIRY_WARNING] if EXPIRY_WARNING else []
    out.extend(line for line in lines if line)
    return "\n".join(out)


app = FastMCP("talos", host="127.0.0.1", port=MCP_PORT)


# --- read tools ---------------------------------------------------------------

def _read(node: str, *args: str) -> str:
    if MISSING_CONFIG:
        return _respond(
            "error: talos-mcp is not configured on this cluster — missing: "
            + ", ".join(MISSING_CONFIG)
            + ". This reports an unpopulated credential, not the absence of "
            "Omni: the sidecar ships fleet-wide, so a cluster that runs Omni "
            "itself or is managed by one lands here too when it was never "
            "onboarded. To enable: populate these values in cluster-secrets, "
            "then restart this pod (the SA key is env, a running container "
            "never sees the new value)."
        )
    if KNOWN_NODES and node not in KNOWN_NODES:
        return _respond(f"error: {node} is not a known node for this cluster ({', '.join(KNOWN_NODES)})")

    omni = _exec([TALOSCTL, f"--talosconfig={TALOSCONFIG}", "--nodes", node, *args], READ_TIMEOUT)
    if omni.ok or not omni.transport_error:
        return _respond("[via Omni]", omni.text())
    if not OPERATOR.ok:
        return _respond(
            "[via Omni]",
            omni.text(),
            f"direct-LAN fallback not available on this cluster ({OPERATOR.reason})",
        )
    direct = _exec(
        [TALOSCTL, f"--talosconfig={OPERATOR_CONFIG}", "--endpoints", node, "--nodes", node, *args],
        READ_TIMEOUT,
    )
    if direct.ok:
        return _respond("[via direct LAN]", direct.text())
    return _respond(
        "[via direct LAN]",
        direct.text(),
        f"(Omni path had failed first: {omni.text().replace(chr(10), ' ')})",
    )


@app.tool()
def get_node_status(node: str) -> str:
    """查詢單一節點的 Talos 版本與連線狀態。node 為節點 IP。"""
    return _read(node, "version", "--short")


@app.tool()
def get_etcd_members(node: str) -> str:
    """查詢 etcd member 清單與健康狀態（透過任一存活節點即可看到全體）。node 為節點 IP。"""
    return _read(node, "etcd", "members")


@app.tool()
def get_link_status(node: str, link_id: str = "") -> str:
    """查詢節點的網路連結狀態，包含 siderolink/kubespan（Omni 管理通道）。
    node 為節點 IP；link_id 可選（例如 "siderolink"），留空則列出全部連結。"""
    args = ["get", "links"]
    if link_id:
        args.append(link_id)
    args += ["-o", "yaml"]
    return _read(node, *args)


@app.tool()
def get_service_logs(node: str, service: str, lines: int = 50) -> str:
    """查詢節點上某個 Talos 服務的近期 log（例如 etcd、kubelet、trustd）。
    node 為節點 IP；service 為服務名稱；lines 為要回傳的行數（預設 50）。"""
    return _read(node, "logs", service, "--tail", str(lines))


# --- reboot_node ------------------------------------------------------------------

_TOKENS: dict[str, tuple[str, float]] = {}   # token -> (node, expires_at)


@dataclass(frozen=True)
class NodeInfo:
    name: str
    ip: str
    control_plane: bool
    ready: bool


def _cluster_inventory() -> "tuple[dict[str, NodeInfo] | None, str]":
    """The cluster's nodes right now, keyed by InternalIP, via this pod's own SA.
    One `kubectl get nodes -o json` serves both the allowlist and the quorum
    guard, so the two never disagree about what the cluster looked like."""
    result = _exec([KUBECTL, "get", "nodes", "-o", "json"], timeout=20)
    if not result.ok:
        return None, (result.stderr or result.stdout).strip() or f"kubectl exit {result.rc}"
    try:
        items = json.loads(result.stdout)["items"]
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"kubectl get nodes did not return a NodeList ({exc})"
    inventory: dict[str, NodeInfo] = {}
    for item in items:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        ips = [a.get("address") for a in status.get("addresses") or [] if a.get("type") == "InternalIP"]
        if not ips:
            continue   # unaddressable: neither a reboot target nor checkable
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        info = NodeInfo(name=meta.get("name") or ips[0], ip=ips[0],
                        control_plane="node-role.kubernetes.io/control-plane" in (meta.get("labels") or {}),
                        ready=ready)
        for ip in ips:
            inventory[ip] = info
    return inventory, ""


_LAST_EXECUTE: dict[str, float] = {}   # node -> _now() at its last execute (in-memory: a sidecar restart forgets it)


def _quorum_block(node: str, inventory: "dict[str, NodeInfo]") -> str:
    """The first *other* control-plane node that is not Ready, as 'name (ip)', else ''.
    The target itself may be NotReady -- that is usually why it is being rebooted."""
    for info in sorted({i.ip: i for i in inventory.values()}.values(), key=lambda i: i.ip):
        if info.control_plane and info.ip != node and not info.ready:
            return f"{info.name} ({info.ip})"
    return ""


def _cooldown_left(node: str, now: float) -> int:
    last = _LAST_EXECUTE.get(node)
    return 0 if last is None else max(0, int(REBOOT_COOLDOWN - (now - last)))


def _host_uptime() -> "tuple[float | None, str]":
    """Seconds since this pod's own node booted, or (None, why not)."""
    try:
        with open(UPTIME_FILE, encoding="ascii") as fh:
            return float(fh.read().split()[0]), ""
    except (OSError, ValueError, IndexError) as exc:
        return None, f"{UPTIME_FILE}: {exc}"


def _own_node_cooldown_refusal(node: str) -> str:
    """The own node's cooldown, counted from its last boot rather than from this
    sidecar's memory, which that boot erased. Any boot counts, not only one this
    tool ordered: rebooting a node again within one --wait window of it coming up
    is the loop the cooldown exists to stop. An unreadable clock refuses (fail
    closed), naming why -- the same rule as an unreadable credential expiry."""
    uptime, err = _host_uptime()
    if uptime is None:
        return f"cannot read this node's boot clock, refusing (fail closed): {err}"
    if uptime < REBOOT_COOLDOWN:
        left = int(REBOOT_COOLDOWN - uptime)
        return (f"{node} is this pod's own node and booted {int(uptime)} s ago; "
                f"cooldown is {REBOOT_COOLDOWN} s from boot, {left} s left. Any boot counts, not only one "
                "reboot_node ordered -- a node back from a power loss included: rebooting a node again "
                "within one --wait window of it coming up is the loop this cooldown exists to stop, and "
                "the sidecar cannot tell which kind of boot it was")
    return ""


_AUDIT_OUT = None   # the real stdout; __main__ points sys.stdout at stderr


def _audit(**fields: object) -> None:
    """One JSON line per reboot_node call to stdout — `kubectl logs -c talos-mcp`
    is the only record of the direct-LAN path, which Omni never sees."""
    line = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "tool": "reboot_node", "user": USER_ID}
    line.update(fields)
    print(json.dumps(line, ensure_ascii=False), file=_AUDIT_OUT or sys.stdout, flush=True)


def _refuse(node: str, reason: str, **extra: object) -> str:
    _audit(node=node, phase="refused", reason=reason, **extra)
    return _respond(f"refused: {reason}")


# Nothing here cordons or uncordons. On jg-jiahd (2026-10-15, k8scc#22) the node
# came back Ready but SchedulingDisabled; the events show it had been cordoned
# four minutes before the token was issued, by someone outside this tool, and
# that cordon survived the reboot (fleet-ops uncordoned by hand 19 min later).
# Whether Talos undoes its *own* graceful-reboot cordon at boot is unmeasured
# (root 5.5, throwaway cluster) -- so the note asserts neither; it says what to
# check before uncordoning.
CORDON_NOTE = ("If the node comes back SchedulingDisabled, find out who cordoned it before you "
               "uncordon (`kubectl get events` and the node's annotations): a cordon that was "
               "already on the node before the reboot survives it, and reboot_node never uncordons.")


OWN_NODE_NOTE = ("This is the node im itself runs on: rebooting it takes im down with it -- on a "
                 "single-node cluster that is the whole service -- and nobody can see the result "
                 "until the pod is back.")


def _reboot_argv(node: str) -> list[str]:
    argv = [TALOSCTL, f"--talosconfig={OPERATOR_CONFIG}", "--endpoints", node, "--nodes", node, "reboot"]
    if node == POD_IP:
        argv.append("--wait=false")
    else:
        argv += ["--wait=true", f"--timeout={REBOOT_WAIT_TIMEOUT}s"]
    return argv


def reboot_node(node: str, reason: str, confirm: str = "") -> str:
    """優雅重啟一個節點（graceful reboot），兩段式確認，守衛在伺服端。
    第一段：node 與 reason → 不動作，回傳一次性 token（120 秒內有效）與將執行的指令——
    這是 agent 自己重讀 will_run 與 reason 的第二眼，不是交給人按。第二段：加
    confirm=<token> → 真的執行，token 作廢。
    reason 必填，寫進每一行 audit。守衛（兩段都查）：node 必須是叢集當下某個節點的
    InternalIP；其他 control-plane 節點任一不 Ready 就拒（quorum）；同一節點在上一次
    執行後 600 秒內再呼叫被拒（冷卻）；本 pod 所在的節點則從它上次開機起算 600 秒——
    那次重啟會連同記憶一起抹掉。重啟本 pod 所在的節點時不等待（pod 會跟著重開）。"""
    stated = reason.strip()
    if not stated:
        return _refuse(node, "reason is required: state the diagnosis that calls for this reboot", stated_reason=stated)
    inventory, err = _cluster_inventory()
    if inventory is None:
        return _refuse(node, f"node inventory unavailable, refusing (fail closed): kubectl get nodes: {err}", stated_reason=stated)
    if node not in inventory:
        return _refuse(node, f"{node!r} is not an InternalIP of any node currently in this cluster ({', '.join(sorted(inventory)) or 'no nodes returned'})", stated_reason=stated)
    blocked_by = _quorum_block(node, inventory)
    if blocked_by:
        return _refuse(node, f"control-plane node {blocked_by} is not Ready; rebooting {node} now would risk etcd quorum (quorum guard)", stated_reason=stated)
    now = _now()
    left = _cooldown_left(node, now)
    if left:
        return _refuse(node, f"{node} was rebooted by this sidecar {REBOOT_COOLDOWN - left} s ago; cooldown is {REBOOT_COOLDOWN} s, {left} s left", stated_reason=stated)
    own_node = node == POD_IP
    if own_node:
        booted = _own_node_cooldown_refusal(node)
        if booted:
            return _refuse(node, booted, stated_reason=stated)

    if not confirm:
        token = secrets.token_urlsafe(24)
        _TOKENS[token] = (node, now + TOKEN_TTL)
        _audit(node=node, phase="token", stated_reason=stated)
        note = ("No action was taken. Re-read will_run against your reason; if it still holds, "
                f"call reboot_node(node, reason, confirm=<token>) within {TOKEN_TTL} s to execute. "
                "The token is single-use and bound to this node; the guards (control-plane quorum, "
                f"{REBOOT_COOLDOWN} s per-node cooldown) are checked again at execute. " + CORDON_NOTE)
        if own_node:
            note += " " + OWN_NODE_NOTE
        body = {
            "token": token,
            "node": node,
            "expires_in": TOKEN_TTL,
            "cooldown_after": REBOOT_COOLDOWN,
            "will_run": " ".join(_reboot_argv(node)),
            "note": note,
        }
        _prune_tokens(now, keep=token)
        return _respond(json.dumps(body, indent=2))

    entry = _TOKENS.get(confirm)
    if entry is None:
        _prune_tokens(now)
        return _refuse(node, "unknown or already-used token — call reboot_node(node, reason) again for a fresh one", stated_reason=stated)
    token_node, expires_at = entry
    if token_node != node:
        _prune_tokens(now, keep=confirm)
        return _refuse(node, f"token was issued for {token_node}, not {node}", stated_reason=stated)
    if now > expires_at:
        del _TOKENS[confirm]
        _prune_tokens(now)
        return _refuse(node, f"token expired ({TOKEN_TTL} s) — call reboot_node(node, reason) again for a fresh one", stated_reason=stated)

    # Consume before running: a crash mid-run must not leave it replayable. The
    # cooldown clock starts here too, so a second call during a long --wait is
    # already inside the window.
    del _TOKENS[confirm]
    _prune_tokens(now)
    _LAST_EXECUTE[node] = now
    argv = _reboot_argv(node)
    result = _exec(argv, REBOOT_NOWAIT_TIMEOUT if own_node else REBOOT_WAIT_TIMEOUT)
    _audit(node=node, phase="execute", rc=result.rc, wait=not own_node, command=" ".join(argv), stated_reason=stated)
    lines = []
    if own_node:
        lines.append(
            f"reboot ordered with --wait=false: this pod runs on {node} and will restart "
            "with it; once the terminal is back, confirm with get_node_status. " + OWN_NODE_NOTE
        )
    lines.append(result.transcript())
    lines.append(CORDON_NOTE)
    return _respond(*lines)


def _prune_tokens(now: float, keep: "str | None" = None) -> None:
    for token, (_, expires_at) in list(_TOKENS.items()):
        if token != keep and now > expires_at:
            del _TOKENS[token]


# Registered only when the credential that can carry it is usable. There is no
# "reboot_node is present but refuses" state: absent credential, absent tool.
if OPERATOR.ok:
    app.tool()(reboot_node)


# --- startup readings ------------------------------------------------------------

def _startup_readings() -> list[str]:
    readings = [
        "reader credential: " + ("ok" if not MISSING_CONFIG else "missing " + ", ".join(MISSING_CONFIG)),
        "operator credential: " + (
            f"ok, expires {OPERATOR.not_after.isoformat()}" if OPERATOR.ok else f"{OPERATOR.status} ({OPERATOR.reason})"
        ),
        "reboot_node: " + ("registered" if OPERATOR.ok else "not registered"),
    ]
    if OPERATOR.ok:
        uptime, err = _host_uptime()
        readings.append("reboot_node guards: stated reason required; other control-plane nodes must be Ready "
                        f"(quorum); {REBOOT_COOLDOWN} s per-node cooldown; own node ({POD_IP or 'POD_IP unset'}) "
                        "counted from boot, uptime " + (f"{int(uptime)} s" if uptime is not None else f"unreadable ({err})"))
    if EXPIRY_WARNING:
        readings.append(EXPIRY_WARNING)
    # Informational: reboot_node's allowlist needs `get nodes` through this
    # pod's SA. Measured here once so a missing automount shows up at startup
    # rather than as "refused" on the first reboot; it gates nothing.
    can_i = _exec([KUBECTL, "auth", "can-i", "list", "nodes"], timeout=20)
    readings.append("kubectl auth can-i list nodes: " + (can_i.stdout.strip() or can_i.text().replace("\n", " ")))
    return readings


if __name__ == "__main__":
    # stdout is the audit stream and nothing else: keep it for _audit, and give
    # everyone else — uvicorn's access log is a StreamHandler on
    # ext://sys.stdout — stderr instead (k8scc#46, the same swap as netdev-mcp).
    _AUDIT_OUT = sys.stdout
    sys.stdout = sys.stderr
    for reading in _startup_readings():
        print(f"talos-mcp: {reading}", file=sys.stderr, flush=True)
    app.run(transport="sse")
