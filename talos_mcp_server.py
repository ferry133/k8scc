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
EXPIRY_WARN_DAYS = 30

# Transport-class failures of the Omni-proxied path: the road, not the
# request. Anything else (PermissionDenied, a bad argument) is returned as-is
# — retrying it over another road would answer a question nobody asked.
TRANSPORT_ERROR_MARKERS = ("unavailable", "no route to host", "context deadline exceeded")

# Patch point for the controls in test/ (token expiry without sleeping).
_now = time.time


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


def _cluster_nodes() -> "tuple[set[str] | None, str]":
    """InternalIPs of the nodes in the cluster right now, via this pod's own SA."""
    jsonpath = '{.items[*].status.addresses[?(@.type=="InternalIP")].address}'
    result = _exec([KUBECTL, "get", "nodes", "-o", f"jsonpath={jsonpath}"], timeout=20)
    if not result.ok:
        return None, (result.stderr or result.stdout).strip() or f"kubectl exit {result.rc}"
    return set(result.stdout.split()), ""


def _audit(**fields: object) -> None:
    """One JSON line per reboot_node call to stdout — `kubectl logs -c talos-mcp`
    is the only record of the direct-LAN path, which Omni never sees."""
    line = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "tool": "reboot_node", "user": USER_ID}
    line.update(fields)
    print(json.dumps(line, ensure_ascii=False), flush=True)


def _refuse(node: str, reason: str) -> str:
    _audit(node=node, phase="refused", reason=reason)
    return _respond(f"refused: {reason}")


def _reboot_argv(node: str) -> list[str]:
    argv = [TALOSCTL, f"--talosconfig={OPERATOR_CONFIG}", "--endpoints", node, "--nodes", node, "reboot"]
    if node == POD_IP:
        argv.append("--wait=false")
    else:
        argv += ["--wait=true", f"--timeout={REBOOT_WAIT_TIMEOUT}s"]
    return argv


def reboot_node(node: str, confirm: str = "") -> str:
    """優雅重啟一個節點（graceful reboot），兩段式確認。
    第一段：只給 node → 不動作，回傳一次性 token（120 秒內有效）與將執行的指令；
    把它拿給終端機前的人看。第二段：node 加 confirm=<token> → 真的執行，token 作廢。
    node 必須是叢集當下某個節點的 InternalIP（每次呼叫向 API server 查）。
    重啟本 pod 所在的節點時不等待（pod 會跟著重開）。"""
    allowed, err = _cluster_nodes()
    if allowed is None:
        return _refuse(node, f"node allowlist unavailable, refusing (fail closed): kubectl get nodes: {err}")
    if node not in allowed:
        return _refuse(node, f"{node!r} is not an InternalIP of any node currently in this cluster ({', '.join(sorted(allowed)) or 'no nodes returned'})")

    now = _now()
    if not confirm:
        token = secrets.token_urlsafe(24)
        _TOKENS[token] = (node, now + TOKEN_TTL)
        _audit(node=node, phase="token")
        body = {
            "token": token,
            "node": node,
            "expires_in": TOKEN_TTL,
            "will_run": " ".join(_reboot_argv(node)),
            "note": "No action was taken. Show this to the person at the terminal; "
                    f"call reboot_node(node, confirm=<token>) within {TOKEN_TTL} s to execute. "
                    "The token is single-use and bound to this node.",
        }
        _prune_tokens(now, keep=token)
        return _respond(json.dumps(body, indent=2))

    entry = _TOKENS.get(confirm)
    if entry is None:
        _prune_tokens(now)
        return _refuse(node, "unknown or already-used token — call reboot_node(node) again for a fresh one")
    token_node, expires_at = entry
    if token_node != node:
        _prune_tokens(now, keep=confirm)
        return _refuse(node, f"token was issued for {token_node}, not {node}")
    if now > expires_at:
        del _TOKENS[confirm]
        _prune_tokens(now)
        return _refuse(node, f"token expired ({TOKEN_TTL} s) — call reboot_node(node) again for a fresh one")

    # Consume before running: a crash mid-run must not leave it replayable.
    del _TOKENS[confirm]
    _prune_tokens(now)
    argv = _reboot_argv(node)
    own_node = node == POD_IP
    result = _exec(argv, REBOOT_NOWAIT_TIMEOUT if own_node else REBOOT_WAIT_TIMEOUT)
    _audit(node=node, phase="execute", rc=result.rc, wait=not own_node, command=" ".join(argv))
    lines = []
    if own_node:
        lines.append(
            f"reboot ordered with --wait=false: this pod runs on {node} and will restart "
            "with it; once the terminal is back, confirm with get_node_status."
        )
    lines.append(result.text())
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
    if EXPIRY_WARNING:
        readings.append(EXPIRY_WARNING)
    # Informational: reboot_node's allowlist needs `get nodes` through this
    # pod's SA. Measured here once so a missing automount shows up at startup
    # rather than as "refused" on the first reboot; it gates nothing.
    can_i = _exec([KUBECTL, "auth", "can-i", "list", "nodes"], timeout=20)
    readings.append("kubectl auth can-i list nodes: " + (can_i.stdout.strip() or can_i.text().replace("\n", " ")))
    return readings


if __name__ == "__main__":
    for reading in _startup_readings():
        print(f"talos-mcp: {reading}", file=sys.stderr, flush=True)
    app.run(transport="sse")
