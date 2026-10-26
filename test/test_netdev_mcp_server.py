#!/usr/bin/env python3
"""Controls for netdev_mcp_server.py — the reader-only release.

Same shape as test_talos_mcp_server.py: every case is the assertion the spec
makes plus a NEGATIVE CONTROL that must fail the same assertion, so 0 failures
cannot mean "the instrument is dead". The server is imported for real (mcp
and its httpx installed, FastMCP constructed) against a FAKE CONTROLLER: an
in-process HTTP server that answers from test/netdev-mcp-fixtures/ and records
every request it receives (method, path, the X-API-Key it was sent).

The fixtures are hand-written from UniFi's documented field names, not
recorded from a console: no cell here says anything about a real controller.
Those are root §6, on FAC1.

Run: python3 test/test_netdev_mcp_server.py   (needs `pip install mcp==<Dockerfile pin>`)
"""
import asyncio
import contextlib
import importlib
import io
import json
import os
import pathlib
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

FIXTURES = json.loads((HERE / "netdev-mcp-fixtures" / "controller.json").read_text())
READ_TOOLS = {"get_sites", "get_controller_info", "get_devices", "get_clients", "get_networks",
              "get_firewall_rules", "get_port_forwards", "get_dhcp_reservations", "get_wlans", "get_lan_inventory"}
ENV_KEYS = ["UNIFI_CONTROLLER_URL", "UNIFI_SITE", "UNIFI_CA_FILE", "UNIFI_READER_KEY_FILE", "UNIFI_OPERATOR_FILE",
            "NETDEV_ALLOW_INSECURE_HTTP", "NETDEV_READ_TIMEOUT", "POD_IP", "IM_FAC_TICKET_REPO",
            "IM_FAC_TICKET_TOKEN_FILE"]
READER_KEY = "dummy-reader-key-7f3a9c"          # must never appear in any output
OPERATOR_VALUE = "dummy-operator-pw-41be"       # mounted, and this image must not even read it
SECRETS_IN_FIXTURE = ["correct-horse-PASS-1234", "ppsk-pw-9876", "radius-s3cret-55"]
INTEGRATION = "/proxy/network/integration/v1"
LEGACY = "/proxy/network/api/s/default"


