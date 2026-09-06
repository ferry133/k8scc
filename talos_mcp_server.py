#!/usr/bin/env python3
"""MCP Talos Server — read-only Talos diagnostics for a single client cluster.

Runs as its own sidecar container (see openspec/changes/insideman in jg-base),
isolated from the `app` container the terminal user/agent shell runs in. The
credential (TALOSCONFIG) is mounted only here and is scoped to the `os:reader`
Talos role, which the Talos API itself rejects any mutating call against —
this server exposes no mutating tools, by design and redundantly by the
credential's own role.

Start: python3 talos_mcp_server.py  (HTTP/SSE transport, binds 127.0.0.1 only)

Requires: TALOSCONFIG (path to an os:reader-scoped talosconfig file), plus
          OMNI_SERVICE_ACCOUNT_KEY and OMNI_ENDPOINT — the talosconfig carries
          no key material of its own, and talosctl's auth library reads those
          two directly from the environment. When any of the three is empty or
          absent the server still starts and serves, but every tool returns a
          "not configured on this cluster" message — the base `im` instance
          ships this sidecar fleet-wide, including clusters with no Omni.

Optional: TALOS_NODES (comma-separated node IPs). Rejects tool calls naming a
          node outside the list. This is a nicer error message, NOT a security
          boundary — the credential already resolves to exactly one cluster
          server-side, so an unknown IP fails at Omni regardless. Left unset by
          the jg-cluster-template pipeline on purpose: the list is hand-written
          for Omni-provisioned clusters (they render `nodes: []`), so it goes
          stale on node replacement and would then block diagnostics during the
          very incident this sidecar exists for. Set it only where the node
          addresses are genuinely fixed.
"""
import os
import subprocess

from mcp.server.fastmcp import FastMCP

TALOSCTL = "/usr/local/bin/talosctl"
TALOSCONFIG = os.environ.get("TALOSCONFIG", "")
KNOWN_NODES = [ip.strip() for ip in os.environ.get("TALOS_NODES", "").split(",") if ip.strip()]
MCP_PORT = int(os.environ.get("TALOS_MCP_PORT", "8765"))


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

app = FastMCP("talos", host="127.0.0.1", port=MCP_PORT)


def _run(node: str, *args: str, timeout: int = 15) -> str:
    if MISSING_CONFIG:
        return (
            "error: talos-mcp is not configured on this cluster — missing: "
            + ", ".join(MISSING_CONFIG)
            + ". Expected on clusters not managed by Omni. To enable: populate "
            "these values in cluster-secrets, then restart this pod (the SA "
            "key is env, a running container never sees the new value)."
        )
    if KNOWN_NODES and node not in KNOWN_NODES:
        return f"error: {node} is not a known node for this cluster ({', '.join(KNOWN_NODES)})"
    try:
        result = subprocess.run(
            [TALOSCTL, f"--talosconfig={TALOSCONFIG}", "--nodes", node, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"error: talosctl timed out after {timeout}s"
    output = result.stdout
    if result.returncode != 0:
        output += f"\n(exit {result.returncode}) {result.stderr}"
    return output.strip() or "(empty response)"


@app.tool()
def get_node_status(node: str) -> str:
    """查詢單一節點的 Talos 版本與連線狀態。node 為節點 IP。"""
    return _run(node, "version", "--short")


@app.tool()
def get_etcd_members(node: str) -> str:
    """查詢 etcd member 清單與健康狀態（透過任一存活節點即可看到全體）。node 為節點 IP。"""
    return _run(node, "etcd", "members")


@app.tool()
def get_link_status(node: str, link_id: str = "") -> str:
    """查詢節點的網路連結狀態，包含 siderolink/kubespan（Omni 管理通道）。
    node 為節點 IP；link_id 可選（例如 "siderolink"），留空則列出全部連結。"""
    args = ["get", "links"]
    if link_id:
        args.append(link_id)
    args += ["-o", "yaml"]
    return _run(node, *args)


@app.tool()
def get_service_logs(node: str, service: str, lines: int = 50) -> str:
    """查詢節點上某個 Talos 服務的近期 log（例如 etcd、kubelet、trustd）。
    node 為節點 IP；service 為服務名稱；lines 為要回傳的行數（預設 50）。"""
    return _run(node, "logs", service, "--tail", str(lines))


if __name__ == "__main__":
    app.run(transport="sse")
