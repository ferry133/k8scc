#!/usr/bin/env python3
"""MCP Talos Server — read-only Talos diagnostics for a single client cluster.

Runs as its own sidecar container (see openspec/changes/insideman in jg-base),
isolated from the `app` container the terminal user/agent shell runs in. The
credential (TALOSCONFIG) is mounted only here and is scoped to the `os:reader`
Talos role, which the Talos API itself rejects any mutating call against —
this server exposes no mutating tools, by design and redundantly by the
credential's own role.

Start: python3 talos_mcp_server.py  (HTTP/SSE transport, binds 127.0.0.1 only)
Requires: TALOSCONFIG (path to an os:reader-scoped talosconfig file),
          TALOS_NODES (comma-separated node IPs for this cluster)
"""
import os
import subprocess

from mcp.server.fastmcp import FastMCP

TALOSCTL = "/usr/local/bin/talosctl"
TALOSCONFIG = os.environ["TALOSCONFIG"]
KNOWN_NODES = [ip.strip() for ip in os.environ.get("TALOS_NODES", "").split(",") if ip.strip()]
MCP_PORT = int(os.environ.get("TALOS_MCP_PORT", "8765"))

app = FastMCP("talos", host="127.0.0.1", port=MCP_PORT)


def _run(node: str, *args: str, timeout: int = 15) -> str:
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
