#!/usr/bin/env python3
"""Controls for netdev_mcp_server.py — reader and operator.

Same shape as test_talos_mcp_server.py: every case is the assertion the spec
makes plus a NEGATIVE CONTROL that must fail the same assertion, so 0 failures
cannot mean "the instrument is dead". The server is imported for real (mcp
and its httpx installed, FastMCP constructed) against a FAKE CONTROLLER: an
in-process HTTP server that answers from test/netdev-mcp-fixtures/ and records
every request it receives (method, path, which account's cookie it carried,
and the body of every write). Reader and operator are two local accounts
logging in over /api/auth/login, each with its own cookie; the fake answers a
write carrying the reader's cookie 403, as FAC1's View Only account did
(k8scc#67 finding 6). The operator half adds a FAKE FAC (GitHub's issues API,
which can pre-ack, post-ack or stay silent), three OUTSIDE PROBES (local TCP
listeners that can be stopped one by one) and a kubectl shim
(test/netdev-mcp-shims) answering /readyz and the API server endpoints.

The fixtures are hand-written from UniFi's documented field names, not
recorded from a console: no cell here says anything about a real controller.
Those are root §6, on FAC1.

Run: python3 test/test_netdev_mcp_server.py   (needs `pip install mcp==<Dockerfile pin>`)
"""
import asyncio
import contextlib
import hashlib
import importlib
import inspect
import io
import json
import os
import pathlib
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

SHIMS = HERE / "netdev-mcp-shims"
os.environ["PATH"] = f"{SHIMS}{os.pathsep}{os.environ.get('PATH', '')}"

FIXTURES = json.loads((HERE / "netdev-mcp-fixtures" / "controller.json").read_text())
READ_TOOLS = {"get_sites", "get_controller_info", "get_devices", "get_clients", "get_networks",
              "get_firewall_rules", "get_port_forwards", "get_dhcp_reservations", "get_wlans", "get_lan_inventory",
              "get_dns_records"}
WRITE_TOOLS = {"set_dhcp_reservation", "clear_dhcp_reservation", "block_client", "unblock_client",
               "set_port_forward", "delete_port_forward", "restart_device", "power_cycle_port",
               "set_dns_forward", "clear_dns_forward"}
ENV_KEYS = ["UNIFI_CONTROLLER_URL", "UNIFI_SITE", "UNIFI_CA_FILE", "UNIFI_READER_FILE", "UNIFI_READER_KEY_FILE",
            "UNIFI_OPERATOR_FILE",
            "NETDEV_ALLOW_INSECURE_HTTP", "NETDEV_READ_TIMEOUT", "POD_IP", "IM_FAC_TICKET_REPO",
            "IM_FAC_TICKET_TOKEN_FILE", "IM_FAC_TICKET_API", "NETDEV_OUTSIDE_PROBES", "NETDEV_SNAPSHOT_DIR",
            "NETDEV_SNAPSHOT_KEEP", "NETDEV_HEALTH_TIMEOUT", "NETDEV_HEALTH_INTERVAL", "NETDEV_WRITES_PER_HOUR",
            "NETDEV_WITNESS_WAIT", "NETDEV_WITNESS_POLL", "NETDEV_PROBE_TIMEOUT", "CLUSTER_NAME", "CLAUDE_USER_ID",
            "NETDEV_DNS_SETTLE", "NETDEV_DNS_PROTECT",
            "KUBERNETES_SERVICE_HOST", "NETDEV_SHIM_READYZ", "NETDEV_SHIM_ENDPOINTS_FAIL", "NETDEV_SHIM_APISERVER"]
READER_USER = "im-reader"
READER_VALUE = "dummy-reader-pw-7f3a9c"         # the View Only password: sent at login, never shown
OPERATOR_USER = "im-operator"
OPERATOR_VALUE = "dummy-operator-pw-41be"       # the operator password: sent at login, never shown
TICKET_TOKEN = "dummy-ticket-token-c0ffee"      # the im→FAC token: sent to the fake FAC, never shown
USER_SECRET = "tv-user-s3cret-31"               # u-tv.x_note: stripped at every exit, kept by the snapshot
COOKIE, CSRF = "fake-session-cookie", "fake-csrf-1"                 # the operator's
READER_COOKIE, READER_CSRF = "fake-reader-cookie", "fake-csrf-r"    # the reader's
SECRETS_IN_FIXTURE = ["correct-horse-PASS-1234", "ppsk-pw-9876", "radius-s3cret-55"]
LEGACY = "/proxy/network/api/s/default"
LEGACY_ROOT = "/proxy/network/api"
V2 = "/proxy/network/v2/api/site/default"