class _Server(ThreadingHTTPServer):
    # HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS lookup that
    # took 35 s on a macOS laptop; the fake needs no name.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeController:
    """Answers the paths the ROUTES table names from the fixture; records requests."""

    def __init__(self):
        self.requests = []
        self.data = json.loads(json.dumps(FIXTURES))
        self.status = {}        # path -> forced HTTP status
        self.legacy_rc = "ok"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                fake.requests.append(("GET", parts.path, self.headers.get("X-API-Key")))
                code, body = fake.answer(parts.path, parse_qs(parts.query))
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def answer(self, path, query):
        if path in self.status:
            return self.status[path], {"error": "forced"}
        site = self.data["sites"][0]["id"]
        integration = {f"{INTEGRATION}/info": "info", f"{INTEGRATION}/sites": "sites",
                       f"{INTEGRATION}/sites/{site}/devices": "devices",
                       f"{INTEGRATION}/sites/{site}/clients": "clients",
                       f"{INTEGRATION}/sites/{site}/networks": "networks"}
        if path in integration:
            data = self.data[integration[path]]
            if not isinstance(data, list):
                return 200, data
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["25"])[0])
            page = data[offset:offset + limit]
            return 200, {"offset": offset, "limit": limit, "count": len(page), "totalCount": len(data), "data": page}
        if path.startswith(LEGACY + "/rest/"):
            name = {"networkconf": "networks", "firewallrule": "firewallrules", "firewallgroup": "firewallgroups",
                    "portforward": "portforwards", "user": "users", "wlanconf": "wlans"}.get(path.rsplit("/", 1)[1])
            if name:
                if self.legacy_rc != "ok":
                    return 200, {"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []}
                return 200, {"meta": {"rc": "ok"}, "data": self.data[name]}
        return 404, {"error": f"no such path {path}"}

    def paths(self):
        return [p for _, p, _ in self.requests]


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.key = d / "reader.key"
        self.key.write_text(READER_KEY + "\n")
        self.empty = d / "empty.key"
        self.empty.write_bytes(b"")
        self.operator = d / "operator"
        self.operator.write_text(f"im-operator\n{OPERATOR_VALUE}\n")
        self.ca = d / "ca.pem"
        self.ca.write_text("-----BEGIN CERTIFICATE-----\nnot a real one\n-----END CERTIFICATE-----\n")
        self.fake = FakeController()
        self.mod = None

    def close(self):
        self.fake.close()
        self.tmp.cleanup()

    def load(self, *, url="fake", key="ok", insecure=True, site="", ca=""):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["UNIFI_CONTROLLER_URL"] = self.fake.url if url == "fake" else url
        if key == "ok":
            os.environ["UNIFI_READER_KEY_FILE"] = str(self.key)
        elif key == "empty":
            os.environ["UNIFI_READER_KEY_FILE"] = str(self.empty)
        elif key == "missing":
            os.environ["UNIFI_READER_KEY_FILE"] = str(self.key) + ".absent"
        elif key != "unset":
            raise ValueError(key)
        if insecure:
            os.environ["NETDEV_ALLOW_INSECURE_HTTP"] = "1"
        if site:
            os.environ["UNIFI_SITE"] = site
        if ca:
            os.environ["UNIFI_CA_FILE"] = str(self.ca) if ca == "ok" else ca
        # the inputs a later image consumes, mounted here so "not read" is a measurement
        os.environ["UNIFI_OPERATOR_FILE"] = str(self.operator)
        os.environ["IM_FAC_TICKET_REPO"] = "ferry133/fac-tickets"
        os.environ["NETDEV_READ_TIMEOUT"] = "5"
        import netdev_mcp_server
        self.mod = importlib.reload(netdev_mcp_server)
        self.fake.requests.clear()
        return self.mod

    def tools(self):
        return {t.name for t in asyncio.run(self.mod.app.list_tools())}

    def call(self, fn, *args, **kwargs):
        """(response, stdout) — stdout must stay empty for read tools."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = fn(*args, **kwargs)
        return out, buf.getvalue()

    def readings(self):
        return "\n".join(self.mod._startup_readings())


def body(text):
    """The JSON after the path-marker line."""
    return json.loads(text.split("\n", 1)[1])


class Case(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()


class Configuration(Case):
    def test_plain_http_refused_without_the_test_flag(self):
        m = self.h.load(insecure=False)
        for name in sorted(READ_TOOLS):
            out, _ = self.h.call(getattr(m, name))
            self.assertIn("controller URL refused", out, name)
        self.assertEqual(self.h.fake.requests, [])
        self.assertIn("controller URL refused", self.h.readings())
        self.assertEqual(self.h.fake.requests, [], "startup readings must not reach the controller either")
        with self.subTest(control="with the flag, requests arrive and the readings say so"):
            m = self.h.load(insecure=True)
            out, _ = self.h.call(m.get_devices)
            self.assertTrue(out.startswith("[via integration]"), out)
            self.assertGreater(len(self.h.fake.requests), 0)
            self.assertIn("insecure http accepted", self.h.readings())

    def test_unknown_scheme_refused(self):
        m = self.h.load(url="ftp://console.example")
        self.assertIn("neither https nor http", self.h.call(m.get_devices)[0])
        with self.subTest(control="https is not refused as a scheme"):
            m = self.h.load(url="https://console.example", ca="ok")
            self.assertEqual(m.URL_REFUSAL, "")

    def test_https_without_a_ca_is_not_configured(self):
        m = self.h.load(url="https://console.example", ca="")
        out, _ = self.h.call(m.get_devices)
        self.assertIn("not configured", out)
        self.assertIn("UNIFI_CA_FILE", out)
        with self.subTest(control="a CA file present removes that item"):
            m = self.h.load(url="https://console.example", ca="ok")
            self.assertNotIn("UNIFI_CA_FILE", " ".join(m.MISSING_CONFIG))

    def test_https_verifies_against_the_ca_file(self):
        # A console whose certificate does not chain to UNIFI_CA_FILE must not
        # be read: the fake speaks plain http on that port, so the TLS
        # handshake fails, and that failure is the answer — not data.
        m = self.h.load(url=self.h.fake.url.replace("http://", "https://"), ca="ok", insecure=False)
        out, _ = self.h.call(m.get_devices)
        self.assertIn("error:", out)
        self.assertNotIn("dev-gw", out)
        with self.subTest(control="the same fake over http with the test flag answers"):
            m = self.h.load()
            self.assertIn("dev-gw", self.h.call(m.get_devices)[0])

    def test_no_option_disables_verification(self):
        src = (ROOT / "netdev_mcp_server.py").read_text()
        self.assertNotIn("verify=False", src.replace('`verify=False`', ""))
        self.assertNotIn("NETDEV_INSECURE_TLS", src)
        with self.subTest(control="the grep can see a verify=False when one is there"):
            self.assertIn("verify=False", "httpx.Client(verify=False)")

    def test_missing_reader_key_answers_not_configured_naming_it(self):
        for state in ("unset", "missing", "empty"):
            with self.subTest(key=state):
                m = self.h.load(key=state)
                for name in sorted(READ_TOOLS):
                    out, _ = self.h.call(getattr(m, name))
                    self.assertIn("not configured", out, name)
                    self.assertIn("UNIFI_READER_KEY_FILE", out, name)
                self.assertEqual(self.h.fake.requests, [])
        with self.subTest(control="key present: data, not a refusal"):
            m = self.h.load()
            self.assertNotIn("not configured", self.h.call(m.get_devices)[0])

    def test_credentials_never_surface(self):
        m = self.h.load()
        outputs = [self.h.readings()]
        stdouts = []
        for name in sorted(READ_TOOLS):
            out, stdout = self.h.call(getattr(m, name))
            outputs.append(out)
            stdouts.append(stdout)
        self.h.fake.status[f"{LEGACY}/rest/wlanconf"] = 401
        outputs.append(self.h.call(m.get_wlans)[0])     # an error response too
        joined = "\n".join(outputs + stdouts)
        for value in (READER_KEY, OPERATOR_VALUE):
            self.assertNotIn(value, joined)
        self.assertNotIn(READER_KEY, json.dumps(dict(os.environ)), "the key must not be exported to env")
        with self.subTest(control="the key really was in play: the fake received it"):
            self.assertIn(READER_KEY, {k for _, _, k in self.h.fake.requests})


class StartupStates(Case):
    def test_reader_only_registers_exactly_the_ten_reader_tools(self):
        m = self.h.load()
        self.assertEqual(self.h.tools(), READ_TOOLS)
        readings = self.h.readings()
        self.assertIn("write tools: not registered (reader-only release)", readings)
        self.assertIn("operator credential: not read by this image", readings)
        with self.subTest(control="the tool list is not empty-equal by accident"):
            self.assertEqual(len(self.h.tools()), 10)

    def test_operator_file_is_not_opened(self):
        # mounted with a value; this image must not even read it
        self.h.load()
        src = (ROOT / "netdev_mcp_server.py").read_text()
        self.assertNotIn("UNIFI_OPERATOR_FILE", src.split('"""', 2)[2])
        with self.subTest(control="the reader key file IS read from its env var"):
            self.assertIn('"UNIFI_READER_KEY_FILE"', src)

    def test_not_configured_still_registers_and_answers(self):
        m = self.h.load(key="unset")
        self.assertEqual(self.h.tools(), READ_TOOLS)
        self.assertIn("reader credential: missing", self.h.readings())
        with self.subTest(control="configured: reader credential ok"):
            self.h.load()
            self.assertIn("reader credential: ok", self.h.readings())

    def test_startup_reading_names_the_controller_version(self):
        self.h.load()
        self.assertIn("controller version: 9.4.19", self.h.readings())
        with self.subTest(control="an unreachable /info reads as unreadable, not as a version"):
            self.h.fake.status[f"{INTEGRATION}/info"] = 503
            self.assertIn("controller version: unreadable", self.h.readings())

    def test_routes_are_labelled_provisional(self):
        self.h.load()
        self.assertIn("routes: provisional, not measured (root spike 1.1)", self.h.readings())


class ReaderTools(Case):
    def test_passphrase_stripped(self):
        m = self.h.load()
        out, _ = self.h.call(m.get_wlans)
        self.assertIn("<stripped>", out)
        for value in SECRETS_IN_FIXTURE:
            self.assertNotIn(value, out)
        wlan = body(out)[0]
        self.assertEqual(wlan["x_passphrase"], "<stripped>")
        self.assertEqual(wlan["radius_secret"], "<stripped>")
        self.assertEqual(wlan["private_preshared_keys"][0]["password"], "<stripped>")
        self.assertEqual(wlan["private_preshared_keys"][0]["networkconf_id"], "net-iot", "non-secrets survive")
        with self.subTest(control="the fake really served them (strip off => they show)"):
            original = m._strip
            m._strip = lambda obj: obj
            try:
                raw = self.h.call(m.get_wlans)[0]
            finally:
                m._strip = original
            for value in SECRETS_IN_FIXTURE:
                self.assertIn(value, raw)

    def test_strip_patterns(self):
        m = self.h.load()
        cases = {"x_password": True, "X_IAPP_KEY": True, "radius_secret": True, "wpa_psk": True,
                 "ssh_key": True, "AdminPassword": True, "passphrase_hint": True,
                 "name": False, "x": False, "key_count": False, "networkconf_id": False, "psk_enabled_x": True}
        for key, secret in cases.items():
            got = m._strip({key: "v"})[key]
            self.assertEqual(got == "<stripped>", secret, key)

    def test_path_marker_follows_the_route_table(self):
        m = self.h.load()
        expect = {"get_sites": "integration", "get_controller_info": "integration", "get_devices": "integration",
                  "get_clients": "integration", "get_lan_inventory": "integration", "get_networks": "legacy",
                  "get_firewall_rules": "legacy", "get_port_forwards": "legacy",
                  "get_dhcp_reservations": "legacy", "get_wlans": "legacy"}
        for name, api in expect.items():
            out, _ = self.h.call(getattr(m, name))
            self.assertEqual(out.split("\n", 1)[0], f"[via {api}]", name)
            self.assertNotIn("error:", out, name)
        with self.subTest(control="moving a route moves the marker and the request"):
            m.ROUTES["networks"] = ("integration", "/sites/{site_id}/networks")
            out, _ = self.h.call(m.get_networks)
            self.assertEqual(out.split("\n", 1)[0], "[via integration]")
            self.assertIn(f"{INTEGRATION}/sites/site-uuid-1/networks", self.h.fake.paths())

    def test_integration_pages_are_all_read(self):
        m = self.h.load()
        m.PAGE_LIMIT = 2
        clients = body(self.h.call(m.get_clients)[0])
        self.assertEqual([c["id"] for c in clients], ["cl-node", "cl-phone", "cl-tv"])
        client_reqs = [p for p in self.h.fake.paths() if p.endswith("/clients")]
        self.assertEqual(len(client_reqs), 2)
        with self.subTest(control="a server that stops after one page returns two"):
            m.MAX_PAGES = 1
            out = self.h.call(m.get_clients)[0]
            self.assertIn("more than 1 pages", out)

    def test_site_resolved_from_unifi_site(self):
        m = self.h.load(site="nosuchsite")
        out = self.h.call(m.get_devices)[0]
        self.assertIn("site 'nosuchsite' (UNIFI_SITE) not found", out)
        self.assertIn("default", out, "names the sites that do exist")
        with self.subTest(control="the default site resolves"):
            m = self.h.load()
            self.assertIn("dev-gw", self.h.call(m.get_devices)[0])

    def test_controller_errors_are_answers_not_data(self):
        m = self.h.load()
        self.h.fake.status[f"{LEGACY}/rest/networkconf"] = 401
        out = self.h.call(m.get_networks)[0]
        self.assertTrue(out.startswith("[via legacy]\nerror:"), out)
        self.assertIn("401", out)
        self.h.fake.status.clear()
        self.h.fake.legacy_rc = "error"
        out = self.h.call(m.get_networks)[0]
        self.assertIn("did not answer rc=ok", out)
        self.assertIn("NoPermission", out)
        with self.subTest(control="rc=ok answers data"):
            self.h.fake.legacy_rc = "ok"
            self.assertIn("net-lan", self.h.call(m.get_networks)[0])

    def test_dhcp_reservations_are_only_fixed_ip_clients(self):
        m = self.h.load()
        rows = body(self.h.call(m.get_dhcp_reservations)[0])
        self.assertEqual([r["_id"] for r in rows], ["u-phone"])
        with self.subTest(control="the fixture holds a non-reserved user that was filtered"):
            self.assertIn("u-tv", [u["_id"] for u in FIXTURES["users"]])

    def test_firewall_rules_carry_the_expanded_addresses(self):
        m = self.h.load()
        rules = {r["_id"]: r for r in body(self.h.call(m.get_firewall_rules)[0])}
        self.assertEqual(rules["rule-ctrl"]["dst_expanded"], ["10.9.9.1", "10.9.9.200-10.9.9.210"])
        self.assertEqual(rules["rule-ctrl"]["src_expanded"], "any")
        self.assertEqual(rules["rule-net"]["src_expanded"], ["10.9.9.1/24"])
        with self.subTest(control="a group that is not in the controller's answer is named, not dropped"):
            self.h.fake.data["firewallgroups"] = [g for g in FIXTURES["firewallgroups"] if g["_id"] != "grp-ctrl"]
            rules = {r["_id"]: r for r in body(self.h.call(m.get_firewall_rules)[0])}
            self.assertEqual(rules["rule-ctrl"]["dst_unresolved"], ["grp-ctrl"])

    def test_lan_inventory_merges_devices_and_clients(self):
        m = self.h.load()
        rows = body(self.h.call(m.get_lan_inventory)[0])
        self.assertEqual(len(rows), 6)
        node = next(r for r in rows if r["ip"] == "10.9.9.62")
        self.assertEqual((node["kind"], node["mac"], node["uplink"]), ("client", "bb:bb:bb:00:00:62", "dev-sw"))
        self.assertEqual({r["kind"] for r in rows}, {"device", "client"})

    def test_reader_results_fill_the_session_cache(self):
        m = self.h.load()
        self.assertIsNone(m._seen("clients", "cl-phone"))
        self.h.call(m.get_clients)
        self.assertEqual(m._seen("clients", "cl-phone")["ipAddress"], "10.9.9.120")
        self.h.call(m.get_wlans)
        self.assertEqual(m._seen("wlans", "wlan-1")["x_passphrase"], "<stripped>", "the cache holds stripped objects")
        with self.subTest(control="past the TTL the entry is gone"):
            later = m._now() + m.CACHE_TTL + 1
            m._now = lambda: later
            self.assertIsNone(m._seen("clients", "cl-phone"))

    def test_read_tools_write_nothing_to_stdout(self):
        m = self.h.load()
        for name in sorted(READ_TOOLS):
            _, stdout = self.h.call(getattr(m, name))
            self.assertEqual(stdout, "", name)
        with self.subTest(control="the capture sees a print"):
            self.assertEqual(self.h.call(print, "x")[1], "x\n")


class ClassA(Case):
    """The four pure functions the write tools will call (D-e). No I/O."""

    POD, CTRL, API = "10.9.9.62", "10.9.9.1", ["10.9.9.20"]

    def args(self, **over):
        f = json.loads(json.dumps(FIXTURES))
        a = dict(pod_ip=self.POD, controller_ip=self.CTRL, api_server_ips=self.API, networks=f["networks"],
                 clients=f["clients"], devices=f["devices"], rules=f["firewallrules"], groups=f["firewallgroups"])
        a.update(over)
        return a

    def setUp(self):
        super().setUp()
        self.m = self.h.load()

    def test_own_network_refused(self):
        why = self.m.class_a("networks", "net-lan", **self.args())
        self.assertIn("LAN", why)
        self.assertIn("10.9.9.1/24", why)
        with self.subTest(control="an unrelated network passes"):
            self.assertIsNone(self.m.class_a("networks", "net-iot", **self.args()))

    def test_api_server_on_another_network_counts(self):
        why = self.m.class_a("networks", "net-iot", **self.args(api_server_ips=["10.9.8.5"]))
        self.assertIn("IoT", why)

    def test_firewall_group_expansion(self):
        why = self.m.class_a("firewallrules", "rule-ctrl", **self.args())
        self.assertIn("drop to controller", why)
        with self.subTest(control="a group without the controller proceeds"):
            self.assertIsNone(self.m.class_a("firewallrules", "rule-iot", **self.args()))
            self.assertIsNone(self.m.class_a("firewallrules", "rule-net", **self.args()))

    def test_group_address_forms(self):
        for member, covers in [("10.9.9.1", True), ("10.9.9.0/24", True), ("10.9.9.0-10.9.9.5", True),
                               ("10.9.9.2", False), ("10.9.10.0/24", False), ("10.9.9.2-10.9.9.9", False)]:
            groups = [{"_id": "g", "group_type": "address-group", "group_members": [member]}]
            rules = [{"_id": "r", "dst_firewallgroup_ids": ["g"]}]
            hits = self.m.fw_rules_covering([(self.POD, self.CTRL)], rules, groups, [])
            self.assertEqual(bool(hits), covers, member)

    def test_unresolved_group_counts_as_covering(self):
        rules = [{"_id": "r", "src_address": self.POD, "dst_firewallgroup_ids": ["gone"]}]
        self.assertTrue(self.m.fw_rules_covering([(self.POD, self.CTRL)], rules, [], []))
        with self.subTest(control="resolved to a group that misses passes"):
            groups = [{"_id": "gone", "group_type": "address-group", "group_members": ["10.9.7.0/24"]}]
            self.assertFalse(self.m.fw_rules_covering([(self.POD, self.CTRL)], rules, groups, []))

    def test_port_group_does_not_constrain_addresses(self):
        rules = [{"_id": "r", "src_address": self.POD, "dst_firewallgroup_ids": ["grp-ports"]}]
        self.assertTrue(self.m.fw_rules_covering([(self.POD, self.CTRL)], rules, FIXTURES["firewallgroups"], []))

    def test_negated_side_counts_as_covering(self):
        rules = [{"_id": "r", "src_address": "10.9.8.7", "src_address_negate": True, "dst_address": self.CTRL}]
        self.assertTrue(self.m.fw_rules_covering([(self.POD, self.CTRL)], rules, [], []))
        with self.subTest(control="without negation the same rule misses the pod"):
            rules[0]["src_address_negate"] = False
            self.assertFalse(self.m.fw_rules_covering([(self.POD, self.CTRL)], rules, [], []))

    def test_attached_device_refused(self):
        why = self.m.class_a("devices", "dev-sw", **self.args())
        self.assertIn("attached", why)
        with self.subTest(control="the AP the node is not on passes"):
            self.assertIsNone(self.m.class_a("devices", "dev-ap", **self.args()))

    def test_missing_reader_data_fails_closed(self):
        for missing in ("networks", "clients", "devices", "rules", "groups"):
            with self.subTest(missing=missing):
                why = self.m.class_a("networks", "net-iot", **self.args(**{missing: None}))
                self.assertIn("fail closed", why)
        self.assertIn("POD_IP", self.m.class_a("networks", "net-iot", **self.args(pod_ip="")))
        with self.subTest(control="every input present: the same target passes"):
            self.assertIsNone(self.m.class_a("networks", "net-iot", **self.args()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
