#!/usr/bin/env python3
"""Controls for talos_mcp_server.py — the operator-credential paths.

Every case is a pair: the assertion the spec makes, and a NEGATIVE CONTROL
where the shim is turned to the opposite outcome and the same assertion must
fail. Without the second half, 0 failures cannot tell "everything works" from
"the instrument is dead" — a server whose fallback never runs would pass every
positive case that only looks at the marker it always prints.

The server is imported for real (mcp installed, FastMCP constructed) with a
fake talosctl and kubectl ahead of it on PATH (test/talos-mcp-shims). The
shims log every argv element on its own, so "the value reached talosctl as ONE
argument" is a measurement, not a reading of subprocess's documentation.

Run: python3 test/test_talos_mcp_server.py     (needs `pip install mcp==<Dockerfile pin>`)
"""
import asyncio
import contextlib
import datetime as dt
import importlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SHIMS = HERE / "talos-mcp-shims"
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True   # no __pycache__ litter in the checkout
os.environ["PATH"] = f"{SHIMS}{os.pathsep}{os.environ.get('PATH', '')}"

READ_TOOLS = {"get_node_status", "get_etcd_members", "get_link_status", "get_service_logs"}
ENV_KEYS = [
    "TALOSCONFIG", "TALOS_OPERATOR_CONFIG", "POD_IP", "TALOS_NODES", "TALOS_MCP_READ_TIMEOUT",
    "OMNI_SERVICE_ACCOUNT_KEY", "OMNI_ENDPOINT", "CLAUDE_USER_ID",
    "SHIM_LOG", "SHIM_OMNI", "SHIM_DIRECT", "SHIM_CONFIG_INFO", "SHIM_REBOOT_RC", "SHIM_KUBECTL", "SHIM_NODES",
    "SHIM_CP", "SHIM_NOTREADY",
]
NODE_A, NODE_B, STRANGER = "10.9.9.23", "10.9.9.24", "10.9.9.99"
REASON = "etcd member on this node unhealthy since the SideroLink loss; graceful reboot per runbook"


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.log = d / "argv.log"
        self.reader = d / "reader-talosconfig"
        self.reader.write_text("context: reader\n")
        self.operator = d / "operator-talosconfig"
        self.operator.write_text("context: operator\n")
        self.empty = d / "empty-talosconfig"
        self.empty.write_bytes(b"")
        self.mod = None

    def close(self):
        self.tmp.cleanup()

    def load(self, *, reader=True, operator="ok", expires_days=365, omni="ok", direct="ok",
             kubectl="ok", nodes=f"{NODE_A} {NODE_B}", cp="", notready="", pod_ip="", known_nodes="", read_timeout="15"):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        if reader:
            os.environ["TALOSCONFIG"] = str(self.reader)
            os.environ["OMNI_SERVICE_ACCOUNT_KEY"] = "shim-key"
            os.environ["OMNI_ENDPOINT"] = "https://omni.example:8090"
        if operator == "ok":
            os.environ["TALOS_OPERATOR_CONFIG"] = str(self.operator)
            when = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=expires_days)).isoformat()
            os.environ["SHIM_CONFIG_INFO"] = (
                "Current context:     operator\nNodes:               not defined\n"
                f"Endpoints:           {NODE_A}\nRoles:               os:operator\n"
                f"Certificate expires: some time from now ({when})"
            )
        elif operator == "empty":
            os.environ["TALOS_OPERATOR_CONFIG"] = str(self.empty)
        elif operator == "unreadable":
            os.environ["TALOS_OPERATOR_CONFIG"] = str(self.operator)   # non-empty, but config info exits 1
        elif operator != "absent":
            raise ValueError(operator)
        os.environ.update({
            "SHIM_LOG": str(self.log), "SHIM_OMNI": omni, "SHIM_DIRECT": direct,
            "SHIM_KUBECTL": kubectl, "SHIM_NODES": nodes, "SHIM_CP": cp, "SHIM_NOTREADY": notready, "POD_IP": pod_ip,
            "TALOS_NODES": known_nodes, "TALOS_MCP_READ_TIMEOUT": read_timeout,
            "CLAUDE_USER_ID": "tester@example",
        })
        if self.log.exists():
            self.log.unlink()
        import talos_mcp_server
        self.mod = importlib.reload(talos_mcp_server)
        if self.log.exists():
            self.log.unlink()   # drop the startup config-info / can-i calls
        return self.mod

    def calls(self, binary=None):
        if not self.log.exists():
            return []
        rows = [line.rstrip("\n").split("\t") for line in self.log.read_text().splitlines()]
        return [r for r in rows if binary is None or r[0] == binary]

    def tools(self):
        return {t.name for t in asyncio.run(self.mod.app.list_tools())}

    def call(self, fn, *args, **kwargs):
        """Run a tool function, returning (response, audit_lines_from_stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = fn(*args, **kwargs)
        audit = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        return out, audit


def first_line(text):
    return text.split("\n", 1)[0]


class ReadPath(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_omni_ok_is_marked_and_makes_no_direct_attempt(self):
        m = self.h.load(omni="ok")
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertEqual(first_line(out), "[via Omni]")
        calls = self.h.calls("talosctl")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--endpoints", calls[0])
        self.assertIn(f"--talosconfig={self.h.reader}", calls[0])
        with self.subTest(control="Omni down turns the marker, so the marker is a reading"):
            m = self.h.load(omni="transport")
            out, _ = self.h.call(m.get_node_status, NODE_A)
            self.assertNotEqual(first_line(out), "[via Omni]")

    def test_transport_error_falls_back_once_over_direct_lan(self):
        m = self.h.load(omni="transport", direct="ok")
        out, _ = self.h.call(m.get_etcd_members, NODE_A)
        self.assertEqual(first_line(out), "[via direct LAN]")
        self.assertIn("answer via direct: etcd", out)
        calls = self.h.calls("talosctl")
        self.assertEqual(len(calls), 2, calls)
        self.assertNotIn("--endpoints", calls[0])
        self.assertEqual(calls[1][1:6], [f"--talosconfig={self.h.operator}", "--endpoints", NODE_A, "--nodes", NODE_A])
        self.assertEqual(calls[1][6:], ["etcd", "members"])
        with self.subTest(control="direct path also down: still exactly one retry, failure reported"):
            m = self.h.load(omni="transport", direct="transport")
            out, _ = self.h.call(m.get_etcd_members, NODE_A)
            self.assertEqual(len(self.h.calls("talosctl")), 2)
            self.assertEqual(first_line(out), "[via direct LAN]")
            self.assertIn("Unavailable", out)
            self.assertIn("Omni path had failed first", out)
        with self.subTest(control="Omni up: no direct attempt at all"):
            m = self.h.load(omni="ok", direct="ok")
            self.h.call(m.get_etcd_members, NODE_A)
            self.assertFalse(any("--endpoints" in c for c in self.h.calls("talosctl")))

    def test_timeout_counts_as_transport_error(self):
        m = self.h.load(omni="hang", direct="ok", read_timeout="1")
        out, _ = self.h.call(m.get_link_status, NODE_A, "siderolink")
        self.assertEqual(first_line(out), "[via direct LAN]")
        self.assertEqual(len(self.h.calls("talosctl")), 2)
        with self.subTest(control="no operator credential: timeout is reported, no retry"):
            m = self.h.load(operator="absent", omni="hang", read_timeout="1")
            out, _ = self.h.call(m.get_link_status, NODE_A, "siderolink")
            self.assertEqual(first_line(out), "[via Omni]")
            self.assertIn("timed out after 1s", out)
            self.assertEqual(len(self.h.calls("talosctl")), 1)

    def test_permission_denied_is_not_retried(self):
        m = self.h.load(omni="denied", direct="ok")
        out, _ = self.h.call(m.get_service_logs, NODE_A, "etcd", 20)
        self.assertEqual(first_line(out), "[via Omni]")
        self.assertIn("PermissionDenied", out)
        self.assertEqual(len(self.h.calls("talosctl")), 1)
        with self.subTest(control="the same setup with a transport error DOES retry"):
            m = self.h.load(omni="transport", direct="ok")
            self.h.call(m.get_service_logs, NODE_A, "etcd", 20)
            self.assertEqual(len(self.h.calls("talosctl")), 2)

    def test_no_operator_credential_names_the_gap(self):
        sentence = "direct-LAN fallback not available on this cluster (no operator credential)"
        m = self.h.load(operator="absent", omni="transport")
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertEqual(first_line(out), "[via Omni]")
        self.assertIn(sentence, out)
        self.assertIn("Unavailable", out)
        self.assertEqual(len(self.h.calls("talosctl")), 1)
        with self.subTest(control="with the credential the sentence is gone"):
            m = self.h.load(operator="ok", omni="transport")
            out, _ = self.h.call(m.get_node_status, NODE_A)
            self.assertNotIn("fallback not available", out)

    def test_reader_not_configured_is_unchanged_even_with_operator(self):
        m = self.h.load(reader=False, operator="ok", omni="transport")
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertIn("not configured on this cluster", out)
        self.assertEqual(self.h.calls("talosctl"), [])
        with self.subTest(control="reader configured: a call is made"):
            m = self.h.load(reader=True, operator="ok")
            self.h.call(m.get_node_status, NODE_A)
            self.assertEqual(len(self.h.calls("talosctl")), 1)

    def test_known_nodes_still_filters_reads(self):
        m = self.h.load(known_nodes=f"{NODE_A},{NODE_B}")
        out, _ = self.h.call(m.get_node_status, STRANGER)
        self.assertIn("not a known node", out)
        self.assertEqual(self.h.calls("talosctl"), [])
        with self.subTest(control="listed node passes"):
            out, _ = self.h.call(m.get_node_status, NODE_A)
            self.assertEqual(first_line(out), "[via Omni]")

    def test_service_argument_is_one_argv_element(self):
        m = self.h.load()
        hostile = "etcd; rm -rf / --tail 1"
        self.h.call(m.get_service_logs, NODE_A, hostile, 5)
        call = self.h.calls("talosctl")[0]
        self.assertEqual(call[4:], ["logs", hostile, "--tail", "5"])
        with self.subTest(control="a sane service lands in the same slot"):
            self.h.load()
            self.h.call(m.get_service_logs, NODE_A, "etcd", 5)
            self.assertEqual(self.h.calls("talosctl")[0][4:], ["logs", "etcd", "--tail", "5"])


class OperatorCredentialState(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_registered_only_when_credential_ok(self):
        self.h.load(operator="ok")
        self.assertEqual(self.h.tools(), READ_TOOLS | {"reboot_node"})
        for state in ("absent", "empty", "unreadable"):
            with self.subTest(control=state):
                self.h.load(operator=state)
                self.assertEqual(self.h.tools(), READ_TOOLS)

    def test_empty_file_reads_as_absent_and_says_so(self):
        m = self.h.load(operator="empty", omni="transport")
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertIn("direct-LAN fallback not available on this cluster (no operator credential: ", out)
        self.assertIn("is empty)", out)
        self.assertEqual(len(self.h.calls("talosctl")), 1)
        with self.subTest(control="non-empty file with readable expiry: fallback runs"):
            m = self.h.load(operator="ok", omni="transport")
            self.h.call(m.get_node_status, NODE_A)
            self.assertEqual(len(self.h.calls("talosctl")), 2)

    def test_expired_reads_as_absent_naming_the_date(self):
        m = self.h.load(operator="ok", expires_days=-1, omni="transport")
        yesterday = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
        self.assertNotIn("reboot_node", self.h.tools())
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertIn(f"direct-LAN fallback not available on this cluster (operator credential expired on {yesterday})", out)
        self.assertEqual(len(self.h.calls("talosctl")), 1)
        with self.subTest(control="expiring today is expired too"):
            self.h.load(operator="ok", expires_days=0)
            self.assertNotIn("reboot_node", self.h.tools())
        with self.subTest(control="a year out: registered, no expiry sentence"):
            m = self.h.load(operator="ok", expires_days=365, omni="transport")
            self.assertIn("reboot_node", self.h.tools())
            out, _ = self.h.call(m.get_node_status, NODE_A)
            self.assertNotIn("expired on", out)

    def test_near_expiry_warns_on_every_response(self):
        m = self.h.load(operator="ok", expires_days=10)
        soon = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=10)).isoformat()
        for fn, args in ((m.get_node_status, (NODE_A,)), (m.get_etcd_members, (NODE_A,)),
                         (m.get_link_status, (NODE_A,)), (m.get_service_logs, (NODE_A, "etcd")),
                         (m.reboot_node, (NODE_A, REASON))):
            out, _ = self.h.call(fn, *args)
            self.assertIn(soon, first_line(out), fn.__name__)
            self.assertIn("talos-operator-credential rotate", first_line(out), fn.__name__)
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertEqual(out.split("\n")[1], "[via Omni]")   # marker is next, not lost
        with self.subTest(control="29 days warns, 30 does not"):
            self.h.load(operator="ok", expires_days=29)
            self.assertTrue(self.h.mod.EXPIRY_WARNING)
            self.h.load(operator="ok", expires_days=30)
            self.assertEqual(self.h.mod.EXPIRY_WARNING, "")
        with self.subTest(control="far from expiry: first line is the path marker"):
            m = self.h.load(operator="ok", expires_days=365)
            out, _ = self.h.call(m.get_node_status, NODE_A)
            self.assertEqual(first_line(out), "[via Omni]")

    def test_unreadable_expiry_fails_closed(self):
        m = self.h.load(operator="unreadable", omni="transport")
        self.assertNotIn("reboot_node", self.h.tools())
        out, _ = self.h.call(m.get_node_status, NODE_A)
        self.assertIn("operator credential expiry could not be read", out)
        self.assertIn("error reading config: EOF", out)
        with self.subTest(control="readable: registered"):
            self.h.load(operator="ok")
            self.assertIn("reboot_node", self.h.tools())


class RebootNode(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def reboots(self):
        return [c for c in self.h.calls("talosctl") if "reboot" in c]

    def test_first_call_issues_token_and_does_nothing(self):
        m = self.h.load()
        out, audit = self.h.call(m.reboot_node, NODE_A, REASON)
        body = json.loads(out)
        self.assertEqual(body["node"], NODE_A)
        self.assertEqual(body["expires_in"], 120)
        self.assertTrue(body["token"])
        self.assertEqual(body["will_run"].split()[-3:], ["reboot", "--wait=true", "--timeout=600s"])
        self.assertEqual(self.reboots(), [])
        self.assertEqual([a["phase"] for a in audit], ["token"])
        self.assertEqual(audit[0]["node"], NODE_A)
        self.assertEqual(audit[0]["user"], "tester@example")
        self.assertIn("uncordon", body["note"])   # the node may come back cordoned (jg-jiahd 2026-10-15)
        with self.subTest(control="the same call plus the token DOES reach talosctl"):
            self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertEqual(len(self.reboots()), 1)

    def test_confirmed_call_runs_exactly_what_it_said_once(self):
        m = self.h.load()
        out, _ = self.h.call(m.reboot_node, NODE_A, REASON)
        body = json.loads(out)
        out2, audit = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
        reboots = self.reboots()
        self.assertEqual(len(reboots), 1)
        self.assertEqual(reboots[0][1:], body["will_run"].split()[1:])   # said == ran, element by element
        self.assertEqual(reboots[0][1:], [f"--talosconfig={self.h.operator}", "--endpoints", NODE_A, "--nodes", NODE_A,
                                          "reboot", "--wait=true", "--timeout=600s"])
        # k8scc#22: the real `reboot --wait` reports on stderr and exits 0 with an
        # empty stdout; the response must still carry the sequence and the exit code
        self.assertIn("reboot sequence via direct", out2)
        self.assertIn("talosctl exit 0", out2)
        self.assertNotIn("(empty response)", out2)
        self.assertIn("uncordon", out2)
        self.assertEqual(audit[-1]["phase"], "execute")
        self.assertEqual(audit[-1]["rc"], 0)
        with self.subTest(control="replaying the token is refused and runs nothing"):
            t0 = m._now(); m._now = lambda: t0 + m.REBOOT_COOLDOWN + 1   # past the cooldown, so the refusal names the token
            out3, audit3 = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertTrue(out3.startswith("refused:"), out3)
            self.assertIn("already-used", out3)
            self.assertEqual(len(self.reboots()), 1)
            self.assertEqual(audit3[-1]["phase"], "refused")
        with self.subTest(control="a nonzero talosctl exit is audited as such"):
            os.environ["SHIM_REBOOT_RC"] = "3"
            t0 = m._now(); m._now = lambda: t0 + m.REBOOT_COOLDOWN + 1   # past NODE_A's cooldown
            body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
            out4, audit4 = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertEqual(audit4[-1]["rc"], 3)
            self.assertIn("talosctl exit 3", out4)   # was "(exit 3)" from text(); the verb now states it uniformly
            self.assertIn("reboot sequence via direct", out4)

    def test_token_is_bound_to_node(self):
        m = self.h.load()
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        out, audit = self.h.call(m.reboot_node, NODE_B, REASON, confirm=body["token"])
        self.assertIn(f"issued for {NODE_A}, not {NODE_B}", out)
        self.assertEqual(self.reboots(), [])
        self.assertEqual(audit[-1]["phase"], "refused")
        with self.subTest(control="the right node with the same token executes"):
            self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertEqual(len(self.reboots()), 1)

    def test_token_expires_after_120_seconds(self):
        m = self.h.load()
        real_now = m._now
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        issued = real_now()
        m._now = lambda: issued + 121
        out, audit = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
        self.assertIn("token expired", out)
        self.assertEqual(self.reboots(), [])
        self.assertEqual(audit[-1]["phase"], "refused")
        with self.subTest(control="at 119 s the same token executes"):
            body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
            issued = m._now()
            m._now = lambda: issued + 119
            self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertEqual(len(self.reboots()), 1)

    def test_node_outside_cluster_is_refused_before_any_talos_call(self):
        m = self.h.load(nodes=f"{NODE_A} {NODE_B}")
        out, audit = self.h.call(m.reboot_node, STRANGER, REASON)
        self.assertTrue(out.startswith("refused:"))
        self.assertIn("not an InternalIP of any node currently in this cluster", out)
        self.assertEqual(self.h.calls("talosctl"), [])
        self.assertEqual(audit, [{**audit[0], "phase": "refused"}])
        with self.subTest(control="the allowlist is read at call time: add the node, same process accepts it"):
            os.environ["SHIM_NODES"] = f"{NODE_A} {NODE_B} {STRANGER}"
            out, audit = self.h.call(m.reboot_node, STRANGER, REASON)
            self.assertIn("token", json.loads(out))

    def test_kubectl_failure_fails_closed(self):
        m = self.h.load(kubectl="fail")
        out, audit = self.h.call(m.reboot_node, NODE_A, REASON)
        self.assertTrue(out.startswith("refused:"))
        self.assertIn("fail closed", out)
        self.assertIn("Unable to connect to the server", out)
        self.assertEqual(self.h.calls("talosctl"), [])
        self.assertEqual(audit[-1]["phase"], "refused")
        with self.subTest(control="kubectl back: token issued"):
            os.environ["SHIM_KUBECTL"] = "ok"
            out, _ = self.h.call(m.reboot_node, NODE_A, REASON)
            self.assertIn("token", json.loads(out))

    def test_own_node_is_rebooted_without_waiting(self):
        m = self.h.load(pod_ip=NODE_A)
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        self.assertTrue(body["will_run"].endswith("reboot --wait=false"), body["will_run"])
        out, audit = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
        self.assertEqual(self.reboots()[0][-1], "--wait=false")
        self.assertNotIn("--timeout=600s", self.reboots()[0])
        self.assertIn("this pod runs on", out)
        self.assertIs(audit[-1]["wait"], False)
        with self.subTest(control="another node from the same pod waits"):
            body = json.loads(self.h.call(m.reboot_node, NODE_B, REASON)[0])
            out, audit = self.h.call(m.reboot_node, NODE_B, REASON, confirm=body["token"])
            self.assertEqual(self.reboots()[-1][-2:], ["--wait=true", "--timeout=600s"])
            self.assertNotIn("this pod runs on", out)
            self.assertIs(audit[-1]["wait"], True)

    def test_argument_injection_cannot_change_the_verb(self):
        m = self.h.load()
        for hostile in (f"{NODE_A} --mode powercycle", f"{NODE_A};shutdown", "--nodes", f"{NODE_A}\n{NODE_B}"):
            with self.subTest(node=hostile):
                out, _ = self.h.call(m.reboot_node, hostile, REASON)
                self.assertTrue(out.startswith("refused:"), out)
                self.assertEqual(self.h.calls("talosctl"), [])
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"] + " --mode powercycle")
        self.assertEqual(self.reboots(), [])
        with self.subTest(control="the clean token runs the literal verb"):
            self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertEqual(self.reboots()[0][6], "reboot")

    # --- root 7.2 (ferry133 ruling 2026-10-15): the gates are the server's, nobody is prompted

    def test_reason_is_required_and_lands_in_every_audit_line(self):
        m = self.h.load()
        schema = next(t for t in asyncio.run(m.app.list_tools()) if t.name == "reboot_node").inputSchema
        self.assertEqual(set(schema["required"]), {"node", "reason"})   # the MCP contract itself, not just the function
        for empty in ("", "   "):
            with self.subTest(reason=repr(empty)):
                out, audit = self.h.call(m.reboot_node, NODE_A, empty)
                self.assertTrue(out.startswith("refused:"), out)
                self.assertIn("reason is required", out)
                self.assertEqual(self.h.calls("kubectl"), [])   # refused before anything is looked up
                self.assertEqual(audit[-1]["phase"], "refused")
                self.assertEqual(audit[-1]["stated_reason"], "")
        with self.subTest(control="a stated reason is accepted and written to the token and execute lines"):
            out, audit = self.h.call(m.reboot_node, NODE_A, REASON)
            body = json.loads(out)
            self.assertEqual(audit[-1]["phase"], "token")
            self.assertEqual(audit[-1]["stated_reason"], REASON)
            _, audit = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertEqual(audit[-1]["phase"], "execute")
            self.assertEqual(audit[-1]["stated_reason"], REASON)

    def test_quorum_guard_refuses_while_another_control_plane_is_not_ready(self):
        m = self.h.load(cp=f"{NODE_A} {NODE_B}", notready=NODE_B)
        out, audit = self.h.call(m.reboot_node, NODE_A, REASON)
        self.assertTrue(out.startswith("refused:"), out)
        self.assertIn("quorum", out)
        self.assertIn(NODE_B, out)
        self.assertEqual(self.h.calls("talosctl"), [])
        self.assertEqual(audit[-1]["phase"], "refused")
        with self.subTest(control="every other control plane Ready: token issued"):
            os.environ["SHIM_NOTREADY"] = ""
            self.assertIn("token", json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0]))
        with self.subTest(control="the target itself NotReady is not a quorum block (that is why it is being rebooted)"):
            os.environ["SHIM_NOTREADY"] = NODE_A
            self.assertIn("token", json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0]))
        with self.subTest(control="a NotReady worker does not block"):
            os.environ["SHIM_CP"] = NODE_A
            os.environ["SHIM_NOTREADY"] = NODE_B
            self.assertIn("token", json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0]))
        with self.subTest(control="checked again at execute: a token issued while quorum held is refused once it does not"):
            os.environ["SHIM_CP"] = f"{NODE_A} {NODE_B}"
            os.environ["SHIM_NOTREADY"] = ""
            body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
            os.environ["SHIM_NOTREADY"] = NODE_B
            out, _ = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
            self.assertIn("quorum", out)
            self.assertEqual(self.reboots(), [])

    def test_cooldown_refuses_a_second_reboot_of_the_same_node(self):
        m = self.h.load()
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        self.assertEqual(body["cooldown_after"], 600)   # the number is in the response, not only in the code
        t0 = m._now()
        self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
        self.assertEqual(len(self.reboots()), 1)
        m._now = lambda: t0 + 30
        out, audit = self.h.call(m.reboot_node, NODE_A, REASON)
        self.assertTrue(out.startswith("refused:"), out)
        self.assertIn("cooldown is 600 s", out)
        self.assertIn("570 s left", out)
        self.assertEqual(audit[-1]["phase"], "refused")
        with self.subTest(control="another node is not in this node's cooldown"):
            self.assertIn("token", json.loads(self.h.call(m.reboot_node, NODE_B, REASON)[0]))
        with self.subTest(control="the window ends: the same node is accepted again"):
            m._now = lambda: t0 + 601
            self.assertIn("token", json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0]))

    def test_own_node_responses_say_im_goes_down_with_it(self):
        m = self.h.load(pod_ip=NODE_A)
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        self.assertIn("takes im down with it", body["note"])
        self.assertIn("single-node cluster", body["note"])
        out, _ = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
        self.assertIn("takes im down with it", out)
        with self.subTest(control="another node's responses do not carry the outage sentence"):
            body = json.loads(self.h.call(m.reboot_node, NODE_B, REASON)[0])
            self.assertNotIn("takes im down with it", body["note"])
            out, _ = self.h.call(m.reboot_node, NODE_B, REASON, confirm=body["token"])
            self.assertNotIn("takes im down with it", out)

    def test_audit_lines_are_json_with_the_required_fields(self):
        m = self.h.load()
        _, refused = self.h.call(m.reboot_node, STRANGER, REASON)
        body = json.loads(self.h.call(m.reboot_node, NODE_A, REASON)[0])
        _, executed = self.h.call(m.reboot_node, NODE_A, REASON, confirm=body["token"])
        self.assertEqual(set(refused[0]) >= {"ts", "tool", "node", "phase", "reason", "user", "stated_reason"}, True, refused)
        self.assertEqual(set(executed[0]) >= {"ts", "tool", "node", "phase", "rc", "user", "stated_reason"}, True, executed)
        self.assertEqual(executed[0]["stated_reason"], REASON)
        for line in refused + executed:
            self.assertEqual(line["tool"], "reboot_node")
            dt.datetime.fromisoformat(line["ts"])
        with self.subTest(control="read tools write no audit line"):
            _, audit = self.h.call(m.get_node_status, NODE_A)
            self.assertEqual(audit, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