class _Server(ThreadingHTTPServer):
    # HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS lookup that
    # took 35 s on a macOS laptop; the fake needs no name.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeController:
    """Answers the paths the ROUTES table names from the fixture; records requests.
    `tls=(cert, key)` serves it over https with that certificate."""

    def __init__(self, tls=None):
        self.requests = []
        self.data = json.loads(json.dumps(FIXTURES))
        self.status = {}        # path -> forced HTTP status
        self.legacy_rc = "ok"
        self.writes = []        # (method, path, body) of every login/PUT/POST/DELETE
        self.on_write = None    # called after each accepted write: the test's "and then…"
        self.dead = False       # every request answers 503 from now on
        self.next_id = 0
        self.swallow = set()    # methods answered "ok" but not stored (an accepted write that did not take)
        self.logins = []        # the username of every login attempt
        self.refuse_reader = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, headers=()):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def who(self):
                cookie = self.headers.get("Cookie") or ""
                return "operator" if f"TOKEN={COOKIE}" in cookie else "reader" if f"TOKEN={READER_COOKIE}" in cookie else None

            def do_GET(self):
                parts = urlsplit(self.path)
                fake.requests.append(("GET", parts.path, self.who()))
                if fake.dead:
                    return self._send(503, {"error": "dead"})
                if self.who() is None or self.headers.get("X-API-Key"):
                    # no cookie, or a key: the reader is a cookie, never a key
                    return self._send(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []})
                code, body = fake.answer(parts.path, parse_qs(parts.query))
                self._send(code, body)

            def _write(self):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                fake.requests.append((self.command, parts.path, self.who()))
                fake.writes.append((self.command, parts.path, body))
                if fake.dead:
                    return self._send(503, {"error": "dead"})
                if parts.path == "/api/auth/login":
                    fake.logins.append((body or {}).get("username"))
                    if body == {"username": OPERATOR_USER, "password": OPERATOR_VALUE}:
                        return self._send(200, {"username": OPERATOR_USER},
                                          [("Set-Cookie", f"TOKEN={COOKIE}; Path=/"), ("X-CSRF-Token", CSRF)])
                    if body == {"username": READER_USER, "password": READER_VALUE} and not fake.refuse_reader:
                        return self._send(200, {"username": READER_USER},
                                          [("Set-Cookie", f"TOKEN={READER_COOKIE}; Path=/"), ("X-CSRF-Token", READER_CSRF)])
                    return self._send(401, {"error": "invalid credentials"})
                if self.who() == "reader":
                    return self._send(403, {"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []})
                if f"TOKEN={COOKIE}" not in (self.headers.get("Cookie") or "") or self.headers.get("X-CSRF-Token") != CSRF:
                    return self._send(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []})
                code, out = fake.write(self.command, parts.path, body)
                self._send(code, out)
                if code == 200 and fake.on_write:
                    fake.on_write(self.command, parts.path, body)

            do_PUT = do_POST = do_DELETE = _write

        self.server = _Server(("127.0.0.1", 0), Handler)
        scheme = "http"
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            # a refused handshake resets the connection; the client's answer is the reading
            self.server.handle_error = lambda request, client_address: None
            scheme = "https"
        self.url = f"{scheme}://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    LEGACY_GETS = {"/rest/networkconf": "networks", "/rest/firewallrule": "firewallrules",
                   "/rest/firewallgroup": "firewallgroups", "/rest/portforward": "portforwards",
                   "/rest/user": "users", "/rest/wlanconf": "wlans", "/stat/sysinfo": "info",
                   "/stat/device": "devices", "/stat/sta": "clients"}
    V2_GETS = {"/firewall-policies": "firewallpolicies", "/firewall/zone": "firewallzones",
               "/static-dns": "dnsrecords"}

    def answer(self, path, query):
        if path in self.status:
            return self.status[path], {"error": "forced"}
        legacy = {LEGACY + k: v for k, v in self.LEGACY_GETS.items()}
        legacy[LEGACY_ROOT + "/self/sites"] = "sites"
        if path in legacy:
            if self.legacy_rc != "ok":
                return 200, {"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []}
            return 200, {"meta": {"rc": "ok"}, "data": self.data[legacy[path]]}
        v2 = {V2 + k: v for k, v in self.V2_GETS.items()}
        if path in v2:
            return 200, self.data[v2[path]]          # v2 answers a bare list
        return 404, {"error": f"no such path {path}"}

    def paths(self):
        return [p for _, p, _ in self.requests]

    COLLECTIONS = {"networkconf": "networks", "firewallrule": "firewallrules", "firewallgroup": "firewallgroups",
                   "portforward": "portforwards", "user": "users", "wlanconf": "wlans"}

    def write(self, method, path, body):
        if path in self.status:
            return self.status[path], {"error": "forced"}
        ok = lambda data: (200, {"meta": {"rc": "ok"}, "data": data})
        if path == V2 + "/static-dns" or path.startswith(V2 + "/static-dns/"):
            return self.write_v2(method, path[len(V2 + "/static-dns"):].strip("/"), body)
        if path in (LEGACY + "/cmd/stamgr", LEGACY + "/cmd/devmgr") and method == "POST":
            if body.get("cmd") in ("block-sta", "unblock-sta"):
                for c in self.data["clients"]:
                    if c["mac"] == body.get("mac"):
                        c["blocked"] = body["cmd"] == "block-sta"
            return ok([])
        rest = path[len(LEGACY + "/rest/"):].split("/") if path.startswith(LEGACY + "/rest/") else []
        name = self.COLLECTIONS.get(rest[0]) if rest else None
        if not name:
            return 404, {"error": f"no such path {path}"}
        items = self.data[name]
        if method in self.swallow:
            return ok([{**(body or {}), "_id": "ghost"}] if method == "POST" else [body])
        if method == "POST" and len(rest) == 1:
            self.next_id += 1
            obj = {**body, "_id": f"new-{self.next_id}"}
            items.append(obj)
            return ok([obj])
        if len(rest) != 2:
            return 404, {"error": f"no such path {path}"}
        idx = next((i for i, o in enumerate(items) if o.get("_id") == rest[1]), None)
        if idx is None:
            return 400, {"meta": {"rc": "error", "msg": "api.err.IdInvalid"}, "data": []}
        if method == "PUT":
            # legacy rest PUT merges: FAC1 kept fixed_ip/use_fixedip through a
            # partial PUT (k8scc#67 finding 4), so a key absent from the body stays
            items[idx] = {**items[idx], **body}
            return ok([items[idx]])
        if method == "DELETE":
            del items[idx]
            return ok([])
        return 405, {"error": method}

    def write_v2(self, method, oid, body):
        """v2 static-dns as FAC1 answered fleet-ops-b0 (k8scc#67 finding 7): POST
        and PUT answer the object itself, a PUT must carry the whole object
        (a partial body is 400), DELETE answers 200. The DELETE body is not on
        record; this fake answers an empty one."""
        items = self.data["dnsrecords"]
        if method in self.swallow:
            return 200, {**(body or {}), "_id": "ghost"}
        if method == "POST" and not oid:
            self.next_id += 1
            obj = {**body, "_id": f"new-{self.next_id}"}
            items.append(obj)
            return 200, obj
        idx = next((i for i, o in enumerate(items) if o.get("_id") == oid), None)
        if not oid or idx is None:
            return 404, {"code": "api.err.NotFound"}
        if method == "PUT":
            if set(items[idx]) - {"_id"} - set(body or {}):
                return 400, {"code": "api.err.InvalidPayload"}
            items[idx] = {**body, "_id": oid}
            return 200, items[idx]
        if method == "DELETE":
            del items[idx]
            return 200, None
        return 405, {"error": method}

    def puts(self):
        return [(p, b) for m, p, b in self.writes if m == "PUT"]

    def mutations(self):
        """Writes that change router state (not the operator login)."""
        return [(m, p, b) for m, p, b in self.writes if p != "/api/auth/login"]


class FakeFAC:
    """GitHub's issues API, the part the server uses. pre_ack / post_ack decide
    what FAC comments. FAC's comments are real ones, kept in the order they
    were left, as GitHub lists them (k8scc#126): the pre-ack at the first read
    once pre_ack is set; the post-ack at the first read after `after` says the
    write happened, or, with post_ack = "early", right behind the pre-ack,
    before anything was applied. fail_comment makes one of the server's own
    comments fail: the one whose text contains it."""

    def __init__(self):
        self.issues = {}            # number -> {"title", "body", "state", "comments": [{"id", "body"}]}
        self.tokens = []            # the Authorization header of every request
        self.pre_ack = False
        self.post_ack = False
        self.after = lambda: True
        self.fail = False
        self.fail_comment = None
        self.no_id_for = None       # a server comment containing this is stored, and answered without an id
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else None

            def handle_one(self):
                fake.tokens.append(self.headers.get("Authorization"))
                if fake.fail:
                    return self._send(500, {"message": "forced"})
                parts = urlsplit(self.path).path.split("/")   # '', repos, owner, repo, issues, [n], [comments]
                body = self._body() if self.command in ("POST", "PATCH") else None
                if parts[4:] == ["issues"] and self.command == "POST":
                    n = len(fake.issues) + 1
                    fake.issues[n] = {"title": body["title"], "body": body["body"], "state": "open", "comments": []}
                    return self._send(201, {"number": n})
                n = int(parts[5])
                issue = fake.issues[n]
                def next_id():
                    return 1000 + sum(len(i["comments"]) for i in fake.issues.values())
                if parts[6:] == ["comments"] and self.command == "POST":
                    if fake.fail_comment and fake.fail_comment in body["body"]:
                        return self._send(502, {"message": "forced for this comment"})
                    cid = next_id()
                    issue["comments"].append({"id": cid, "body": body["body"]})
                    if fake.no_id_for and fake.no_id_for in body["body"]:
                        return self._send(201, {"body": body["body"]})
                    return self._send(201, {"id": cid})
                if parts[6:] == ["comments"] and self.command == "GET":
                    def fac_says(kind, text):
                        if kind not in issue.setdefault("fac", set()):
                            issue["fac"].add(kind)
                            issue["comments"].append({"id": next_id(), "body": text, "fac": True})
                    if fake.pre_ack:
                        fac_says("pre", "pre-ack: fleet-ops-65 [69d6d7] reached the cluster from outside")
                    if fake.post_ack == "early" and "pre" in issue.get("fac", ()):
                        fac_says("post", "post-ack: fleet-ops-65 [69d6d7] still reach it")
                    elif fake.post_ack is True and fake.after():
                        fac_says("post", "post-ack: fleet-ops-65 [69d6d7] still reach it")
                    return self._send(200, [{"id": c["id"], "body": c["body"]} for c in issue["comments"]])
                if self.command == "PATCH":
                    issue["state"] = body["state"]
                    return self._send(200, {"number": n, "state": issue["state"]})
                return self._send(404, {"message": "Not Found"})

            do_GET = do_POST = do_PATCH = handle_one

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posted(self, n):
        """The comments the server itself posted on issue n."""
        return [c["body"] for c in self.issues[n]["comments"] if not c.get("fac")]

    def thread(self, n):
        """Every comment on issue n in order, as ('fac' | 'server', first line)."""
        return [("fac" if c.get("fac") else "server", c["body"].split("\n")[0]) for c in self.issues[n]["comments"]]


class Probe:
    """A local TCP listener standing in for one outside endpoint."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.addr = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.up = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.up:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def stop(self):
        """Stop listening, and prove it. On Linux, close() on a socket that
        another thread is blocked in accept() on does not stop it listening:
        the kernel keeps the socket alive for the blocked call, so connects
        still succeed (k8scc CI on 9da2a42: 11 cases that stop a probe failed
        on ubuntu-latest and passed on macOS, where close() wakes accept()).
        shutdown() first wakes the accept and stops the listen on both."""
        self.up = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass   # macOS: ENOTCONN on a listening socket; close() below suffices there
        self.sock.close()
        host, port = self.addr.rsplit(":", 1)
        try:
            socket.create_connection((host, int(port)), timeout=0.5).close()
        except OSError:
            return
        raise AssertionError(f"harness, not guard: probe {self.addr} still accepts connections after stop()")


class FakeDNS:
    """One UDP socket per address the server asks (`_dns_endpoint` is patched
    to hand out these), answering SOA queries with the mode set for that
    address: noerror (default; the SOA in the answer section), nxdomain (the
    SOA in the authority section), nosoa (NOERROR with no record at all),
    servfail, refused, or silent. `soa[ip]` is
    that address's (MNAME, serial); `forward[(ip, domain)] = other` makes ip
    answer for that domain as other does, which is what a gateway with the
    forward in effect looks like."""
    RCODE = {"noerror": 0, "nosoa": 0, "servfail": 2, "nxdomain": 3, "refused": 5}
    PUBLIC = ("ns1.public.test", 2026112201)

    def __init__(self):
        self.modes, self.socks, self.queries, self.soa, self.forward = {}, {}, [], {}, {}
        self.stale = {}   # (ip, domain) -> serial: a cached answer, same MNAME, an older serial

    def endpoint(self, ip):
        if ip not in self.socks:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            self.socks[ip] = sock
            threading.Thread(target=self._serve, args=(ip, sock), daemon=True).start()
        return "127.0.0.1", self.socks[ip].getsockname()[1]

    @staticmethod
    def _name(name):
        return b"".join(bytes([len(lb)]) + lb.encode() for lb in name.split(".")) + b"\x00"

    def _serve(self, ip, sock):
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                return
            q, i = [], 12
            while data[i]:
                q.append(data[i + 1:i + 1 + data[i]].decode())
                i += 1 + data[i]
            question = data[12:i + 5]
            self.queries.append((ip, ".".join(q), int.from_bytes(data[i + 1:i + 3], "big")))
            who = self.forward.get((ip, ".".join(q).lower()), ip)
            mode = self.modes.get(ip, "noerror")
            if mode == "noerror" and who != ip:
                mode = self.modes.get(who, "noerror")
            if mode == "silent":
                continue
            mname, serial = self.soa.get(who, self.PUBLIC)
            serial = self.stale.get((ip, ".".join(q).lower()), serial)
            rdata = self._name(mname) + self._name("hostmaster." + mname) + serial.to_bytes(4, "big") + b"\x00" * 16
            rr = b"\xc0\x0c" + b"\x00\x06\x00\x01" + (300).to_bytes(4, "big") + len(rdata).to_bytes(2, "big") + rdata
            an, ns = (1, 0) if mode == "noerror" else (0, 1) if mode == "nxdomain" else (0, 0)
            head = data[:2] + bytes([0x81, 0x80 | self.RCODE[mode]]) + b"\x00\x01" + bytes([0, an, 0, ns, 0, 0])
            sock.sendto(head + question + (rr if an or ns else b""), addr)

    def asked(self, ip):
        return [name for who, name, _ in self.queries if who == ip]

    def close(self):
        for sock in self.socks.values():
            sock.close()


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.key = d / "reader"
        self.key.write_text(f"{READER_USER}\n{READER_VALUE}\n")
        self.oneline = d / "reader-oneline"
        self.oneline.write_text("a-bare-api-key-not-an-account\n")
        self.empty = d / "empty.key"
        self.empty.write_bytes(b"")
        self.operator = d / "operator"
        self.operator.write_text(f"{OPERATOR_USER}\n{OPERATOR_VALUE}\n")
        self.bad_operator = d / "bad-operator"
        self.bad_operator.write_text(f"{OPERATOR_USER}\nwrong-password\n")
        self.ticket_token = d / "ticket.token"
        self.ticket_token.write_text(TICKET_TOKEN + "\n")
        self.snapshots = d / "snapshots"
        self.snapshots.mkdir()
        self.ca = d / "ca.pem"
        self.ca.write_text("-----BEGIN CERTIFICATE-----\nnot a real one\n-----END CERTIFICATE-----\n")
        self.fake = FakeController()
        self.fac = FakeFAC()
        self.probes = [Probe(), Probe(), Probe()]
        self.dns = FakeDNS()
        self.mod = None

    def close(self):
        for p in self.probes:
            if p.up:
                p.stop()
        self.fake.close()
        self.fac.close()
        self.dns.close()
        self.tmp.cleanup()

    def load(self, *, url="fake", key="ok", insecure=True, site="", ca="", write=False, **over):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["UNIFI_CONTROLLER_URL"] = self.fake.url if url == "fake" else url
        if key == "ok":
            os.environ["UNIFI_READER_FILE"] = str(self.key)
        elif key == "empty":
            os.environ["UNIFI_READER_FILE"] = str(self.empty)
        elif key == "missing":
            os.environ["UNIFI_READER_FILE"] = str(self.key) + ".absent"
        elif key == "oneline":
            os.environ["UNIFI_READER_FILE"] = str(self.oneline)
        elif key == "old":          # the previous image's input only: a rollout half landed
            os.environ["UNIFI_READER_KEY_FILE"] = str(self.oneline)
        elif key != "unset":
            raise ValueError(key)
        if insecure:
            os.environ["NETDEV_ALLOW_INSECURE_HTTP"] = "1"
        if site:
            os.environ["UNIFI_SITE"] = site
        if ca:
            os.environ["UNIFI_CA_FILE"] = str(self.ca) if ca == "ok" else ca
        os.environ["NETDEV_READ_TIMEOUT"] = "5"
        if write:
            env = {"UNIFI_OPERATOR_FILE": str(self.operator), "IM_FAC_TICKET_REPO": "ferry133/fac-tickets",
                   "IM_FAC_TICKET_TOKEN_FILE": str(self.ticket_token), "IM_FAC_TICKET_API": self.fac.url,
                   "NETDEV_OUTSIDE_PROBES": ",".join(p.addr for p in self.probes), "POD_IP": "10.9.9.62",
                   "NETDEV_SNAPSHOT_DIR": str(self.snapshots), "NETDEV_WITNESS_WAIT": "0.3",
                   "NETDEV_WITNESS_POLL": "0.05", "NETDEV_HEALTH_TIMEOUT": "0.6", "NETDEV_HEALTH_INTERVAL": "0.05",
                   "NETDEV_PROBE_TIMEOUT": "0.5", "CLUSTER_NAME": "jg-test", "CLAUDE_USER_ID": "im-test",
                   "NETDEV_DNS_SETTLE": "0.05"}
            env.update({k: v for k, v in over.items() if v is not None})
            for k in [k for k, v in over.items() if v is None]:
                env.pop(k, None)
            os.environ.update(env)
        import netdev_mcp_server
        self.mod = importlib.reload(netdev_mcp_server)
        # the fake's URL is 127.0.0.1; the fixture's controller is the gateway
        self.mod._controller_ip = lambda: "10.9.9.1"
        self.mod._dns_endpoint = self.dns.endpoint   # every DNS query goes to the fake, by address
        self.fake.requests.clear()
        self.fake.writes.clear()
        self.fake.logins.clear()
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
            self.assertTrue(out.startswith("[via legacy]"), out)
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

    def test_missing_reader_answers_not_configured_naming_it(self):
        for state in ("unset", "missing", "empty", "oneline", "old"):
            with self.subTest(key=state):
                m = self.h.load(key=state)
                for name in sorted(READ_TOOLS):
                    out, _ = self.h.call(getattr(m, name))
                    self.assertIn("not configured", out, name)
                    self.assertIn("reader credential (UNIFI_READER_FILE", out, name)
                self.assertEqual(self.h.fake.requests, [])
                self.assertEqual("UNIFI_READER_KEY_FILE is set but no longer read" in out, state == "old", out)
                self.assertEqual("expected two lines" in out, state == "oneline", out)
        with self.subTest(control="the account present: data, not a refusal"):
            m = self.h.load()
            self.assertNotIn("not configured", self.h.call(m.get_devices)[0])

    def test_reader_logs_in_as_its_own_account_and_never_sends_a_key(self):
        m = self.h.load(write=True)
        self.h.call(m.get_devices)
        self.h.call(m.get_networks)
        self.assertEqual(self.h.fake.logins, [READER_USER], "one reader login, then the cookie")
        gets = [(p, who) for meth, p, who in self.h.fake.requests if meth == "GET"]
        self.assertTrue(gets)
        self.assertEqual({who for _, who in gets}, {"reader"}, "every read carries the reader's cookie, never the operator's")
        src = (ROOT / "netdev_mcp_server.py").read_text()
        self.assertNotIn('"X-API-Key"', src)
        with self.subTest(control="the fake refuses a key-only or cookieless GET, so a key path would show"):
            import httpx
            self.assertEqual(httpx.get(self.h.fake.url + LEGACY + "/stat/device", headers={"X-API-Key": "k"}).status_code, 401)
        with self.subTest(control="a write with the reader's cookie is answered 403 by the fake, like FAC1"):
            import httpx
            c = httpx.Client(base_url=self.h.fake.url)
            c.post("/api/auth/login", json={"username": READER_USER, "password": READER_VALUE})
            r = c.put(LEGACY + "/rest/user/u-tv", json={"note": "x"}, headers={"X-CSRF-Token": READER_CSRF})
            self.assertEqual(r.status_code, 403)

    def test_reader_session_expiry_logs_in_again_once(self):
        m = self.h.load()
        self.h.call(m.get_devices)
        m._controller()._session._client.cookies.clear()     # the console dropped the session
        out = self.h.call(m.get_devices)[0]
        self.assertIn("dev-gw", out)
        self.assertEqual(self.h.fake.logins, [READER_USER, READER_USER])
        with self.subTest(control="a refused login is an answer naming the refusal, not data, not the password"):
            m = self.h.load()
            self.h.fake.refuse_reader = True
            out = self.h.call(m.get_devices)[0]
            self.assertIn("controller refused the reader login (401)", out)
            self.assertNotIn(READER_VALUE, out)
            self.assertNotIn("dev-gw", out)

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
        for value in (READER_VALUE, OPERATOR_VALUE, READER_COOKIE, READER_CSRF):
            self.assertNotIn(value, joined)
        self.assertNotIn(READER_VALUE, json.dumps(dict(os.environ)), "the password must not be exported to env")
        with self.subTest(control="the password really was in play: the fake received it at login"):
            self.assertIn({"username": READER_USER, "password": READER_VALUE},
                          [b for _, p, b in self.h.fake.writes if p == "/api/auth/login"])


def _openssl(*args, cwd):
    """openssl must exist: a TLS control that cannot make its certificate is
    not measured, and must not read as passed."""
    r = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"harness, not guard: openssl {' '.join(args)}: {r.stderr.strip()}")


# What a CA writes into the leaves it signs. Without the authority key id the
# strict check, on by default since Python 3.13, refuses the leaf before the
# hostname is looked at ("Missing Authority Key Identifier", k8scc#119), and
# the cell then measures the fixture, not the server.
LEAF_IDS = "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n"


def make_certs(d: pathlib.Path) -> dict:
    """Certificates shaped like the ones that matter:
    - fac1: FAC1's factory certificate as fleet-ops-b0 read it (2026-11-14):
      self-signed CN=unifi.local; keyUsage incl. Certificate Sign; its EKUs;
      no basicConstraints, no SKI/AKI. Its SANs minus IP:127.0.0.1, which is
      where the fake listens: with it a hostname check would pass and the
      control below would stop discriminating (fleet-ops-b0).
    - fac1_child: signed by fac1's key, under a name of its own. OpenSSL
      counts fac1 as a CA, so this chains to it; only the identity check
      refuses it. With fac1's own name as its subject it looked self-issued,
      and OpenSSL 4.1 refused it as "self-signed certificate" before the
      identity check was reached (k8scc#119).
    - plain: self-signed CN=unifi.local with no extensions besides SKI.
    - other: another such certificate, a stranger's.
    - root + leaf_ip / leaf_name: a customer's own CA (CA:TRUE) and two leaves
      it signed, one valid for 127.0.0.1 and one only for unifi.local; chain =
      root + leaf_ip, a two-certificate file."""
    (d / "plain.cnf").write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=unifi.local\n")
    (d / "fac1.cnf").write_text(
        "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=e\n[dn]\nCN=unifi.local\n[e]\n"
        "keyUsage=digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment,keyCertSign\n"
        "extendedKeyUsage=serverAuth,clientAuth,codeSigning,timeStamping\n"
        "subjectAltName=DNS:unifi.local,DNS:localhost,DNS:[::1],IP:FE80::1\n"
        "subjectKeyIdentifier=none\nauthorityKeyIdentifier=none\n")
    (d / "ca.cnf").write_text("[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3\n[dn]\nCN=Customer Root CA\n"
                              "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    for name, cnf in (("fac1", "fac1.cnf"), ("plain", "plain.cnf"), ("other", "plain.cnf"), ("root", "ca.cnf")):
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.pem",
                 "-days", "2", "-config", cnf, cwd=d)
    (d / "child.cnf").write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=behind.unifi.local\n")
    for name, issuer, ext, cnf in (("fac1_child", "fac1", "", "child.cnf"),
                                   ("leaf_ip", "root", "subjectAltName=IP:127.0.0.1\n" + LEAF_IDS, "plain.cnf"),
                                   ("leaf_name", "root", "subjectAltName=DNS:unifi.local\n" + LEAF_IDS, "plain.cnf")):
        (d / f"{name}.ext").write_text(ext + "basicConstraints=CA:FALSE\n")
        _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
                 "-config", cnf, cwd=d)
        _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{issuer}.pem", "-CAkey", f"{issuer}.key",
                 "-CAcreateserial", "-out", f"{name}.pem", "-days", "2", "-extfile", f"{name}.ext", cwd=d)
    (d / "two.pem").write_text((d / "fac1.pem").read_text() + (d / "other.pem").read_text())
    (d / "chain.pem").write_text((d / "root.pem").read_text() + (d / "leaf_ip.pem").read_text())
    names = ("fac1", "fac1_child", "plain", "other", "root", "leaf_ip", "leaf_name", "two", "chain")
    return {n: d / f"{n}.pem" for n in names} | {f"{n}.key": d / f"{n}.key" for n in names if (d / f"{n}.key").exists()}


class ProbeFamilies(Case):
    """k8scc#115 (fleet-ops-d5, jg-jcc2): a probe hostname that publishes A and
    AAAA sometimes resolved to AAAA only (8 of 120 lookups), and the v4-only
    node then failed the connect with ENETUNREACH while the network was fine.
    Here the lookup is faked: `dual.test` answers an unspecified-family query
    with an IPv6 address only (::1 on a closed port, so the v6 path fails at
    once), and an AF_INET query with 127.0.0.1, where the listener is."""

    HOST = "dual.test"

    def setUp(self):
        super().setUp()
        self.m = self.h.load()
        closed = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        closed.bind(("::1", 0))
        self.v6_port = closed.getsockname()[1]
        closed.close()
        self.v4_port = None
        self.asked = []
        real = socket.getaddrinfo

        def fake(host, port, family=0, type=0, proto=0, flags=0):
            if host != self.HOST:
                return real(host, port, family, type, proto, flags)
            self.asked.append(family)
            if family == socket.AF_INET:
                if self.v4_port is None:
                    raise socket.gaierror(socket.EAI_NONAME, "no A record (fake)")
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", self.v4_port))]
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", self.v6_port, 0, 0))]
        patcher = unittest.mock.patch("socket.getaddrinfo", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def probe(self, raw):
        probes, why = self.m._parse_probes(",".join([raw, "127.0.0.1:1", "127.0.0.1:2"]))
        self.assertEqual(why, "")
        return self.m._probe(probes[0])

    def test_a_tcp_probe_whose_lookup_lost_the_a_record_still_connects_over_ipv4(self):
        self.v4_port = self.h.probes[0].sock.getsockname()[1]
        self.assertEqual(self.probe(f"{self.HOST}:{self.v4_port}"), "")
        self.assertEqual(set(self.asked), {socket.AF_INET}, "only an A lookup is made")
        with self.subTest(control="the unspecified lookup really is v6-only and does fail"):
            with self.assertRaises(OSError):
                socket.create_connection((self.HOST, self.v4_port), timeout=1).close()

    def test_an_https_probe_whose_lookup_lost_the_a_record_still_connects_over_ipv4(self):
        d = pathlib.Path(self.h.tmp.name)
        certs = make_certs(d)
        (d / "dual.ext").write_text(f"subjectAltName=DNS:{self.HOST}\nbasicConstraints=CA:FALSE\n" + LEAF_IDS)
        _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "dual.key", "-out", "dual.csr",
                 "-config", "plain.cnf", cwd=d)
        _openssl("x509", "-req", "-in", "dual.csr", "-CA", "root.pem", "-CAkey", "root.key", "-CAcreateserial",
                 "-out", "dual.pem", "-days", "2", "-extfile", "dual.ext", cwd=d)

        class Ok(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204 if self.headers.get("Host", "").split(":")[0] == ProbeFamilies.HOST else 421)
                self.end_headers()

            def log_message(self, *a):
                pass

        class Server(ThreadingHTTPServer):
            def server_bind(self):   # no getfqdn(): see FakeController
                socketserver.TCPServer.server_bind(self)
        srv = Server(("127.0.0.1", 0), Ok)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(d / "dual.pem"), str(d / "dual.key"))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.v4_port = srv.server_address[1]
        os.environ["SSL_CERT_FILE"] = str(certs["root"])
        self.addCleanup(os.environ.pop, "SSL_CERT_FILE", None)
        self.assertEqual(self.probe(f"https://{self.HOST}:{self.v4_port}/minio/health/live"), "")
        self.assertNotIn(socket.AF_INET6, self.asked)
        with self.subTest(control="the name is verified: a certificate for another name is refused"):
            ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx2.load_cert_chain(str(certs["leaf_name"]), str(certs["leaf_name.key"]))
            # a second server with the unifi.local leaf, behind the same A answer
            srv2 = Server(("127.0.0.1", 0), Ok)
            srv2.socket = ctx2.wrap_socket(srv2.socket, server_side=True)
            threading.Thread(target=srv2.serve_forever, daemon=True).start()
            self.addCleanup(srv2.server_close)
            self.addCleanup(srv2.shutdown)
            self.v4_port = srv2.server_address[1]
            why = self.probe(f"https://{self.HOST}:{self.v4_port}/x")
            self.assertIn("CERTIFICATE_VERIFY_FAILED", why)

    def test_no_a_record_is_lost_and_says_so(self):
        why = self.probe(f"{self.HOST}:443")
        self.assertIn("no IPv4 address", why)
        self.assertNotIn(socket.AF_INET6, self.asked)

    def test_an_ipv6_literal_probe_is_refused_at_startup(self):
        for raw in ("[2001:db8::1]:443", "https://[2001:db8::1]/x"):
            with self.subTest(probe=raw):
                probes, why = self.m._parse_probes(",".join([raw, "127.0.0.1:1", "127.0.0.1:2"]))
                self.assertEqual(probes, [])
                self.assertIn("IPv4", why)
        with self.subTest(control="an IPv4 literal and a hostname are accepted"):
            self.assertEqual(self.m._parse_probes("1.1.1.1:443,https://api.github.com/x,dual.test:443")[1], "")


class TLS(Case):
    """The fake served over real TLS: what the console's certificate does to
    the handshake is the one thing a plain-http fake cannot show."""

    def setUp(self):
        super().setUp()
        self.certs = make_certs(pathlib.Path(self.h.tmp.name))

    def serve(self, name):
        self.h.fake.close()
        self.h.fake = FakeController(tls=(self.certs[name], self.certs[f"{name}.key"]))

    def devices(self, ca):
        m = self.h.load(ca=str(self.certs[ca]), insecure=False)
        return m, self.h.call(m.get_devices)[0]

    def test_the_fixture_is_fac1_shaped(self):
        # the readings fleet-ops-b0 took on FAC1's own pem, reproduced here, so
        # a fixture that drifts from it shows (the first fixture had no
        # keyCertSign, read as non-CA, and let a wrong rule pass)
        ctx = ssl.create_default_context(cafile=str(self.certs["fac1"]))
        self.assertEqual(ctx.cert_store_stats()["x509_ca"], 1, "OpenSSL counts it as a CA, as on FAC1")
        self.serve("fac1")
        import httpx
        strict = ssl.create_default_context(cafile=str(self.certs["fac1"]))
        strict.check_hostname = False
        strict.verify_flags |= ssl.VERIFY_X509_STRICT       # Python ≥ 3.13's default; set here so 3.11/3.12 measure it too
        with self.assertRaisesRegex(httpx.ConnectError, "invalid CA certificate"):
            httpx.get(self.h.fake.url + LEGACY + "/stat/device", verify=strict)
        # the first image's context: refused on 3.13 by strict, on 3.11 by the hostname
        with self.assertRaisesRegex(httpx.ConnectError, "IP address mismatch|not valid for|invalid CA certificate"):
            httpx.get(self.h.fake.url + LEGACY + "/stat/device", verify=str(self.certs["fac1"]))

    def test_fac1_cert_pinned_is_read(self):
        self.serve("fac1")
        m, out = self.devices("fac1")
        self.assertIn("dev-gw", out)
        self.assertIn("TLS: pinned certificate", self.h.readings())
        with self.subTest(control="the plain-shaped factory certificate is pinned the same way"):
            self.serve("plain")
            self.assertIn("dev-gw", self.devices("plain")[1])

    def test_a_certificate_the_pin_signed_is_refused(self):
        self.serve("fac1_child")
        m, out = self.devices("fac1")
        self.assertIn("error:", out)
        self.assertIn("not the one in UNIFI_CA_FILE", out)
        self.assertNotIn("dev-gw", out)
        self.assertEqual(self.h.fake.requests, [], "nothing reached the console, not even the login")
        with self.subTest(control="it does chain: without the identity check the same anchor accepts it"):
            import httpx
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.load_verify_locations(cafile=str(self.certs["fac1"]))
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            self.assertEqual(httpx.get(self.h.fake.url + LEGACY + "/stat/device", verify=ctx).status_code, 401)

    def test_another_certificate_is_refused(self):
        self.serve("fac1")
        m, out = self.devices("other")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", out)
        self.assertNotIn("dev-gw", out)
        self.assertEqual(self.h.fake.requests, [])

    def test_a_single_ca_is_a_pin_not_a_licence(self):
        # a root CA alone in the file is pinned like any one certificate: what
        # it signed is refused, never read with the hostname off
        self.serve("leaf_ip")
        m, out = self.devices("root")
        self.assertIn("not the one in UNIFI_CA_FILE", out)
        self.assertNotIn("dev-gw", out)

    def test_two_certificates_keep_the_hostname_check(self):
        # one of them is the very certificate served (fleet-ops-b0's edge)
        self.serve("fac1")
        m, out = self.devices("two")
        self.assertIn("error:", out)
        self.assertNotIn("dev-gw", out)
        self.assertIn("TLS: CA + hostname (2 certificates)", self.h.readings())

    def test_a_chain_is_ca_mode_with_the_hostname_checked(self):
        self.serve("leaf_name")
        m, out = self.devices("chain")
        self.assertIn("IP address mismatch", out, "the hostname check is what refused it")
        with self.subTest(control="the same chain reads a leaf valid for the address dialled"):
            self.serve("leaf_ip")
            self.assertIn("dev-gw", self.devices("chain")[1])


class StartupStates(Case):
    def test_reader_only_for_each_missing_input(self):
        cases = {
            "operator file absent": ({"UNIFI_OPERATOR_FILE": None}, "operator credential absent"),
            "operator login refused": ({"UNIFI_OPERATOR_FILE": str(self.h.bad_operator)}, "refused the operator login"),
            "ticket repo unset": ({"IM_FAC_TICKET_REPO": None}, "IM_FAC_TICKET_REPO unset"),
            "ticket token missing": ({"IM_FAC_TICKET_TOKEN_FILE": str(self.h.ticket_token) + ".absent"}, "token missing"),
            "two probes": ({"NETDEV_OUTSIDE_PROBES": ",".join(p.addr for p in self.h.probes[:2])}, "exactly three"),
            # k8scc#49: urlsplit().port raises on these, and the parser runs at import
            **{f"probe port {bad}": ({"NETDEV_OUTSIDE_PROBES": ",".join([p.addr for p in self.h.probes[:2]]
                                                                        + [f"https://probe.invalid:{bad}/x"])},
                                     f"probe 'https://probe.invalid:{bad}/x' has an invalid port")
               for bad in ("abc", "99999", "0")},
            # str.isdigit() is True for superscripts, and int() then raises at import (fleet-ops-65, k8scc#51)
            "tcp probe port ²": ({"NETDEV_OUTSIDE_PROBES": ",".join([p.addr for p in self.h.probes[:2]] + ["probe.invalid:5²"])},
                                 "probe 'probe.invalid:5²': expected host:port"),
            "POD_IP unset": ({"POD_IP": None}, "POD_IP unset"),
            "snapshot dir unset": ({"NETDEV_SNAPSHOT_DIR": None}, "NETDEV_SNAPSHOT_DIR unset"),
        }
        for name, (over, said) in cases.items():
            with self.subTest(missing=name):
                self.h.load(write=True, **over)
                self.assertEqual(self.h.tools(), READ_TOOLS)
                readings = self.h.readings()
                self.assertIn("write tools: not registered", readings)
                self.assertIn(said, readings)
        with self.subTest(control="all present: the write tools are listed"):
            self.h.load(write=True)
            self.assertEqual(self.h.tools(), READ_TOOLS | WRITE_TOOLS)
            self.assertIn("write tools: registered", self.h.readings())

    def test_operator_login_is_measured_at_startup(self):
        m = self.h.load(write=True)
        self.assertEqual(m.OPERATOR_STATE, "ok")
        self.assertIn("operator credential: ok", self.h.readings())
        with self.subTest(control="a wrong password reads as invalid, naming the refusal, not the password"):
            self.h.load(write=True, UNIFI_OPERATOR_FILE=str(self.h.bad_operator))
            self.assertIn("invalid", self.h.mod.OPERATOR_STATE)
            self.assertNotIn("wrong-password", self.h.readings())

    def test_forbidden_verbs_have_no_tool(self):
        self.h.load(write=True)
        tools = self.h.tools()
        self.assertEqual(tools, READ_TOOLS | WRITE_TOOLS)
        forbidden = re.compile(r"vlan|network|wlan|passphrase|psk|firmware|upgrade|wan(?!_port)|site|policy|apikey|api_key|"
                               r"admin|account|firewall", re.I)   # dns: set/clear_dns_forward since k8scc#109
        writers = [t for t in asyncio.run(self.h.mod.app.list_tools()) if t.name in WRITE_TOOLS]
        for t in writers:
            self.assertIsNone(forbidden.search(t.name), t.name)
            self.assertIsNone(forbidden.search(" ".join(t.inputSchema.get("properties", {}))), t.name)
        with self.subTest(control="the pattern catches a forbidden name"):
            self.assertTrue(forbidden.search("set_wlan_passphrase"))

    def test_confirm_is_a_string_token(self):
        m = self.h.load(write=True)
        for fn in m.WRITE_TOOLS:
            sig = inspect.signature(fn)
            self.assertIs(sig.parameters["confirm"].annotation, str, fn.__name__)
            self.assertEqual(sig.parameters["confirm"].default, "", fn.__name__)
            self.assertIs(sig.parameters["reason"].annotation, str, fn.__name__)

    def test_not_configured_still_registers_and_answers(self):
        m = self.h.load(key="unset")
        self.assertEqual(self.h.tools(), READ_TOOLS)
        self.assertIn("reader credential: missing", self.h.readings())
        with self.subTest(control="configured: exactly ok (jg-base daily-check row 31 reads anything else as fail)"):
            self.h.load()
            self.assertIn("reader credential: ok", self.h.readings().splitlines())
        with self.subTest(control="a refused reader login shows on the version line, not as data"):
            self.h.load()                      # a fresh process: no session yet
            self.h.fake.refuse_reader = True
            self.assertIn("controller version: unreadable", self.h.readings())
            self.assertIn("refused the reader login", self.h.readings())

    def test_startup_reading_names_the_controller_version(self):
        self.h.load()
        self.assertIn("controller version: 11.0.99", self.h.readings())
        with self.subTest(control="an unreachable sysinfo reads as unreadable, not as a version"):
            self.h.fake.status[f"{LEGACY}/stat/sysinfo"] = 503
            self.assertIn("controller version: unreadable", self.h.readings())

    def test_routes_name_what_is_not_yet_read(self):
        self.h.load()
        readings = self.h.readings()
        self.assertIn("firewallpolicies=v2", readings)
        self.assertIn("not yet read on a console", readings)
        self.assertNotIn("integration", readings)


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
        # the whole list goes: FAC1's rows were empty lists, so their shape is
        # unread, and a key whose name says "keys" is a secret (finding 2)
        self.assertEqual(wlan["private_preshared_keys"], "<stripped>")
        self.assertEqual(wlan["security"], "wpapsk", "non-secrets survive")
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
                 "name": False, "x": False, "key_count": False, "networkconf_id": False, "psk_enabled_x": True,
                 "private_preshared_keys": True, "api_keys": True, "preshared_list": True, "network_ids": False}
        for key, secret in cases.items():
            got = m._strip({key: "v"})[key]
            self.assertEqual(got == "<stripped>", secret, key)

    def test_path_marker_follows_the_route_table(self):
        m = self.h.load()
        expect = {"get_sites": "legacy-root", "get_controller_info": "legacy", "get_devices": "legacy",
                  "get_clients": "legacy", "get_lan_inventory": "legacy", "get_networks": "legacy",
                  "get_firewall_rules": "legacy+v2", "get_port_forwards": "legacy",
                  "get_dhcp_reservations": "legacy", "get_wlans": "legacy", "get_dns_records": "v2"}
        for name, api in expect.items():
            out, _ = self.h.call(getattr(m, name))
            self.assertEqual(out.split("\n", 1)[0], f"[via {api}]", name)
            self.assertNotIn("error:", out, name)
        with self.subTest(control="moving a route moves the marker and the request"):
            m.ROUTES["networks"] = ("v2", "/firewall/zone")
            out, _ = self.h.call(m.get_networks)
            self.assertEqual(out.split("\n", 1)[0], "[via v2]")
            self.assertIn(f"{V2}/firewall/zone", self.h.fake.paths())

    def test_site_names_the_legacy_path(self):
        m = self.h.load(site="nosuchsite")
        out = self.h.call(m.get_devices)[0]
        self.assertIn("/proxy/network/api/s/nosuchsite/stat/device", out)
        self.assertIn("404", out)
        with self.subTest(control="the default site answers"):
            m = self.h.load()
            self.assertIn("dev-gw", self.h.call(m.get_devices)[0])

    def test_a_v2_answer_that_is_not_a_list_is_an_error(self):
        m = self.h.load()
        self.h.fake.data["firewallpolicies"] = {"unexpected": True}
        out = self.h.call(m.get_firewall_rules)[0]
        self.assertIn("error:", out)
        self.assertIn("not a list", out)
        with self.subTest(control="a list answers"):
            self.h.fake.data["firewallpolicies"] = []
            self.assertNotIn("error:", self.h.call(m.get_firewall_rules)[0])

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
        rules = {r["_id"]: r for r in body(self.h.call(m.get_firewall_rules)[0])["legacy_rules"]}
        self.assertEqual(rules["rule-ctrl"]["dst_expanded"], ["10.9.9.1", "10.9.9.200-10.9.9.210"])
        self.assertEqual(rules["rule-ctrl"]["src_expanded"], "any")
        self.assertEqual(rules["rule-net"]["src_expanded"], ["10.9.9.1/24"])
        with self.subTest(control="a group that is not in the controller's answer is named, not dropped"):
            self.h.fake.data["firewallgroups"] = [g for g in FIXTURES["firewallgroups"] if g["_id"] != "grp-ctrl"]
            rules = {r["_id"]: r for r in body(self.h.call(m.get_firewall_rules)[0])["legacy_rules"]}
            self.assertEqual(rules["rule-ctrl"]["dst_unresolved"], ["grp-ctrl"])

    def test_zone_based_policies_are_read_when_legacy_is_empty(self):
        # FAC1 (k8scc#67 finding 1): rest/firewallrule and rest/firewallgroup
        # 200 with 0 rows, 91 policies in v2
        m = self.h.load()
        self.h.fake.data["firewallrules"], self.h.fake.data["firewallgroups"] = [], []
        out = body(self.h.call(m.get_firewall_rules)[0])
        self.assertEqual(out["legacy_rules"], [])
        pols = {p["_id"]: p for p in out["policies"]}
        self.assertEqual(set(pols), {"pol-gw", "pol-node", "pol-iot", "pol-lan"})
        self.assertEqual(pols["pol-node"]["src_expanded"], ["10.9.9.62"])
        self.assertEqual(pols["pol-node"]["dst_expanded"], ["10.9.8.1/24"])
        self.assertEqual(pols["pol-gw"]["src_expanded"], ["10.9.9.1/24", "10.9.8.1/24"], "a zone expands to its networks")
        self.assertEqual(pols["pol-gw"]["dst_unresolved"], ["zone Gateway (no networks)"])
        self.assertEqual(out["zones"], "read")
        with self.subTest(case="a target type not modelled is named, not shown as any"):
            self.h.fake.data["firewallpolicies"].append(
                {"_id": "pol-mac", "name": "by client", "source": {"matching_target": "CLIENT", "client_macs": [NODE]},
                 "destination": {"matching_target": "IP", "ips": ["10.9.9.1"]}})
            pols = {p["_id"]: p for p in body(self.h.call(m.get_firewall_rules)[0])["policies"]}
            self.assertEqual(pols["pol-mac"]["src_unresolved"], ["matching_target CLIENT"])
            self.h.fake.data["firewallpolicies"].pop()
        with self.subTest(control="zones unreadable: a zone-only side is named unresolved, the rest still expands"):
            self.h.fake.status[f"{V2}/firewall/zone"] = 404
            out = body(self.h.call(m.get_firewall_rules)[0])
            pols = {p["_id"]: p for p in out["policies"]}
            self.assertIn("unreadable", out["zones"])
            self.assertEqual(pols["pol-gw"]["src_unresolved"], ["zone zone-internal (zones unreadable)"])
            self.assertEqual(pols["pol-node"]["src_expanded"], ["10.9.9.62"])
        with self.subTest(control="policies unreadable: an error, not an empty firewall"):
            self.h.fake.status[f"{V2}/firewall-policies"] = 403
            out = self.h.call(m.get_firewall_rules)[0]
            self.assertTrue(out.startswith("[via legacy+v2]\nerror:"), out)

    def test_lan_inventory_merges_devices_and_clients(self):
        m = self.h.load()
        rows = body(self.h.call(m.get_lan_inventory)[0])
        self.assertEqual(len(rows), 6)
        node = next(r for r in rows if r["ip"] == "10.9.9.62")
        self.assertEqual((node["kind"], node["mac"], node["uplink"]), ("client", "bb:bb:bb:00:00:62", "aa:aa:aa:00:00:02"))
        self.assertEqual({r["kind"] for r in rows}, {"device", "client"})

    def test_reader_results_fill_the_session_cache(self):
        m = self.h.load()
        self.assertIsNone(m._seen("clients", "cl-phone"))
        self.h.call(m.get_clients)
        self.assertEqual(m._seen("clients", "cl-phone")["ip"], "10.9.9.120")
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
                 clients=f["clients"], devices=f["devices"], rules=f["firewallrules"], groups=f["firewallgroups"],
                 policies=f["firewallpolicies"], zones=f["firewallzones"])
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
        # a legacy client names its switch by MAC (sw_mac); the device is named by _id or MAC
        for name in ("dev-sw", SW):
            with self.subTest(target=name):
                self.assertIn("attached", self.m.class_a("devices", name, **self.args()))
        with self.subTest(control="the AP the node is not on passes"):
            self.assertIsNone(self.m.class_a("devices", "dev-ap", **self.args()))

    # k8scc#104: the uplink chain, in the shape read on FAC1 (uplink.uplink_mac;
    # the gateway's uplink has none). The fixture is gw ← sw ← ap.
    def on_wifi(self):
        f = json.loads(json.dumps(FIXTURES))
        node = next(c for c in f["clients"] if c["ip"] == self.POD)
        node.pop("sw_mac"); node.pop("sw_port"); node["is_wired"] = False; node["ap_mac"] = AP
        return f["clients"]

    def test_device_between_attachment_and_gateway_refused(self):
        a = self.args(clients=self.on_wifi())
        self.assertIn("attached", self.m.class_a("devices", "dev-ap", **a))
        for name in ("dev-sw", SW):
            with self.subTest(case="node on Wi-Fi: the AP's switch", target=name):
                self.assertIn("on the path from this node to the gateway", self.m.class_a("devices", name, **a))
        with self.subTest(case="a second switch tier: node → edge → core → gateway"):
            f = json.loads(json.dumps(FIXTURES))
            edge = {"_id": "dev-edge", "name": "edge switch", "type": "usw", "mac": "aa:aa:aa:00:00:04",
                    "uplink": {"uplink_mac": SW, "uplink_remote_port": 9}}
            node = next(c for c in f["clients"] if c["ip"] == self.POD)
            node["sw_mac"] = edge["mac"]
            out = self.m.class_a("devices", "dev-sw", **self.args(clients=f["clients"], devices=f["devices"] + [edge]))
            self.assertIn("edge switch → core switch → gateway", out)
        with self.subTest(control="a device off the path passes: a second AP on the same switch"):
            f = json.loads(json.dumps(FIXTURES))
            ap2 = {"_id": "dev-ap2", "name": "hall ap", "type": "uap", "mac": "aa:aa:aa:00:00:05",
                   "uplink": {"uplink_mac": SW, "uplink_remote_port": 14}}
            self.assertIsNone(self.m.class_a("devices", "dev-ap2", **self.args(clients=self.on_wifi(),
                                                                               devices=f["devices"] + [ap2])))
        with self.subTest(control="wired on the switch: the AP is a leaf below it, not on the path"):
            self.assertIsNone(self.m.class_a("devices", "dev-ap", **self.args()))

    def test_unreadable_uplink_chain_fails_closed(self):
        def devices(**sw_uplink):
            f = json.loads(json.dumps(FIXTURES))
            next(d for d in f["devices"] if d["_id"] == "dev-sw")["uplink"] = sw_uplink
            return f["devices"]
        for case, devs in {
            "an uplink naming no device": devices(uplink_mac="aa:aa:aa:00:00:99"),
            "a loop": devices(uplink_mac=AP),
            # fleet-ops-d5's mutation on FAC1's data (#106): the switch's uplink
            # removed; the walk stopped there and the gateway left the path
            "a switch with no uplink (only a gateway may be the root)": devices(),
        }.items():
            with self.subTest(case=case):
                # the AP itself is off the path; a broken walk refuses it anyway
                out = self.m.class_a("devices", "dev-ap", **self.args(devices=devs))
                self.assertIn("cannot be read", out)
                self.assertIn("fail closed", out)
        with self.subTest(case="the attachment itself is not a device"):
            f = json.loads(json.dumps(FIXTURES))
            next(c for c in f["clients"] if c["ip"] == self.POD)["sw_mac"] = "aa:aa:aa:00:00:98"
            self.assertIn("cannot be read", self.m.class_a("devices", "dev-ap", **self.args(clients=f["clients"])))
        with self.subTest(case="node on Wi-Fi, the switch's uplink gone: the gateway is not silently dropped"):
            out = self.m.class_a("devices", "dev-gw", **self.args(clients=self.on_wifi(), devices=devices()))
            self.assertIn("names no uplink and is not a gateway", out)
        with self.subTest(control="the gateway as root with no uplink_mac is not broken"):
            self.assertIn("on the path", self.m.class_a("devices", "dev-gw", **self.args(clients=self.on_wifi())))
        with self.subTest(control="the same switch, uplink to the gateway: the AP passes"):
            self.assertIsNone(self.m.class_a("devices", "dev-ap", **self.args(devices=devices(uplink_mac=GW))))
        with self.subTest(control="a broken chain does not touch other collections"):
            self.assertIsNone(self.m.class_a("users", TV, **self.args(devices=devices(uplink_mac="aa:aa:aa:00:00:99"))))

    def test_zone_policies_covering_im_path(self):
        # legacy empty, as on FAC1: the policies alone must decide
        a = self.args(rules=[], groups=[])
        self.assertIn("internal to gateway", self.m.class_a("firewallpolicies", "pol-gw", **a))
        self.assertIn("lan to lan", self.m.class_a("firewallpolicies", "pol-lan", **a))
        with self.subTest(control="policies that miss the pod↔controller/API pairs pass"):
            self.assertIsNone(self.m.class_a("firewallpolicies", "pol-iot", **a))
            self.assertIsNone(self.m.class_a("firewallpolicies", "pol-node", **a), "node → IoT is not im's path")

    def test_unresolvable_policy_side_counts_as_covering(self):
        a = self.args(rules=[], groups=[])
        # IoT → LAN: the destination holds the controller, so the source decides
        iot_only = lambda **side: [{"_id": "p", "name": "p", "source": {"matching_target": "NETWORK", "network_ids": ["net-iot"], **side},
                                    "destination": {"matching_target": "NETWORK", "network_ids": ["net-lan"]}}]
        self.assertIsNone(self.m.class_a("firewallpolicies", "p", **{**a, "policies": iot_only()}), "baseline: misses")
        for name, pol in {
            "negated networks": iot_only(match_opposite_networks=True),
            "unknown network id": iot_only(network_ids=["gone"]),
            "a target type not modelled": [{"_id": "p", "source": {"matching_target": "CLIENT", "client_macs": [NODE]},
                                            "destination": {"matching_target": "NETWORK", "network_ids": ["net-lan"]}}],
            "zone not among the zones": [{"_id": "p", "source": {"matching_target": "ANY", "zone_id": "zone-vpn"},
                                          "destination": {"matching_target": "IP", "ips": [self.CTRL]}}],
            "side missing": [{"_id": "p", "destination": {"matching_target": "IP", "ips": [self.CTRL]}}],
        }.items():
            with self.subTest(case=name):
                self.assertIsNotNone(self.m.class_a("firewallpolicies", "p", **{**a, "policies": pol}))
        with self.subTest(case="zones unreadable: a zone-only side covers"):
            pol = [{"_id": "p", "source": {"matching_target": "ANY", "zone_id": "zone-internal"},
                    "destination": {"matching_target": "IP", "ips": [self.CTRL]}}]
            self.assertIsNotNone(self.m.class_a("firewallpolicies", "p", **{**a, "policies": pol, "zones": None}))
            self.assertIsNotNone(self.m.class_a("firewallpolicies", "p", **{**a, "policies": pol}),
                                 "and read: Internal holds the LAN, so it covers too")

    def test_missing_reader_data_fails_closed(self):
        for missing in ("networks", "clients", "devices", "rules", "groups", "policies"):
            with self.subTest(missing=missing):
                why = self.m.class_a("networks", "net-iot", **self.args(**{missing: None}))
                self.assertIn("fail closed", why)
        self.assertIn("POD_IP", self.m.class_a("networks", "net-iot", **self.args(pod_ip="")))
        with self.subTest(control="every input present: the same target passes"):
            self.assertIsNone(self.m.class_a("networks", "net-iot", **self.args()))


# --- the operator half -----------------------------------------------------------------

PHONE, TV, NODE = "bb:bb:bb:00:01:20", "bb:bb:bb:00:01:30", "bb:bb:bb:00:00:62"
GW, SW, AP = "aa:aa:aa:00:00:01", "aa:aa:aa:00:00:02", "aa:aa:aa:00:00:03"
ARGS = {
    "set_dhcp_reservation": {"mac": TV, "ip": "10.9.9.131"},
    "clear_dhcp_reservation": {"mac": PHONE},
    "block_client": {"mac": PHONE},
    "unblock_client": {"mac": PHONE},
    "set_port_forward": {"name": "nas", "proto": "tcp", "wan_port": 8080, "lan_ip": "10.9.9.130", "lan_port": 80},
    "delete_port_forward": {"id": "pf-1"},
    "restart_device": {"mac": AP},
    "power_cycle_port": {"switch_mac": SW, "port": 3},
    "set_dns_forward": {"domain": "home.test", "target": "10.9.9.200"},
    "clear_dns_forward": {"domain": "lab.test"},
}
READERS_FOR_WRITES = ("get_dhcp_reservations", "get_clients", "get_port_forwards", "get_devices", "get_dns_records")
USER_PATH = f"{LEGACY}/rest/user"


class WriteCase(Case):
    def setUp(self):
        super().setUp()
        self.m = self.h.load(write=True)
        self.audit = []

    def run_tool(self, tool, /, **args):
        out, stdout = self.h.call(getattr(self.m, tool), **args)
        for line in stdout.splitlines():
            self.audit.append(json.loads(line))   # stdout is audit lines and nothing else
        return out

    def read(self, *names):
        for name in names or READERS_FOR_WRITES:
            self.run_tool(name)

    def token(self, tool, /, reason="the customer asked", **args):
        out = self.run_tool(tool, reason=reason, **(args or ARGS[tool]))
        self.assertTrue(out.startswith("{"), out)
        return json.loads(out)

    def confirm(self, tool, tok, /, reason="the customer asked", **args):
        out = self.run_tool(tool, reason=reason, confirm=tok, **(args or ARGS[tool]))
        self.assertTrue(out.startswith("{"), out)
        return json.loads(out)

    def write(self, tool, /, **args):
        """token, then confirm: the result body."""
        return self.confirm(tool, self.token(tool, **args)["token"], **args)

    def phases(self, target):
        return [a["phase"] for a in self.audit if a["target"] == target]

    def lose_two_probes_on(self, method):
        def hook(m, path, body):
            if m == method:
                self.h.probes[0].stop()
                self.h.probes[1].stop()
        self.h.fake.on_write = hook

    def last_comment(self, n=1):
        return self.h.fac.posted(n)[-1]


class Writes(WriteCase):
    def test_blank_reason_refused_before_any_request(self):
        for name, args in ARGS.items():
            with self.subTest(tool=name):
                out = self.run_tool(name, reason="  \t", **args)
                self.assertIn("reason is required", out)
        self.assertEqual(self.h.fake.requests, [])
        self.assertEqual(self.h.fac.issues, {})
        self.assertEqual([a["phase"] for a in self.audit], ["refused"] * len(ARGS))
        with self.subTest(control="with a reason, requests are made"):
            self.read("get_clients")
            self.token("block_client")
            self.assertEqual(len(self.h.fac.issues), 1)

    def test_unread_target_refused(self):
        out = self.run_tool("block_client", reason="noisy", mac=PHONE)
        self.assertIn("read it first", out)
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="after get_clients, a token is issued"):
            self.read("get_clients")
            self.assertIn("token", self.token("block_client"))

    def test_expired_cache_is_unread(self):
        self.read("get_clients")
        later = self.m._now() + self.m.CACHE_TTL + 1
        self.m._now = lambda: later
        self.assertIn("read it first", self.run_tool("block_client", reason="x", mac=PHONE))

    def test_body_changes_only_the_defined_fields(self):
        self.read("get_dhcp_reservations")
        tok = self.token("set_dhcp_reservation")
        res = self.confirm("set_dhcp_reservation", tok["token"])
        self.assertEqual(res["result"], "applied", res)
        puts = self.h.fake.puts()
        self.assertEqual([p for p, _ in puts], [f"{USER_PATH}/u-tv"])
        sent = puts[0][1]
        cached = next(u for u in FIXTURES["users"] if u["_id"] == "u-tv")
        self.assertEqual(sent, {**cached, "use_fixedip": True, "fixed_ip": "10.9.9.131"})
        after = tok["will_change"]["after"]
        self.assertEqual(set(after), set(sent))
        for k, v in sent.items():
            if k == "x_note":
                self.assertEqual(after[k], "<stripped>")
                self.assertEqual(v, USER_SECRET, "the write carries the secret back, not <stripped>")
            else:
                self.assertEqual(after[k], v, k)
        with self.subTest(control="the reader's cookie never rides a write, the operator's never a read"):
            writes = [who for meth, p, who in self.h.fake.requests if meth != "GET" and p != "/api/auth/login"]
            self.assertTrue(writes)
            self.assertEqual(set(writes), {"operator"})
            self.assertEqual({who for meth, _, who in self.h.fake.requests if meth == "GET"}, {"reader"})

    def test_token_single_use_bound_and_timed(self):
        self.m.WRITE_COOLDOWN = 0     # isolate the token from the cooldown gate
        self.read()
        tok = self.token("set_dhcp_reservation")["token"]
        self.assertEqual(self.confirm("set_dhcp_reservation", tok)["result"], "applied")
        replay = self.run_tool("set_dhcp_reservation", reason="x", confirm=tok, **ARGS["set_dhcp_reservation"])
        self.assertIn("unknown or already-used token", replay)
        self.assertEqual(len(self.h.fake.puts()), 1, "exactly one write for the first use")
        other = self.token("clear_dhcp_reservation")["token"]
        out = self.run_tool("block_client", reason="x", confirm=other, **ARGS["block_client"])
        self.assertIn("token was issued for clear_dhcp_reservation users/u-phone", out)
        late = self.token("block_client")["token"]
        later = self.m._now() + self.m.TOKEN_TTL + 1
        self.m._now = lambda: later
        self.read("get_clients")
        out = self.run_tool("block_client", reason="x", confirm=late, **ARGS["block_client"])
        self.assertIn("token expired", out)
        self.assertEqual(len(self.h.fake.mutations()), 1)
        with self.subTest(control="an expired token's ticket is closed as not-executed"):
            expired = [n for n, i in self.h.fac.issues.items() if "block_client" in i["title"]][0]
            self.assertIn("result: not-executed", self.h.fac.posted(expired)[-1])

    def test_changed_arguments_do_not_match_the_token(self):
        self.read("get_dhcp_reservations")
        tok = self.token("set_dhcp_reservation")["token"]
        out = self.run_tool("set_dhcp_reservation", reason="x", confirm=tok, mac=TV, ip="10.9.9.199")
        self.assertIn("will_change is no longer", out)
        self.assertEqual(self.h.fake.puts(), [])

    def test_cooldown_and_hourly_ceiling(self):
        self.read()
        self.write("set_dhcp_reservation")
        out = self.run_tool("set_dhcp_reservation", reason="again", **ARGS["set_dhcp_reservation"])
        self.assertRegex(out, r"cooldown is 600 s, \d+ s left")
        with self.subTest(control="another object inside the window, under the ceiling, is accepted"):
            self.assertIn("token", self.token("clear_dhcp_reservation"))
        self.m.WRITES_PER_HOUR = 1
        out = self.run_tool("block_client", reason="x", **ARGS["block_client"])
        self.assertIn("hourly ceiling reached: 1 of 1", out)
        self.assertIn("0 left", out)

    def test_create_needs_the_collection_read(self):
        out = self.run_tool("set_port_forward", reason="x", **ARGS["set_port_forward"])
        self.assertIn("get_port_forwards", out)
        with self.subTest(control="after the read the same create gets a token"):
            self.read("get_port_forwards")
            self.assertEqual(self.token("set_port_forward")["target"], "portforwards/new:nas")

    def test_audit_lines_are_the_only_stdout_and_carry_the_fields(self):
        self.read()
        self.write("set_dhcp_reservation")
        fields = {"ts", "tool", "target", "phase", "stated_reason", "user", "snapshot_digest", "diff_digest",
                  "health", "outbound", "fac_witness"}
        for line in self.audit:
            self.assertLessEqual(fields, set(line), line)
            self.assertEqual(line["user"], "im-test")
        self.assertTrue(any("rc" in a for a in self.audit if a["phase"] == "execute"))
        self.assertTrue(all("reason" in a for a in self.audit if a["phase"] == "refused"))


class DeadMan(WriteCase):
    def test_unhealthy_baseline_refuses(self):
        self.read()
        self.assertIn("token", self.token("set_dhcp_reservation"))
        self.assertEqual(len(self.h.fac.issues), 1, "control: all three up => token and ticket")
        self.h.probes[2].stop()
        out = self.run_tool("clear_dhcp_reservation", reason="x", **ARGS["clear_dhcp_reservation"])
        self.assertIn("baseline unhealthy", out)
        self.assertEqual(len(self.h.fac.issues), 1, "no ticket opened")
        self.assertEqual(self.h.fake.mutations(), [])

    def test_fac_silent_im_reverts_from_inside(self):
        self.h.fac.pre_ack = self.h.fac.post_ack = True
        self.h.fac.after = lambda: bool(self.h.fake.puts())
        self.lose_two_probes_on("PUT")
        self.read()
        tok = self.token("set_dhcp_reservation")
        self.assertEqual(tok["fac_witness"], "armed")
        res = self.confirm("set_dhcp_reservation", tok["token"])
        self.assertEqual(res["result"], "reverted", res)
        self.assertEqual(res["outbound"], "lost")
        puts = self.h.fake.puts()
        self.assertEqual(len(puts), 2)
        self.assertEqual(puts[1][1], next(u for u in FIXTURES["users"] if u["_id"] == "u-tv"), "the snapshot went back")
        self.assertIn("result: reverted", self.last_comment())
        self.assertIn("outbound: lost", self.last_comment())
        self.assertEqual(self.h.fac.issues[1]["state"], "closed")
        self.assertEqual(self.phases("users/u-tv"), ["token", "execute", "reverted"])

    def test_probes_kept_up_applies_without_revert(self):
        self.h.fac.pre_ack = self.h.fac.post_ack = True
        self.h.fac.after = lambda: bool(self.h.fake.puts())
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual((res["result"], res["outbound"], res["fac_witness"]), ("applied", "ok", "ok"))
        self.assertEqual(len(self.h.fake.puts()), 1, "no snapshot PUT")
        self.assertEqual(self.phases("users/u-tv"), ["token", "execute"])

    def test_one_probe_lost_is_degraded_not_reverted(self):
        def hook(m, path, body):
            self.h.probes[0].stop()
        self.h.fake.on_write = hook
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual((res["result"], res["outbound"]), ("applied", "degraded"))
        self.assertIn("degraded", res["note"])

    def test_witness_unmeasured_does_not_block(self):
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual((res["result"], res["fac_witness"]), ("applied", "unmeasured"))
        self.assertIn("fac_witness: unmeasured", self.last_comment())
        self.assertIn("result: applied", self.last_comment())

    def test_witness_armed_and_lost_reverts(self):
        self.h.fac.pre_ack = True
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual((res["result"], res["fac_witness"], res["outbound"]), ("reverted", "lost", "ok"))
        self.assertIn("fac_witness: lost", self.last_comment())
        self.assertIn("post-ack", " ".join(res["failed"]))

    def test_a_post_ack_left_before_the_change_is_applied_is_not_a_witness(self):
        # k8scc#126 (fleet-ops-1e): FAC could not see from the ticket that the
        # change had been applied, and a post-ack left before it counted
        self.h.fac.pre_ack, self.h.fac.post_ack = True, "early"
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual((res["result"], res["fac_witness"], res["outbound"]), ("reverted", "lost", "ok"), res)
        self.assertIn("post-ack", " ".join(res["failed"]))
        thread = self.h.fac.thread(1)
        self.assertEqual(thread[:3], [("fac", "pre-ack: fleet-ops-65 [69d6d7] reached the cluster from outside"),
                                      ("fac", "post-ack: fleet-ops-65 [69d6d7] still reach it"),
                                      ("server", "netdev-mcp applying")], "the harness: the post-ack really is before it")
        with self.subTest(control="the same post-ack left after the applying comment is one"):
            self.h.fac.post_ack = True
            self.h.fac.after = lambda: len(self.h.fake.puts()) >= 3      # this second write's own PUT
            self.m._LAST_WRITE.clear()
            self.read()
            res = self.write("set_dhcp_reservation")
            self.assertEqual((res["result"], res["fac_witness"]), ("applied", "ok"), res)
            kinds = self.h.fac.thread(2)
            self.assertLess(kinds.index(("server", "netdev-mcp applying")),
                            kinds.index(("fac", "post-ack: fleet-ops-65 [69d6d7] still reach it")))

    def test_a_post_ack_counts_for_nothing_when_the_applying_comment_is_not_listed(self):
        # the listing is the only order there is: an anchor that is not in it
        # (a page that does not reach it, an id GitHub never lists) places nothing
        self.h.fac.pre_ack = self.h.fac.post_ack = True
        self.h.fac.after = lambda: bool(self.h.fake.puts())
        self.read()
        self.assertEqual(self.write("set_dhcp_reservation")["result"], "applied")
        own = [c["id"] for c in self.h.fac.issues[1]["comments"] if c["body"].startswith("netdev-mcp applying")]
        self.assertEqual(len(own), 1)
        self.assertTrue(self.m.TICKETS.witnessed(1, "post-ack:", after=own[0]), "control: behind the real anchor it counts")
        self.assertTrue(self.m.TICKETS.witnessed(1, "post-ack:"), "control: with no anchor asked for it counts")
        self.assertFalse(self.m.TICKETS.witnessed(1, "post-ack:", after=987654321))

    def test_the_ticket_says_applying_before_the_write_is_sent(self):
        seen = []
        self.h.fake.on_write = lambda m, p, b: seen.append(list(self.h.fac.posted(1)))
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual(res["result"], "applied", res)
        self.assertEqual(len(seen), 1)
        self.assertEqual([c.split("\n")[0] for c in seen[0]], ["netdev-mcp applying"], "on the ticket when the PUT arrived")
        self.assertEqual([c.split("\n")[0] for c in self.h.fac.posted(1)], ["netdev-mcp applying", "netdev-mcp release"])
        self.assertIn("post-ack:", seen[0][0])
        self.assertIn("netdev-mcp applying", self.h.fac.issues[1]["body"], "the ticket tells FAC what to wait for")

    def test_no_applying_comment_no_write(self):
        self.h.fac.fail_comment = "netdev-mcp applying"
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual(res["result"], "aborted", res)
        self.assertIn("nothing was written", res["reason"])
        self.assertEqual(self.h.fake.mutations(), [])
        self.assertEqual(self.phases("users/u-tv"), ["token", "aborted"])
        self.assertIn("result: aborted", self.last_comment())
        with self.subTest(control="with the comment accepted the same write goes out"):
            self.h.fac.fail_comment = None
            self.m._LAST_WRITE.clear()
            self.read()
            self.assertEqual(self.write("set_dhcp_reservation")["result"], "applied")
            self.assertEqual(len(self.h.fake.mutations()), 1)

    def test_an_applying_comment_without_an_id_is_no_write(self):
        # with no id there is nothing to place a post-ack behind, and falling
        # back to "any post-ack" is the hole of k8scc#126 again (fleet-ops-1e
        # on #129: removing the check left every cell green)
        self.h.fac.pre_ack, self.h.fac.post_ack = True, "early"
        self.h.fac.no_id_for = "netdev-mcp applying"
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual(res["result"], "aborted", res)
        self.assertIn("no comment id", res["reason"])
        self.assertEqual(self.h.fake.mutations(), [])
        with self.subTest(control="with an id in the answer the same write is sent"):
            self.h.fac.no_id_for = None
            self.m._LAST_WRITE.clear()
            self.read()
            self.assertNotEqual(self.write("set_dhcp_reservation")["result"], "aborted")
            self.assertGreaterEqual(len(self.h.fake.mutations()), 1)

    def test_server_comments_never_count_as_witness(self):
        self.read()
        self.write("set_dhcp_reservation")                 # applied: a release comment
        self.h.fake.on_write = lambda m, p, b: setattr(self.h.fake, "dead", True)
        self.write("clear_dhcp_reservation")               # revert-failed: a revert-failed comment
        for n in self.h.fac.issues:
            for body in self.h.fac.posted(n):
                self.assertTrue(body.startswith("netdev-mcp "), body)
                self.assertFalse(body.startswith(("pre-ack:", "post-ack:")))
            self.assertFalse(self.m.TICKETS.witnessed(n, "pre-ack:"))
            self.assertFalse(self.m.TICKETS.witnessed(n, "post-ack:"))
        with self.assertRaises(AssertionError):
            self.m.TICKETS.comment(1, "pre-ack: forged by the server")
        with self.subTest(control="a pre-ack: comment from the same identity arms it"):
            self.h.fac.pre_ack = True
            self.assertTrue(self.m.TICKETS.witnessed(1, "pre-ack:"))

    def test_created_object_reverted_by_delete(self):
        self.lose_two_probes_on("POST")
        self.read("get_port_forwards")
        res = self.write("set_port_forward")
        self.assertEqual(res["result"], "reverted", res)
        deletes = [p for m, p, _ in self.h.fake.writes if m == "DELETE"]
        self.assertEqual(deletes, [f"{LEGACY}/rest/portforward/new-1"])
        self.assertEqual([pf["_id"] for pf in self.h.fake.data["portforwards"]], ["pf-1"])
        self.assertIn("result: reverted", self.last_comment())

    def test_created_object_kept_when_healthy(self):
        self.read("get_port_forwards")
        res = self.write("set_port_forward")
        self.assertEqual(res["result"], "applied", res)
        self.assertEqual([m for m, _, _ in self.h.fake.mutations()], ["POST"], "no DELETE")
        self.assertIn("new-1", [pf["_id"] for pf in self.h.fake.data["portforwards"]])

    def test_a_second_new_object_blocks_the_delete(self):
        def hook(m, path, body):
            if m == "POST":
                self.h.fake.data["portforwards"].append({"_id": "other-9", "name": "someone else's"})
                self.h.probes[0].stop()
                self.h.probes[1].stop()
        self.h.fake.on_write = hook
        self.read("get_port_forwards")
        res = self.write("set_port_forward")
        self.assertEqual(res["result"], "revert-failed", res)
        self.assertEqual([m for m, _, _ in self.h.fake.writes if m == "DELETE"], [])
        self.assertIn("new-1", res["revert"])
        self.assertIn("other-9", res["revert"])
        self.assertIn("inbox", res)
        self.assertIn("result: revert-failed", self.last_comment())

    def test_snapshot_get_failure_aborts(self):
        self.read()
        tok = self.token("set_dhcp_reservation")["token"]
        self.h.fake.status[USER_PATH] = 500
        res = self.confirm("set_dhcp_reservation", tok)
        self.assertEqual(res["result"], "aborted")
        self.assertIn("500", res["reason"])
        self.assertEqual(self.h.fake.mutations(), [])
        self.assertIn("result: aborted", self.last_comment())
        self.assertEqual(self.h.fac.issues[1]["state"], "closed")
        self.assertEqual(self.phases("users/u-tv")[-1], "aborted")
        with self.subTest(control="no snapshot file either"):
            self.assertEqual(list(self.h.snapshots.iterdir()), [])

    def test_revert_failure_is_reported_not_swallowed(self):
        self.h.fake.on_write = lambda m, p, b: setattr(self.h.fake, "dead", True)
        self.read()
        res = self.confirm("set_dhcp_reservation", self.token("set_dhcp_reservation")["token"])
        self.assertEqual(res["result"], "revert-failed", res)
        self.assertEqual(self.phases("users/u-tv")[-1], "revert-failed")
        self.assertEqual(self.h.fac.issues[1]["state"], "open")
        self.assertTrue(self.last_comment().startswith("netdev-mcp revert-failed"))
        self.assertIn(res["snapshot_digest"], res["inbox"])
        self.assertIn("~/workspace/inbox/", res["inbox"])

    def test_unused_token_ticket_closes_at_its_own_expiry(self):
        self.m.TOKEN_TTL, self.m.EXPIRY_GRACE = 0.2, 0.05
        self.read("get_clients")
        self.token("block_client")
        self.assertEqual(self.h.fac.issues[1]["state"], "open")
        deadline = time.monotonic() + 3
        while self.h.fac.issues[1]["state"] == "open" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.h.fac.issues[1]["state"], "closed", "no further call was made, and it still closed")
        self.assertIn("result: not-executed", self.last_comment())
        self.assertEqual(self.h.fake.mutations(), [])
        with self.subTest(control="a used token's timer closes nothing a second time"):
            self.m.TOKEN_TTL = 0.2
            self.read("get_dhcp_reservations")
            self.write("set_dhcp_reservation")
            time.sleep(0.4)
            self.assertEqual(sum("not-executed" in c for c in self.h.fac.posted(2)), 0)

    def test_delete_revert_reads_back(self):
        def hook(m, path, body):
            if m == "DELETE":
                self.h.probes[0].stop()
                self.h.probes[1].stop()
                self.h.fake.swallow.add("POST")     # the re-create answers ok and stores nothing
        self.h.fake.on_write = hook
        self.read("get_port_forwards")
        res = self.write("delete_port_forward")
        self.assertEqual(res["result"], "revert-failed", res)
        self.assertIn("does not hold it", res["revert"])
        self.assertIn("inbox", res)
        self.assertEqual(self.h.fake.data["portforwards"], [])

    def test_delete_revert_that_takes_reads_reverted(self):
        self.lose_two_probes_on("DELETE")
        self.read("get_port_forwards")
        res = self.write("delete_port_forward")
        self.assertEqual(res["result"], "reverted", res)
        self.assertEqual([pf["name"] for pf in self.h.fake.data["portforwards"]], ["ha"])

    def test_revert_put_that_does_not_take_is_revert_failed(self):
        def hook(m, path, body):
            if m == "PUT":
                self.h.probes[0].stop()
                self.h.probes[1].stop()
                self.h.fake.swallow.add("PUT")      # the revert PUT is accepted and changes nothing
        self.h.fake.on_write = hook
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual(res["result"], "revert-failed", res)
        self.assertIn("still differ from the snapshot", res["revert"])

    def test_merged_put_leaves_residue_that_is_judged_not_ignored(self):
        # k8scc#67 finding 4: the legacy PUT merges, so PUTting the snapshot
        # back cannot remove fixed_ip, which u-tv did not have before
        self.lose_two_probes_on("PUT")
        self.read()
        res = self.write("set_dhcp_reservation")
        self.assertEqual(res["result"], "reverted", res)
        self.assertIn("fixed_ip left behind but switched off", res["revert"])
        tv = next(u for u in self.h.fake.data["users"] if u["_id"] == "u-tv")
        self.assertEqual((tv["use_fixedip"], tv["fixed_ip"]), (False, "10.9.9.131"), "the residue really is there")
        with self.subTest(control="without the inert rule the same residue is revert-failed, not reverted"):
            self.h.close()
            self.h = Harness()
            self.m = self.h.load(write=True)
            self.m.INERT_RESIDUE = {}
            self.lose_two_probes_on("PUT")
            self.read()
            res = self.write("set_dhcp_reservation")
            self.assertEqual(res["result"], "revert-failed", res)
            self.assertIn("fixed_ip remain on u-tv although the snapshot had no such key", res["revert"])

    def test_residue_with_no_inert_rule_is_revert_failed(self):
        del self.h.fake.data["portforwards"][0]["enabled"]     # a key set_port_forward sets, absent before
        self.lose_two_probes_on("PUT")
        self.read("get_port_forwards")
        args = {"name": "ha", "proto": "tcp", "wan_port": 8443, "lan_ip": "10.9.9.131", "lan_port": 8123}
        res = self.write("set_port_forward", **args)
        self.assertEqual(res["result"], "revert-failed", res)
        self.assertIn("enabled remain on pf-1", res["revert"])
        with self.subTest(control="a key the snapshot had is put back and the revert reads reverted"):
            self.h.close()
            self.h = Harness()
            self.m = self.h.load(write=True)
            self.lose_two_probes_on("PUT")
            self.read("get_port_forwards")
            self.assertEqual(self.write("set_port_forward", **args)["result"], "reverted")

    def test_create_outside_delete_reverts_is_never_deleted(self):
        # k8scc#67 finding 5: DELETE rest/user answered 404 on FAC1
        self.read()
        plan = self.m.Plan("x", "users", "", "users/new:x", "create", {"mac": "bb:bb:bb:00:09:99"})
        snap = self.m.Snapshot(None, sorted(u["_id"] for u in FIXTURES["users"]), "d", "p")
        ok, why = self.m._revert(plan, snap, "new-9")
        self.assertFalse(ok)
        self.assertIn("not reverted by DELETE on this console", why)
        self.assertEqual([m for m, _, _ in self.h.fake.writes if m == "DELETE"], [])
        with self.subTest(control="the table holds what a shipped tool creates, and no shipped tool creates outside it"):
            # dnsrecords since k8scc#109 (DELETE static-dns answered 200 on FAC1, finding 7)
            self.assertEqual(self.m.DELETE_REVERTS, {"portforwards", "dnsrecords"})
            src = (ROOT / "netdev_mcp_server.py").read_text()
            creates = re.findall(r'Plan\("[a-z_]+", "([a-z]+)", "", [^\n]*"create"', src)
            self.assertEqual(set(creates), {"portforwards", "dnsrecords"}, creates)

    def test_object_changed_since_the_token_aborts(self):
        self.read()
        tok = self.token("set_dhcp_reservation")["token"]
        tv = next(u for u in self.h.fake.data["users"] if u["_id"] == "u-tv")
        tv["use_fixedip"], tv["fixed_ip"] = True, "10.9.9.177"     # someone else, between the phases
        res = self.confirm("set_dhcp_reservation", tok)
        self.assertEqual(res["result"], "aborted", res)
        self.assertIn("changed on the controller since the token", res["reason"])
        self.assertEqual(self.h.fake.puts(), [])
        with self.subTest(control="a create whose name appeared meanwhile aborts too"):
            self.read("get_port_forwards")
            tok = self.token("set_port_forward")["token"]
            self.h.fake.data["portforwards"].append({"_id": "pf-x", "name": "nas"})
            res = self.confirm("set_port_forward", tok)
            self.assertEqual(res["result"], "aborted", res)
            self.assertIn("appeared since the token", res["reason"])
        with self.subTest(control="unchanged: the same write applies"):
            self.h.fake.data["portforwards"].pop()
            tv["use_fixedip"], tv["fixed_ip"] = False, None
            del tv["fixed_ip"]
            self.m._LAST_WRITE.clear()
            self.read()
            self.assertEqual(self.write("set_dhcp_reservation")["result"], "applied")

    def test_lines_match_phases_and_digests(self):
        self.lose_two_probes_on("PUT")
        self.read()
        self.write("set_dhcp_reservation", reason="pin the tv")
        lines = [a for a in self.audit if a["target"] == "users/u-tv"]
        self.assertEqual([a["phase"] for a in lines], ["token", "execute", "reverted"])
        self.assertEqual({a["stated_reason"] for a in lines}, {"pin the tv"})
        files = list(self.h.snapshots.iterdir())
        self.assertEqual(len(files), 1)
        digest = hashlib.sha256(files[0].read_bytes()).hexdigest()
        self.assertEqual([a["snapshot_digest"] for a in lines[1:]], [digest, digest])
        self.assertEqual(lines[0]["health"], [])
        for a in lines[1:]:
            self.assertLessEqual({"controller", "apiserver", "readback"}, set(a["health"]))
            self.assertNotIn("probes", a["health"])
        self.assertEqual(len({a["diff_digest"] for a in lines}), 1)

    def test_secret_stripped_at_every_write_exit_and_kept_by_the_snapshot(self):
        self.read()
        tok_out = self.run_tool("set_dhcp_reservation", reason="x", **ARGS["set_dhcp_reservation"])
        res_out = self.run_tool("set_dhcp_reservation", reason="x", confirm=json.loads(tok_out)["token"],
                                **ARGS["set_dhcp_reservation"])
        exits = [tok_out, res_out, json.dumps(self.audit), json.dumps(self.h.fac.issues)]
        for text in exits:
            self.assertNotIn(USER_SECRET, text)
        self.assertIn("<stripped>", tok_out)
        snap = list(self.h.snapshots.iterdir())[0].read_text()
        self.assertEqual(snap.count(USER_SECRET), 1, "positive control: the snapshot keeps it")
        self.assertEqual(oct(list(self.h.snapshots.iterdir())[0].stat().st_mode & 0o777), "0o600")

    def test_block_reverts_by_unblocking(self):
        self.lose_two_probes_on("POST")
        self.read("get_clients")
        res = self.write("block_client")
        self.assertEqual(res["result"], "reverted", res)
        cmds = [b["cmd"] for m, p, b in self.h.fake.mutations() if p.endswith("/cmd/stamgr")]
        self.assertEqual(cmds, ["block-sta", "unblock-sta"])

    def test_a_restart_cannot_be_reverted_and_says_so(self):
        self.lose_two_probes_on("POST")
        self.read("get_devices")
        res = self.write("restart_device")
        self.assertEqual(res["result"], "revert-failed")
        self.assertIn("cannot be undone", res["revert"])
        self.assertEqual([b["cmd"] for _, _, b in self.h.fake.mutations()], ["restart"])

    def test_credentials_never_surface_on_the_write_path(self):
        self.read()
        outputs = [self.h.readings(), self.write("set_dhcp_reservation").__repr__()]
        self.h.fake.on_write = lambda m, p, b: setattr(self.h.fake, "dead", True)
        outputs.append(repr(self.write("clear_dhcp_reservation")))
        joined = "\n".join(outputs + [json.dumps(self.audit), json.dumps(self.h.fac.issues)])
        for value in (READER_VALUE, OPERATOR_VALUE, TICKET_TOKEN, COOKIE, CSRF, READER_COOKIE, READER_CSRF):
            self.assertNotIn(value, joined)
        with self.subTest(control="each one really was in play"):
            self.assertIn(f"Bearer {TICKET_TOKEN}", self.h.fac.tokens)
            self.assertEqual(self.m.OPERATOR_STATE, "ok")


class WriteClassA(WriteCase):
    def test_own_client_refused(self):
        self.read("get_clients")
        out = self.run_tool("block_client", reason="x", mac=NODE)
        self.assertIn("class A", out)
        self.assertIn("10.9.9.62", out)
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="another client reaches the token phase"):
            self.assertIn("token", self.token("block_client"))

    def test_reserving_an_address_on_im_own_path_refused(self):
        self.read("get_dhcp_reservations")
        for ip in ("10.9.9.62", "10.9.9.1", "10.9.9.20"):
            with self.subTest(ip=ip):
                self.assertIn("class A", self.run_tool("set_dhcp_reservation", reason="x", mac=TV, ip=ip))
        self.assertEqual(self.h.fac.issues, {})

    def test_gateway_restart_refused(self):
        self.read("get_devices")
        out = self.run_tool("restart_device", reason="x", mac=GW)
        self.assertIn("is the gateway", out)      # not just "gateway": that is also the device's name
        self.assertNotIn('"token"', out)
        with self.subTest(case="legacy stat/device may report the gateway's WAN address: its type still refuses"):
            next(d for d in self.h.fake.data["devices"] if d["mac"] == GW)["ip"] = "203.0.113.5"
            out = self.run_tool("restart_device", reason="x", mac=GW)
            self.assertIn("is the gateway", out)
            self.assertNotIn('"token"', out)
        with self.subTest(control="an AP reaches the token phase"):
            self.assertIn("token", self.token("restart_device"))

    def test_gateway_port_power_cycle_refused(self):
        # k8scc#71 FYI: the gateway check was on restart_device only, and class A's
        # device verdict passes a gateway (it is not what the node is attached to)
        self.read("get_devices")
        out = self.run_tool("power_cycle_port", reason="x", switch_mac=GW, port=1)
        self.assertIn("is the gateway", out)
        self.assertNotIn('"token"', out)
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(case="a WAN address in the gateway's legacy ip: its type still refuses"):
            next(d for d in self.h.fake.data["devices"] if d["mac"] == GW)["ip"] = "203.0.113.5"
            out = self.run_tool("power_cycle_port", reason="x", switch_mac=GW, port=1)
            self.assertIn("is the gateway", out)
            self.assertNotIn('"token"', out)
        with self.subTest(control="a port on a device that is not the gateway reaches the token phase"):
            self.assertIn("token", self.token("power_cycle_port", switch_mac=AP, port=1))

    def test_attached_switch_refused_whole(self):
        self.read("get_devices")
        self.assertIn("attached", self.run_tool("restart_device", reason="x", mac=SW))
        self.assertIn("attached", self.run_tool("power_cycle_port", reason="x", switch_mac=SW, port=17))
        with self.subTest(control="a port on a switch the node is not on reaches the token phase"):
            self.assertIn("token", self.token("power_cycle_port", switch_mac=AP, port=1))

    def test_switch_above_the_node_ap_refused(self):
        # k8scc#104 through the tool: the node on Wi-Fi, the switch is the AP's uplink
        node = next(c for c in self.h.fake.data["clients"] if c["mac"] == NODE)
        node.pop("sw_mac"); node.pop("sw_port"); node["is_wired"] = False; node["ap_mac"] = AP
        self.read("get_devices")
        for tool, args in (("restart_device", {"mac": SW}), ("power_cycle_port", {"switch_mac": SW, "port": 12})):
            with self.subTest(tool=tool):
                out = self.run_tool(tool, reason="x", **args)
                self.assertIn("on the path from this node to the gateway", out)
                self.assertNotIn('"token"', out)
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="wired again: the switch is refused as the attachment, the AP is a leaf"):
            node.pop("ap_mac"); node.update(is_wired=True, sw_mac=SW, sw_port=7)
            self.assertIn("token", self.token("restart_device", mac=AP))

    def test_missing_reader_data_fails_closed(self):
        self.read()
        self.h.fake.status[f"{LEGACY}/rest/networkconf"] = 500
        for name, args in ARGS.items():
            with self.subTest(tool=name):
                out = self.run_tool(name, reason="x", **args)
                self.assertIn("class A inputs unreadable", out)
                self.assertIn("500", out)
        self.h.fake.status.clear()
        os.environ["NETDEV_SHIM_ENDPOINTS_FAIL"] = "1"
        try:
            self.assertIn("kubectl get endpoints kubernetes", self.run_tool("block_client", reason="x", mac=PHONE))
        finally:
            del os.environ["NETDEV_SHIM_ENDPOINTS_FAIL"]
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="readable again: the same write reaches the token phase"):
            self.assertIn("token", self.token("block_client"))


DNS_PATH = f"{V2}/static-dns"
GATEWAY_RESOLVER = "10.9.9.1"     # net-lan's ip_subnet host part: the network carrying POD_IP 10.9.9.62


class DnsForwards(WriteCase):
    """k8scc#109: set_dns_forward / clear_dns_forward on v2 static-dns. The
    write shape is fleet-ops-b0's FAC1 reading (k8scc#67 finding 7); the
    timing rule is finding 8; "in effect" is the gateway answering SOA as the
    target does (fleet-ops-d5 on #112). Every DNS answer comes from FakeDNS:
    the targets each have their own SOA, the gateway answers lab.test as
    10.9.9.201 (the fixture's forward) and everything else as public DNS."""

    TARGET, OLD, NEW = "10.9.9.200", "10.9.9.201", "10.9.9.202"

    def setUp(self):
        super().setUp()
        self.setUp_dns()

    def dns_writes(self):
        return [(m, p, b) for m, p, b in self.h.fake.mutations() if p.startswith(DNS_PATH)]

    def takes_effect(self, domain, target):
        """After the controller accepts the write, the gateway answers `domain` as `target` (None: as public DNS)."""
        def hook(m, path, body):
            if target is None:
                self.h.dns.forward.pop((GATEWAY_RESOLVER, domain), None)
            else:
                self.h.dns.forward[(GATEWAY_RESOLVER, domain)] = target
        self.h.fake.on_write = hook

    def test_records_are_read_over_v2_and_fill_the_cache(self):
        out = self.run_tool("get_dns_records")
        self.assertEqual(out.split("\n", 1)[0], "[via v2]")
        self.assertEqual({r["key"] for r in body(out)}, {"nas.home.test", "lab.test"})
        self.assertIn(DNS_PATH, self.h.fake.paths())
        with self.subTest(control="unread, the write is refused naming the reader"):
            self.h.load(write=True)
            self.m = self.h.mod
            self.assertIn("get_dns_records", self.run_tool("set_dns_forward", reason="x", **ARGS["set_dns_forward"]))

    def test_forward_created_in_the_measured_shape_and_seen_in_effect(self):
        self.takes_effect("home.test", self.TARGET)
        self.read("get_dns_records")
        res = self.write("set_dns_forward")
        self.assertEqual(res["result"], "applied", res)
        self.assertIn("dns", res["health"])
        self.assertEqual(res["dns_effect"], "in effect")
        self.assertNotIn("dns_note", res)
        self.assertIn("dns_effect: in effect", self.last_comment())
        self.assertEqual(self.dns_writes(), [("POST", DNS_PATH, {"record_type": "NS", "key": "home.test",
                                                                 "value": self.TARGET, "port": 0, "enabled": True})])
        self.assertIn("home.test", self.h.dns.asked(self.TARGET), "the target was asked before the token")
        self.assertIn("home.test", self.h.dns.asked(GATEWAY_RESOLVER), "the LAN resolver was asked in health")
        self.assertEqual(self.phases("dnsrecords/home.test"), ["token", "execute"])
        with self.subTest(control="every write went to v2, none to a legacy rest path"):
            self.assertFalse([p for _, p, _ in self.h.fake.mutations() if p.startswith(LEGACY + "/rest/")])

    def test_a_forward_the_gateway_never_answers_through_is_reverted(self):
        # the controller took the record, the gateway still answers home.test from public DNS
        self.read("get_dns_records")
        res = self.write("set_dns_forward")
        self.assertEqual(res["result"], "reverted", res)
        self.assertIn("not in effect", " ".join(res["failed"]))
        self.assertEqual([m for m, _, _ in self.dns_writes()], ["POST", "DELETE"])

    def test_an_answer_that_cannot_show_the_change_is_unmeasured_not_a_pass(self):
        self.h.dns.soa[self.TARGET] = FakeDNS.PUBLIC     # the target itself answers as public DNS does
        self.read("get_dns_records")
        res = self.write("set_dns_forward")
        self.assertEqual(res["result"], "applied", res)
        self.assertTrue(res["dns_effect"].startswith("unmeasured"), res["dns_effect"])
        self.assertNotIn("dns", res["health"])
        self.assertIn("could not be measured", res["dns_note"])
        self.assertIn("dns_effect: unmeasured", self.last_comment())
        with self.subTest(control="a target with its own SOA, and the gateway turning to it, is measured"):
            self.h.dns.soa[self.TARGET] = ("k8s-gateway.home.test", 7)
            self.takes_effect("other.test", self.TARGET)
            self.read("get_dns_records")
            res = self.write("set_dns_forward", domain="other.test", target=self.TARGET)
            self.assertEqual((res["result"], res["dns_effect"]), ("applied", "in effect"))
            self.assertIn("dns", res["health"])

    def test_existing_forward_is_put_whole(self):
        self.takes_effect("lab.test", self.NEW)
        self.read("get_dns_records")
        res = self.write("set_dns_forward", domain="lab.test", target=self.NEW)
        self.assertEqual((res["result"], res["dns_effect"]), ("applied", "in effect"), res)
        before = next(r for r in FIXTURES["dnsrecords"] if r["_id"] == "dns-fwd-lab")
        self.assertEqual(self.dns_writes(), [("PUT", f"{DNS_PATH}/dns-fwd-lab", {**before, "value": self.NEW})])
        with self.subTest(control="the fake refuses a partial PUT, as FAC1 did (400)"):
            with self.assertRaises(self.m.ControllerError) as cm:
                self.m.OPERATOR.request_v2("PUT", "/static-dns/dns-fwd-lab", {"enabled": False})
            self.assertIn("400", str(cm.exception))

    def test_settle_comes_before_the_first_health_round(self):
        # the gateway turns to the target only once the settle has passed: a round before it fails.
        # A settle distinct from NETDEV_HEALTH_INTERVAL (0.05), or the hook cannot tell them apart.
        self.m = self.h.load(write=True, NETDEV_DNS_SETTLE="0.07")
        real_sleep, settle = self.m._sleep, self.m.DNS_SETTLE
        self.assertEqual(settle, 0.07)
        landing = {"domain": "home.test"}

        def sleep(sec):
            if sec == settle:
                self.h.dns.forward[(GATEWAY_RESOLVER, landing["domain"])] = self.TARGET
            real_sleep(sec)
        self.m._sleep = sleep
        self.read("get_dns_records")
        self.assertEqual(self.write("set_dns_forward")["result"], "applied")
        with self.subTest(control="a forward that never lands in the window reads as not in effect"):
            self.m.DNS_SETTLE = 0.09   # not the value the hook lands the forward on
            landing["domain"] = "other.test"
            self.m._LAST_WRITE.clear()
            self.read("get_dns_records")
            self.assertEqual(self.write("set_dns_forward", domain="other.test", target=self.TARGET)["result"],
                             "reverted")

    def test_created_forward_deleted_when_the_lan_resolver_fails(self):
        self.h.fake.on_write = lambda m, p, b: self.h.dns.modes.__setitem__(GATEWAY_RESOLVER, "servfail")
        self.read("get_dns_records")
        res = self.write("set_dns_forward")
        self.assertEqual(res["result"], "reverted", res)
        self.assertIn("dns:", " ".join(res["failed"]))
        self.assertEqual([m for m, _, _ in self.dns_writes()], ["POST", "DELETE"])
        self.assertEqual(self.dns_writes()[1][1], f"{DNS_PATH}/new-1")
        self.assertEqual([r["_id"] for r in self.h.fake.data["dnsrecords"]], ["dns-a-nas", "dns-fwd-lab"])
        self.assertEqual(self.phases("dnsrecords/home.test"), ["token", "execute", "reverted"])

    def test_cleared_forward_recreated_when_the_lan_resolver_fails(self):
        self.h.fake.on_write = lambda m, p, b: self.h.dns.modes.__setitem__(GATEWAY_RESOLVER, "refused")
        self.read("get_dns_records")
        res = self.write("clear_dns_forward")
        self.assertEqual(res["result"], "reverted", res)
        before = {k: v for k, v in next(r for r in FIXTURES["dnsrecords"] if r["_id"] == "dns-fwd-lab").items()
                  if k != "_id"}
        self.assertEqual(self.dns_writes(), [("DELETE", f"{DNS_PATH}/dns-fwd-lab", None), ("POST", DNS_PATH, before)])

    def test_a_clear_is_in_effect_only_when_the_old_target_stops_answering_through_the_gateway(self):
        self.takes_effect("lab.test", None)
        self.read("get_dns_records")
        res = self.write("clear_dns_forward")
        self.assertEqual((res["result"], res["dns_effect"]), ("applied", "in effect"), res)
        self.assertNotIn("lab.test", [r["key"] for r in self.h.fake.data["dnsrecords"]])
        with self.subTest(control="the gateway still answering as the old target is not in effect: re-created"):
            self.h.close()
            self.h = Harness()
            self.setUp_dns()
            self.m = self.h.load(write=True)
            self.read("get_dns_records")
            res = self.write("clear_dns_forward")
            self.assertEqual(res["result"], "reverted", res)
            self.assertIn("still answers as the old target", " ".join(res["failed"]))

    def test_a_cached_serial_does_not_read_a_clear_as_in_effect(self):
        # fleet-ops-d5 on #112: the gateway still forwards to the old target but
        # serves an older cached serial; only MNAME tells whose answer it is
        def hook(m, path, body):
            self.h.dns.stale[(GATEWAY_RESOLVER, "lab.test")] = 1
        self.h.fake.on_write = hook
        self.read("get_dns_records")
        res = self.write("clear_dns_forward")
        self.assertEqual(res["result"], "reverted", res)
        self.assertIn("still answers as the old target", " ".join(res["failed"]))
        with self.subTest(control="the fake really answers with a different serial and the same MNAME"):
            now, _ = self.m._dns_soa(GATEWAY_RESOLVER, "lab.test")
            old, _ = self.m._dns_soa(self.OLD, "lab.test")
            self.assertEqual(now[1], old[1])
            self.assertNotEqual(now[2], old[2])

    def test_a_clear_answered_without_an_soa_is_unmeasured(self):
        # k8scc#113 (fleet-ops-d5 on #112): an answer with no SOA has no MNAME,
        # so nobody can tell whose answer it is; it must not read as in effect
        def hook(m, path, body):
            self.h.dns.forward.pop((GATEWAY_RESOLVER, "lab.test"), None)
            self.h.dns.modes[GATEWAY_RESOLVER] = "nosoa"
        self.h.fake.on_write = hook
        self.read("get_dns_records")
        res = self.write("clear_dns_forward")
        self.assertEqual(res["result"], "applied", res)
        self.assertTrue(res["dns_effect"].startswith("unmeasured"), res["dns_effect"])
        self.assertIn("no SOA", res["dns_effect"])
        self.assertNotIn("dns", res["health"])
        with self.subTest(control="the fake really answers NOERROR with an empty MNAME"):
            self.assertEqual(self.m._dns_soa(GATEWAY_RESOLVER, "lab.test"), (("NOERROR", "", None), ""))

    def setUp_dns(self):
        self.h.dns.soa.update({self.TARGET: ("k8s-gateway.home.test", 7), self.OLD: ("old-gw.lab.test", 3),
                               self.NEW: ("new-gw.lab.test", 5)})
        self.h.dns.forward[(GATEWAY_RESOLVER, "lab.test")] = self.OLD

    def test_a_clear_of_a_forward_not_visibly_in_effect_is_unmeasured(self):
        self.h.dns.forward.pop((GATEWAY_RESOLVER, "lab.test"))   # the gateway did not answer through it before
        self.read("get_dns_records")
        res = self.write("clear_dns_forward")
        self.assertEqual(res["result"], "applied", res)
        self.assertTrue(res["dns_effect"].startswith("unmeasured"), res["dns_effect"])
        self.assertNotIn("dns", res["health"])

    def test_a_target_that_does_not_answer_is_refused_before_any_ticket(self):
        self.read("get_dns_records")
        for mode in ("silent", "refused", "servfail"):
            with self.subTest(target=mode):
                self.h.dns.modes[self.TARGET] = mode
                out = self.run_tool("set_dns_forward", reason="x", **ARGS["set_dns_forward"])
                self.assertIn("does not answer for home.test", out)
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="NXDOMAIN is an answer: the target serves DNS for that name"):
            self.h.dns.modes[self.TARGET] = "nxdomain"
            self.assertIn("token", self.token("set_dns_forward"))

    def test_the_soa_fingerprint_is_parsed_from_answer_and_authority(self):
        self.assertEqual(self.m._dns_soa(self.TARGET, "home.test"), (("NOERROR", "k8s-gateway.home.test", 7), ""))
        self.h.dns.modes[self.OLD] = "nxdomain"
        self.assertEqual(self.m._dns_soa(self.OLD, "x.lab.test"), (("NXDOMAIN", "old-gw.lab.test", 3), ""))
        with self.subTest(control="two servers with different SOAs read differently"):
            self.assertNotEqual(self.m._dns_soa(self.TARGET, "home.test")[0], self.m._dns_soa(GATEWAY_RESOLVER, "home.test")[0])

    def test_names_on_im_path_under_the_domain_are_refused(self):
        self.h.load(write=True, NETDEV_DNS_PROTECT="chamber.home.test")
        self.m = self.h.mod
        self.read("get_dns_records")
        out = self.run_tool("set_dns_forward", reason="x", **ARGS["set_dns_forward"])
        self.assertIn("class A", out)
        self.assertIn("chamber.home.test", out)
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="a domain that covers none of them reaches the token"):
            self.assertIn("token", self.token("set_dns_forward", domain="other.test", target=self.TARGET))
        with self.subTest(control="a sibling name is not under it"):
            self.assertEqual(self.m._dns_on_path("chamber.home.test.evil"), [])
            self.assertEqual(self.m._dns_on_path("ome.test"), [])
        with self.subTest(names="what the sidecar connects to counts by itself"):
            self.m.TICKET_API = "https://api.github.com"
            self.m.CONTROLLER_URL = "https://unifi.home.test"
            self.m.PROBES = [self.m.Probe("https://probe.example.org/x", "https", "probe.example.org", 443),
                             self.m.Probe("10.0.0.1:443", "tcp", "10.0.0.1", 443)]
            self.assertEqual(self.m._dns_on_path("github.com"), ["api.github.com"])
            self.assertEqual(self.m._dns_on_path("example.org"), ["probe.example.org"])
            self.assertEqual(self.m._dns_on_path("home.test"), ["unifi.home.test", "chamber.home.test"])
            self.assertNotIn("10.0.0.1", self.m._dns_path_names(), "an address is not a name")
        with self.subTest(default="cloudflared's tunnel names are protected without configuration"):
            self.h.load(write=True)
            self.assertEqual(self.h.mod._dns_on_path("argotunnel.com"), ["argotunnel.com"])

    def test_clear_on_im_path_refused_too(self):
        self.h.load(write=True, NETDEV_DNS_PROTECT="api.lab.test")
        self.m = self.h.mod
        self.read("get_dns_records")
        out = self.run_tool("clear_dns_forward", reason="x", **ARGS["clear_dns_forward"])
        self.assertIn("api.lab.test", out)
        self.assertEqual(self.dns_writes(), [])

    def test_bad_domains_and_targets_refused_before_any_ticket(self):
        self.read("get_dns_records")
        bad = {"com": "one label", "-a.test": "not a domain", "a..test": "not a domain", "a_b.test": "not a domain",
               "": "not a domain"}
        for domain, said in bad.items():
            with self.subTest(domain=domain):
                self.assertIn(said, self.run_tool("set_dns_forward", reason="x", domain=domain, target=self.TARGET))
        for target in ("gw.home.test", "127.0.0.1", "0.0.0.0", "224.0.0.5"):
            with self.subTest(target=target):
                self.assertIn("refused", self.run_tool("set_dns_forward", reason="x", domain="home.test", target=target))
        self.assertEqual(self.h.fac.issues, {})
        with self.subTest(control="a trailing dot and capitals normalise to the same domain"):
            tok = self.token("set_dns_forward", domain="Home.Test.", target=self.TARGET)
            self.assertEqual(tok["target"], "dnsrecords/home.test")

    def test_a_record_with_the_same_key_and_an_unchanged_forward_refused(self):
        self.read("get_dns_records")
        self.assertIn("already has a A record",
                      self.run_tool("set_dns_forward", reason="x", domain="nas.home.test", target=self.TARGET))
        self.assertIn("already forwards to 10.9.9.201",
                      self.run_tool("set_dns_forward", reason="x", domain="lab.test", target=self.OLD))
        self.assertIn("no forward to clear", self.run_tool("clear_dns_forward", reason="x", domain="nas.home.test"))
        self.assertEqual(self.h.fac.issues, {})

    def test_no_lan_resolver_fails_closed(self):
        self.h.fake.data["networks"] = [n for n in self.h.fake.data["networks"] if n["_id"] != "net-lan"]
        self.read("get_dns_records")
        out = self.run_tool("set_dns_forward", reason="x", **ARGS["set_dns_forward"])
        self.assertIn("no LAN resolver", out)
        with self.subTest(control="with the network, the resolver is its gateway"):
            self.h.fake.data["networks"] = json.loads(json.dumps(FIXTURES["networks"]))
            self.assertIn("token", self.token("set_dns_forward"))

    def test_one_cooldown_for_set_and_clear_of_one_domain(self):
        self.takes_effect("lab.test", None)
        self.read("get_dns_records")
        self.assertEqual(self.write("clear_dns_forward")["result"], "applied")
        self.read("get_dns_records")
        self.assertIn("cooldown", self.run_tool("set_dns_forward", reason="x", domain="lab.test", target=self.NEW))

if __name__ == "__main__":
    unittest.main(verbosity=2)
