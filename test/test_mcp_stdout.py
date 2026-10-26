#!/usr/bin/env python3
"""Controls: each MCP sidecar's stdout carries its audit lines and nothing else.

uvicorn's access log is a StreamHandler on ext://sys.stdout, so a FastMCP app
run as-is puts one `INFO: … "POST /messages/…"` line on stdout per request —
mixed into the audit stream that `kubectl logs -c <sidecar>` is the only record
of. Each server's __main__ keeps the real stdout for _audit and points
sys.stdout at stderr (k8scc#46 for talos-mcp, k8scc#45 for netdev-mcp).

Every case runs the server as a real process:
  - as shipped (python3 <server>.py, configured so its mutating tool is
    registered): one MCP session over SSE calls that tool in a way that is
    refused before anything acts — talos reboot_node on an IP outside the
    allowlist, netdev block_client with a blank reason. stdout must be exactly
    that one audit line (phase refused), and the access lines must be on stderr.
    Each half catches what the other cannot: stdout empty passes a server that
    sent its audit to stderr too (the two __main__ lines swapped — fleet-ops-65
    measured that every other control here stayed green, k8scc#48), and the
    stderr half fails a server that never logged the request at all.
  - positive control (import the module, app.run() without __main__): the
    access line on stdout. If this stops holding, uvicorn no longer logs to
    stdout and the as-shipped case proves nothing — fail rather than pass.
"""

import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))
import test_netdev_mcp_server as netdev_controls   # noqa: E402  (fakes and harness; also prepends its shims to PATH)
import test_talos_mcp_server as talos_controls     # noqa: E402

from mcp import ClientSession                      # noqa: E402
from mcp.client.sse import sse_client              # noqa: E402

SERVERS = {
    # name: (module, port env, shims dir)
    "talos": ("talos_mcp_server", "TALOS_MCP_PORT", HERE / "talos-mcp-shims"),
    "netdev": ("netdev_mcp_server", "NETDEV_MCP_PORT", HERE / "netdev-mcp-shims"),
}
ACCESS = '"POST /messages/'


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(proc, port, name):
    deadline = time.monotonic() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                out, err = proc.communicate(timeout=5) if proc.poll() is not None else ("", "")
                raise AssertionError(f"{name} never listened on {port}: rc={proc.poll()} stderr={err[-2000:]!r}")
            time.sleep(0.2)


async def call_tool(port, tool, args):
    async with sse_client(f"http://127.0.0.1:{port}/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return "".join(getattr(c, "text", "") for c in result.content)


def run(name, shipped, env=None, tool=None, args=None):
    """Start the server, send one request, stop it; (stdout, stderr, tool answer)."""
    module, port_env, shims = SERVERS[name]
    port = free_port()
    env = dict(env if env is not None else os.environ)
    env["PATH"] = f"{shims}{os.pathsep}{env.get('PATH', '')}"   # this server's shims first: both ship a kubectl
    env["PYTHONUNBUFFERED"] = "1"
    env[port_env] = str(port)
    if shipped:
        argv = [sys.executable, str(ROOT / f"{module}.py")]
    else:
        argv = [sys.executable, "-c", f"import {module} as m; m.app.run(transport='sse')"]
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    answer = ""
    try:
        wait_listening(proc, port, name)
        if tool:
            answer = asyncio.run(call_tool(port, tool, args))
        else:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/messages/?session_id=0", data=b"{}",
                                         method="POST", headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5).close()
            except urllib.error.HTTPError:
                pass   # 400 is fine: the request reached uvicorn, which is all the access log needs
        time.sleep(0.5)
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    return out, err, answer


class Stdout(unittest.TestCase):
    def check(self, name, env, tool, args, registered):
        out, _, _ = run(name, shipped=False, env={k: v for k, v in env.items() if k == "PATH"} | {"HOME": env.get("HOME", "")})
        self.assertIn(ACCESS, out, f"{name} positive control: uvicorn no longer logs to stdout, so the shipped case "
                                   f"cannot discriminate (stdout={out!r})")
        out, err, answer = run(name, shipped=True, env=env, tool=tool, args=args)
        self.assertIn(registered, err, f"{name}: the mutating tool was not registered, so no audit line can be asked for")
        self.assertIn("refused", answer.lower(), answer)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, f"{name} as shipped: stdout must be exactly the one audit line, got {out!r}")
        audit = json.loads(lines[0])
        self.assertEqual((audit["tool"], audit["phase"]), (tool, "refused"), audit)
        self.assertIn(ACCESS, err, f"{name} as shipped: the access lines must reach stderr, or nothing was logged")
        self.assertNotIn(lines[0], err, f"{name}: the audit line also went to stderr")

    def test_talos(self):
        h = talos_controls.Harness()
        try:
            h.load()
            self.check("talos", dict(os.environ), "reboot_node",
                       {"node": talos_controls.STRANGER, "reason": talos_controls.REASON}, "reboot_node: registered")
        finally:
            h.close()

    def test_netdev(self):
        h = netdev_controls.Harness()
        try:
            h.load(write=True)
            self.check("netdev", dict(os.environ), "block_client",
                       {"mac": "bb:bb:bb:00:01:30", "reason": " "}, "write tools: registered")
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
