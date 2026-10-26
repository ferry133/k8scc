#!/usr/bin/env python3
"""Controls: each MCP sidecar's stdout carries its audit lines and nothing else.

uvicorn's access log is a StreamHandler on ext://sys.stdout, so a FastMCP app
run as-is puts one `INFO: … "POST /messages/…"` line on stdout per request —
mixed into the audit stream that `kubectl logs -c <sidecar>` is the only record
of. Each server's __main__ keeps the real stdout for _audit and points
sys.stdout at stderr (k8scc#46 for talos-mcp, k8scc#45 for netdev-mcp).

Every case runs the server as a real process and sends one request:
  - as shipped (python3 <server>.py): stdout empty, the access line on stderr.
    The stderr half is what makes this discriminate: a server that never logged
    the request would also leave stdout empty.
  - positive control (import the module, app.run() without __main__): the
    access line on stdout. If this stops holding, uvicorn no longer logs to
    stdout and the as-shipped case proves nothing — fail rather than pass.

The audit side (that _audit still reaches the real stdout after the swap) is
covered in each server's own controls.
"""

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

SERVERS = {
    # name: (module, port env, shims dir, env that keeps startup off the network)
    "talos": ("talos_mcp_server", "TALOS_MCP_PORT", HERE / "talos-mcp-shims", {}),
    "netdev": ("netdev_mcp_server", "NETDEV_MCP_PORT", HERE / "netdev-mcp-shims",
               {"UNIFI_CONTROLLER_URL": "", "NETDEV_ALLOW_INSECURE_HTTP": ""}),
}
ACCESS = '"POST /messages/'


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(name, shipped):
    module, port_env, shims, extra = SERVERS[name]
    port = free_port()
    env = dict(os.environ, PATH=f"{shims}{os.pathsep}{os.environ.get('PATH', '')}", PYTHONUNBUFFERED="1")
    env.update(extra)
    env[port_env] = str(port)
    if shipped:
        argv = [sys.executable, str(ROOT / f"{module}.py")]
    else:
        argv = [sys.executable, "-c", f"import {module} as m; m.app.run(transport='sse')"]
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(f"{name} never listened on {port}: rc={proc.poll()}")
                time.sleep(0.2)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/messages/?session_id=0", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except urllib.error.HTTPError:
            pass   # 400/404 is fine: the request reached uvicorn, which is all the access log needs
        time.sleep(0.5)
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    return out, err


class Stdout(unittest.TestCase):
    def check(self, name):
        out, err = run(name, shipped=False)
        self.assertIn(ACCESS, out, f"{name} positive control: uvicorn no longer logs to stdout, so the shipped case "
                                   f"cannot discriminate (stdout={out!r})")
        out, err = run(name, shipped=True)
        self.assertEqual(out, "", f"{name} as shipped: stdout must carry audit lines only")
        self.assertIn(ACCESS, err, f"{name} as shipped: the access line must reach stderr, or nothing was logged")

    def test_talos(self):
        self.check("talos")

    def test_netdev(self):
        self.check("netdev")


if __name__ == "__main__":
    unittest.main()
