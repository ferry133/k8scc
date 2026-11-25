#!/usr/bin/env python3
"""MCP Netdev Server — the customer's UniFi controller, for im.

Runs as its own sidecar container (`netdev-mcp`, jg-base#222), isolated from
the `app` container the terminal user/agent shell runs in, the same split as
talos-mcp: the UniFi credentials are mounted only here. The root change is
fleet-ops `openspec/changes/im-unifi-management`; this server's contract is
k8scc `openspec/changes/im-netdev-mcp` (spec `netdev-mcp-server`).

Start: python3 netdev_mcp_server.py  (HTTP/SSE transport, binds 127.0.0.1 only)

Three startup states decide which tools exist: reader input missing ⇒ every
tool answers "not configured"; reader usable but any write input missing ⇒ the
ten read tools only, the startup readings naming what is missing; everything
usable ⇒ the eight write tools too. There is no state in which a write tool is
registered but refuses every call for lack of configuration.

Requires: UNIFI_CONTROLLER_URL — https://<console>. `http://` is refused
          unless NETDEV_ALLOW_INSECURE_HTTP=1 (the controls' fake controller),
          and the startup readings say when it was accepted.
          UNIFI_CA_FILE — the console's own certificate or its CA, exported at
          onboarding (fleet-ops skill `unifi-im-credential`). TLS is always
          verified against it; there is no option to turn verification off,
          because `verify=False` is the one-line fix that always "works".
          UNIFI_READER_FILE — the console's View Only local account, two lines
          (user, password), read from the file at startup, never from env or
          argv, and logged in over `/api/auth/login` (cookie + CSRF) like the
          operator. Not an API key: on FAC1 (UniFi OS 6.0.17, Network 11.0.99)
          a View Only user cannot create one, and the owner's key PUT a user
          row (k8scc#67 finding 6; ferry133 ruled 2026-11-13).
          When any of the three is missing every tool answers "not configured",
          naming what is missing, and makes no request.

Optional: UNIFI_SITE — the legacy site name (default `default`), also the v2
          API's site.
          NETDEV_READ_TIMEOUT — seconds per HTTP request (15).
          NETDEV_MCP_PORT — listen port (8766).

Write inputs (all of them, or no write tool is registered):
          UNIFI_OPERATOR_FILE — the local operator account, two lines (user,
          password), logged in once at startup over the legacy API; a login
          the controller refuses is the same as no file.
          IM_FAC_TICKET_REPO + IM_FAC_TICKET_TOKEN_FILE — the im→FAC channel
          (a private GitHub repo and a token that can open, comment on and
          close its issues). The ticket leaves the LAN before anything is
          written: besides stdout it is the only company-side record.
          NETDEV_OUTSIDE_PROBES — exactly three, `https://host/path` (GET,
          2xx/3xx) or `host:port` (TCP connect), over IPv4 only (k8scc#115).
          POD_IP — the node's address (hostNetwork); class A starts from it.
          NETDEV_SNAPSHOT_DIR — a writable directory on this sidecar's own
          volume. Snapshots keep secrets (they are the revert's raw material),
          so it is never a path `app` can see.
Write tuning: NETDEV_SNAPSHOT_KEEP (50), NETDEV_HEALTH_TIMEOUT (120),
          NETDEV_HEALTH_INTERVAL (10), NETDEV_WRITES_PER_HOUR (12),
          NETDEV_WITNESS_WAIT (120), NETDEV_WITNESS_POLL (5),
          NETDEV_PROBE_TIMEOUT (5), CLUSTER_NAME (ticket title), CLAUDE_USER_ID
          (audit `user`), IM_FAC_TICKET_API (https://api.github.com; the
          controls point it at a fake).

Which API serves which collection is data, not code: ROUTES and WRITES below.
Every read response says which API answered (`[via legacy]` / `[via v2]`), so
a route that moves shows up in the output, not only in this file. Root spike
1.1 read FAC1 (k8scc#67); which routes that settles is noted at ROUTES.

stdout carries the audit lines and nothing else: __main__ points sys.stdout at
stderr before the server starts (uvicorn's access log is a StreamHandler on
ext://sys.stdout, resolved when uvicorn configures logging) and keeps the real
stdout for _audit alone.
"""
import datetime as _dt
import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.server.fastmcp import FastMCP

CONTROLLER_URL = os.environ.get("UNIFI_CONTROLLER_URL", "").strip().rstrip("/")
SITE = os.environ.get("UNIFI_SITE", "").strip() or "default"
CA_FILE = os.environ.get("UNIFI_CA_FILE", "").strip()
READER_FILE = os.environ.get("UNIFI_READER_FILE", "").strip()
# The earlier image's reader input. Never read; named in the "not configured"
# answer so a half-landed rollout says which half is missing (k8scc#67).
OLD_READER_KEY_FILE = os.environ.get("UNIFI_READER_KEY_FILE", "").strip()
ALLOW_INSECURE_HTTP = os.environ.get("NETDEV_ALLOW_INSECURE_HTTP", "") == "1"
READ_TIMEOUT = int(os.environ.get("NETDEV_READ_TIMEOUT", "15"))
MCP_PORT = int(os.environ.get("NETDEV_MCP_PORT", "8766"))

OPERATOR_FILE = os.environ.get("UNIFI_OPERATOR_FILE", "").strip()
TICKET_REPO = os.environ.get("IM_FAC_TICKET_REPO", "").strip()
TICKET_TOKEN_FILE = os.environ.get("IM_FAC_TICKET_TOKEN_FILE", "").strip()
TICKET_API = (os.environ.get("IM_FAC_TICKET_API", "").strip() or "https://api.github.com").rstrip("/")
PROBES_RAW = os.environ.get("NETDEV_OUTSIDE_PROBES", "")
POD_IP = os.environ.get("POD_IP", "").strip()
SNAPSHOT_DIR = os.environ.get("NETDEV_SNAPSHOT_DIR", "").strip()
SNAPSHOT_KEEP = int(os.environ.get("NETDEV_SNAPSHOT_KEEP", "50"))
HEALTH_TIMEOUT = float(os.environ.get("NETDEV_HEALTH_TIMEOUT", "120"))
HEALTH_INTERVAL = float(os.environ.get("NETDEV_HEALTH_INTERVAL", "10"))
WRITES_PER_HOUR = int(os.environ.get("NETDEV_WRITES_PER_HOUR", "12"))
WITNESS_WAIT = float(os.environ.get("NETDEV_WITNESS_WAIT", "120"))
WITNESS_POLL = float(os.environ.get("NETDEV_WITNESS_POLL", "5"))
PROBE_TIMEOUT = float(os.environ.get("NETDEV_PROBE_TIMEOUT", "5"))
# DNS forwards (k8scc#109): seconds after the apply before the health rounds
# start (FAC1, k8scc#67 finding 8: the forward took effect at ~30 s, and the
# gateway's resolver went dark for under ~20 s after a DELETE; 5 s sampling),
# and the names on im's path that this sidecar cannot see for itself.
DNS_SETTLE = float(os.environ.get("NETDEV_DNS_SETTLE", "45"))
DNS_PROTECT_RAW = os.environ.get("NETDEV_DNS_PROTECT", "argotunnel.com")
CLUSTER = os.environ.get("CLUSTER_NAME", "").strip()
USER_ID = os.environ.get("CLAUDE_USER_ID", "")
KUBECTL = shutil.which("kubectl") or "/usr/local/bin/kubectl"

CACHE_TTL = 600   # seconds a reader result can address a write (spec: session cache)
TOKEN_TTL = 120
EXPIRY_GRACE = 1   # seconds past TOKEN_TTL before an unused token's ticket is closed
WRITE_COOLDOWN = 600

# httpx logs every request at INFO; the startup readings and the tool
# responses already say what was asked, and a request line per call drowns
# them in `kubectl logs -c netdev-mcp`. Warnings and errors still show.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Patch points for the controls in test/ (expiry without sleeping).
_now = time.time
_clock = time.monotonic
_sleep = time.sleep

# collection -> (api, path). `legacy` paths are under /proxy/network/api/s/{site}
# (answer {meta: {rc}, data: [...]}), `legacy-root` under /proxy/network/api,
# `v2` under /proxy/network/v2/api/site/{site} (answer a bare list).
#
# No Integration API route: it refuses the View Only cookie (fleet-ops-b0 on
# FAC1, 2026-11-14: integration/v1/sites 401 with the cookie + CSRF that
# legacy rest/networkconf answered 200), and the reader no longer has a key.
# Answered 200 on FAC1 (k8scc#67 findings 1-3, read with the since-revoked key
# and with cookies): rest/networkconf, rest/portforward, rest/firewallrule,
# rest/firewallgroup, rest/user, rest/wlanconf, stat/sysinfo, v2
# firewall-policies. NOT read there: stat/device, stat/sta, self/sites and v2
# firewall/zone (all four paths are UniFi's documented ones), and no v2 path
# with the View Only cookie. Root §6 on jg-jcc2 reads those. Change the data
# here, not the tools.
#
# On Network 11 the firewall is zone-based: rest/firewallrule and
# rest/firewallgroup answer 200 with 0 rows while 91 policies live in v2
# firewall-policies. Both are read; an empty legacy list is not "no rules".
ROUTES = {
    "info":             ("legacy", "/stat/sysinfo"),
    "sites":            ("legacy-root", "/self/sites"),
    "devices":          ("legacy", "/stat/device"),
    "clients":          ("legacy", "/stat/sta"),
    "networks":         ("legacy", "/rest/networkconf"),
    "firewallrules":    ("legacy", "/rest/firewallrule"),
    "firewallgroups":   ("legacy", "/rest/firewallgroup"),
    "firewallpolicies": ("v2", "/firewall-policies"),
    "firewallzones":    ("v2", "/firewall/zone"),
    "portforwards":     ("legacy", "/rest/portforward"),
    "users":            ("legacy", "/rest/user"),
    "wlans":            ("legacy", "/rest/wlanconf"),
    # k8scc#109: v2 static-dns holds both A records and forwards (record_type
    # NS, key = domain, value = target). Read on FAC1 with the since-revoked
    # key (k8scc#67 finding 3, 5 rows), written there with the operator
    # (finding 7); not yet read with the View Only cookie (root §6).
    "dnsrecords":       ("v2", "/static-dns"),
}
API_PREFIX = {"legacy": "/proxy/network/api/s/{site}", "legacy-root": "/proxy/network/api",
              "v2": "/proxy/network/v2/api/site/{site}"}

# The operator's side, all legacy (cookie + CSRF from a local account). Read
# on FAC1 (k8scc#67 findings 4-6): login, PUT rest/user (it merges), DELETE
# rest/user (404), cmd/stamgr forget-sta (200). The rest are UniFi's
# documented paths, not yet read on a console. One collection is written over
# v2 instead, on its ROUTES path: dnsrecords (static-dns; finding 7: POST 200
# with the whole object, PUT wants the whole object, partial PUT 400, DELETE 200).
LOGIN_PATH = "/api/auth/login"
WRITES = {
    "rest":   "/rest/{collection}",          # PUT …/{_id}, POST (create), DELETE …/{_id}
    "stamgr": "/cmd/stamgr",                  # block-sta / unblock-sta
    "devmgr": "/cmd/devmgr",                  # restart / power-cycle
}
LEGACY_NAME = {"networks": "networkconf", "firewallrules": "firewallrule", "firewallgroups": "firewallgroup",
               "portforwards": "portforward", "users": "user", "wlans": "wlanconf"}
# A create is reverted by DELETE only where DELETE removes the object. On FAC1
# `DELETE rest/user/<_id>` answered 404 (k8scc#67 finding 5; `forget-sta`
# removed it instead), so a create in any other collection is never "reverted"
# by a DELETE that cannot work. rest/portforward's DELETE is not measured.
DELETE_REVERTS = {"portforwards", "dnsrecords"}   # dnsrecords: DELETE measured on FAC1 (finding 7)

# A PUT on the legacy API MERGES (k8scc#67 finding 4: a partial PUT kept
# fixed_ip/use_fixedip), so PUTting the snapshot back cannot remove a key the
# snapshot did not have. Such a key left behind is harmless only where another
# field switches it off; this table names those, and any other leftover key is
# reported as residue (revert-failed), never as reverted.
INERT_RESIDUE = {("users", "fixed_ip"): lambda obj: obj.get("use_fixedip") is not True}


# --- configuration -----------------------------------------------------------

def _read_secret_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _account_lines(path: str) -> list[str]:
    return [ln.strip() for ln in _read_secret_file(path).splitlines() if ln.strip()] if path else []


def _reader_problem() -> str:
    """'' when UNIFI_READER_FILE holds two lines (user, password), else why not.
    Absent, unreadable and 0 bytes are the same thing: a Secret key rendered
    from an unset value mounts as an empty file."""
    where = f"UNIFI_READER_FILE{'=' + READER_FILE if READER_FILE else ''}"
    lines = _account_lines(READER_FILE)
    if not lines:
        why = f"reader credential ({where})"
    elif len(lines) != 2:
        why = f"reader credential ({where}: expected two lines, user and password; found {len(lines)})"
    else:
        return ""
    if OLD_READER_KEY_FILE:
        why += (" — UNIFI_READER_KEY_FILE is set but no longer read: the reader is now the View Only account "
                "in UNIFI_READER_FILE (k8scc#67)")
    return why


def _missing_config() -> list[str]:
    missing = []
    if not CONTROLLER_URL:
        missing.append("controller URL (UNIFI_CONTROLLER_URL)")
    reader = _reader_problem()
    if reader:
        missing.append(reader)
    if CONTROLLER_URL.startswith("https://") and (not CA_FILE or not os.path.isfile(CA_FILE) or os.path.getsize(CA_FILE) == 0):
        missing.append(f"controller CA (UNIFI_CA_FILE{'=' + CA_FILE if CA_FILE else ''})")
    return missing


def _url_refusal() -> str:
    """Why the controller URL is refused, or '' when it is usable."""
    if not CONTROLLER_URL:
        return ""   # reported as missing, not as refused
    scheme = urlsplit(CONTROLLER_URL).scheme
    if scheme == "https":
        return ""
    if scheme == "http" and ALLOW_INSECURE_HTTP:
        return ""
    if scheme == "http":
        return ("controller URL refused: plain http:// is accepted only with NETDEV_ALLOW_INSECURE_HTTP=1, "
                "which exists for the controls' fake controller, not for a customer console")
    return f"controller URL refused: scheme {scheme or '(none)'!r} is neither https nor http"


MISSING_CONFIG = _missing_config()
URL_REFUSAL = _url_refusal()
# Read once at startup into this process only; never exported, never printed.
_READER_ACCOUNT = ("", "") if MISSING_CONFIG else tuple(_account_lines(READER_FILE))


def _not_ready() -> str:
    """The reason every tool must answer without a request, or ''."""
    if MISSING_CONFIG:
        return ("error: netdev-mcp is not configured on this cluster — missing: " + ", ".join(MISSING_CONFIG)
                + ". The sidecar exists only where jg-base mounted it; populate the UniFi values in the "
                "cluster's secrets (fleet-ops skill `unifi-im-credential`) and restart this pod.")
    if URL_REFUSAL:
        return "error: " + URL_REFUSAL
    return ""


# --- secrets never leave ------------------------------------------------------

# `_keys$` and `preshared`: wlanconf's `private_preshared_keys` (PPSK) matched
# none of the others (k8scc#67 finding 2; empty lists on FAC1, so nothing
# leaked there).
_SECRET_KEY = re.compile(r"^x_|_secret$|_keys?$|passphrase|password|psk|preshared", re.I)
STRIPPED = "<stripped>"


def _strip(obj):
    """Every value under a key that looks like a secret, replaced — recursively.
    UniFi's own convention is the `x_` prefix (`x_passphrase`, `x_password`…);
    the other patterns catch what does not follow it. Applied to every reader
    response; the snapshot (a later image) is the one exit that keeps them."""
    if isinstance(obj, dict):
        return {k: (STRIPPED if isinstance(k, str) and _SECRET_KEY.search(k) else _strip(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


# --- the controller -------------------------------------------------------------

class ControllerError(Exception):
    """`answered` is True when the controller itself said no (an HTTP status,
    rc != ok) and False when the request may or may not have reached it."""

    def __init__(self, message: str, answered: bool = False):
        super().__init__(message)
        self.answered = answered


class _PinnedContext(ssl.SSLContext):
    """Accepts exactly one certificate, compared byte for byte on every new
    connection: `wrap_socket` is where httpx's sync transport hands the
    connected socket to TLS, after the handshake has already verified the
    peer against the same single anchor."""
    pinned_der = b""

    def wrap_socket(self, *args, **kwargs):
        sock = super().wrap_socket(*args, **kwargs)
        if sock.getpeercert(binary_form=True) != self.pinned_der:
            sock.close()
            raise ssl.SSLCertVerificationError(
                "the console presented a certificate that is not the one in UNIFI_CA_FILE (pinned: exactly that "
                "certificate, nothing it signed); to trust a CA instead, give the CA and the chain (two or more "
                "certificates), and the hostname is then checked")
        return sock


_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def _tls_context() -> "tuple[ssl.SSLContext, str]":
    """The context every controller request is verified with, and its mode.

    A UniFi console's factory certificate is self-signed for `unifi.local`,
    valid for no address the sidecar dials. FAC1's (fleet-ops-b0, 2026-11-14):
    SANs unifi.local, localhost, [::1], 127.0.0.1, fe80::1; keyUsage including
    Certificate Sign; no basicConstraints, no SKI/AKI. Verified as a CA with
    the hostname checked, every request failed the handshake (`IP address
    mismatch`); Python 3.13's VERIFY_X509_STRICT rejects it besides ("invalid
    CA certificate": keyCertSign without basicConstraints). OpenSSL counts it
    as a CA, so no rule reading the CA bit can tell it from a root CA — the
    first version of this fix tried and put FAC1 in CA mode.

    So the mode is decided by count, and the pin by identity:
    - exactly one certificate in the file ⇒ pinned: it is the only anchor, the
      hostname is not checked, the strict flag is cleared, and the peer must
      be that very certificate (DER equal) — so a CA alone in the file never
      authorises what it signed with the hostname off.
    - two or more ⇒ CA mode: the hostname and the strict flag stay on."""
    try:
        with open(CA_FILE, encoding="ascii", errors="replace") as fh:
            blocks = _PEM_CERT.findall(fh.read())
        if len(blocks) == 1:
            ctx = _PinnedContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.check_hostname = False
            ctx.load_verify_locations(cadata=blocks[0])
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            ctx.pinned_der = ssl.PEM_cert_to_DER_cert(blocks[0])
            return ctx, "pinned certificate (exactly the one in UNIFI_CA_FILE; hostname not checked)"
        ctx = ssl.create_default_context(cafile=CA_FILE)
    except (OSError, ValueError) as exc:   # ssl.SSLError is an OSError
        raise ControllerError(f"controller CA {CA_FILE} (UNIFI_CA_FILE) cannot be loaded: {exc}") from None
    return ctx, f"CA + hostname ({len(blocks)} certificates)"


def _controller_client(headers: dict) -> httpx.Client:
    verify = _tls_context()[0] if CONTROLLER_URL.startswith("https://") else True
    return httpx.Client(base_url=CONTROLLER_URL, verify=verify, timeout=READ_TIMEOUT,
                        headers={"Accept": "application/json", **headers}, follow_redirects=False)


def _legacy_data(method: str, path: str, resp: httpx.Response) -> list:
    if resp.status_code // 100 != 2:
        raise ControllerError(f"{method} {path}: controller answered {resp.status_code} {resp.text[:200]!r}", answered=True)
    try:
        body = resp.json()
    except ValueError:
        raise ControllerError(f"{method} {path}: controller answered {resp.status_code} with a body that is not JSON",
                              answered=True) from None
    if not isinstance(body, dict) or (body.get("meta") or {}).get("rc") != "ok" or not isinstance(body.get("data"), list):
        msg = (body.get("meta") or {}).get("msg") if isinstance(body, dict) else None
        raise ControllerError(f"{method} {path}: legacy API did not answer rc=ok ({msg or 'no meta'})", answered=True)
    return body["data"]


def _v2_data(path: str, resp: httpx.Response) -> list:
    """v2 answers a bare JSON list (FAC1: firewall-policies, 91 rows). A dict
    holding a `data` list is accepted too; anything else is an error, never []."""
    if resp.status_code // 100 != 2:
        raise ControllerError(f"GET {path}: controller answered {resp.status_code} {resp.text[:200]!r}", answered=True)
    try:
        body = resp.json()
    except ValueError:
        raise ControllerError(f"GET {path}: controller answered {resp.status_code} with a body that is not JSON",
                              answered=True) from None
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    if not isinstance(body, list):
        raise ControllerError(f"GET {path}: v2 API answered {type(body).__name__}, not a list", answered=True)
    return body


class Session:
    """One local account on its own HTTP client: POST /api/auth/login once,
    then cookie + CSRF on every call, and a 401 logs in again once (the
    session expired). The reader and the operator are two of these, so the
    reader's cookie never rides a write and the operator's never a read.
    Neither password nor any login response body is ever put into a message."""

    def __init__(self, role: str, user: str, password: str):
        self._role, self._user, self._password = role, user, password
        self._client = _controller_client({})
        self._csrf = ""
        self._logged_in = False
        # tools run on worker threads (k8scc#132): one request at a time per
        # account, so a re-login and the CSRF value it brings are not raced
        self._lock = threading.RLock()

    def _take_csrf(self, resp: httpx.Response) -> None:
        self._csrf = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token") or self._csrf

    def login(self) -> None:
        with self._lock:
            try:
                resp = self._client.post(LOGIN_PATH, json={"username": self._user, "password": self._password})
            except httpx.HTTPError as exc:
                raise ControllerError(f"POST {LOGIN_PATH}: {type(exc).__name__}: {exc}") from None
            if resp.status_code // 100 != 2:
                raise ControllerError(f"POST {LOGIN_PATH}: controller refused the {self._role} login ({resp.status_code})",
                                      answered=True)
            self._take_csrf(resp)
            self._logged_in = True

    def send(self, method: str, path: str, body=None) -> httpx.Response:
        with self._lock:
            return self._send(method, path, body)

    def _send(self, method: str, path: str, body=None) -> httpx.Response:
        if not self._logged_in:
            self.login()
        for attempt in (0, 1):
            try:
                resp = self._client.request(method, path, json=body,
                                            headers={"X-CSRF-Token": self._csrf} if self._csrf else {})
            except httpx.HTTPError as exc:
                # the exception text names the URL and the failure, never a header
                raise ControllerError(f"{method} {path}: {type(exc).__name__}: {exc}") from None
            self._take_csrf(resp)
            if resp.status_code == 401 and attempt == 0:
                self.login()
                continue
            return resp
        raise AssertionError("unreachable")


class Controller:
    """The reader: the View Only account (UNIFI_READER_FILE) on its own
    Session, legacy and v2 paths only. Writes never go through it (Operator)."""

    def __init__(self):
        self._session = Session("reader", *_READER_ACCOUNT)

    def _path(self, collection: str) -> "tuple[str, str]":
        api, path = ROUTES[collection]
        return api, API_PREFIX[api].format(site=SITE) + path

    def read(self, collection: str) -> "tuple[str, list]":
        """(api, data) for one collection, unstripped."""
        api, path = self._path(collection)
        resp = self._session.send("GET", path)
        return api, (_v2_data(path, resp) if api == "v2" else _legacy_data("GET", path, resp))


class Operator:
    """The writer: the local operator account over the legacy API."""

    def __init__(self, user: str, password: str):
        self._session = Session("operator", user, password)

    def login(self) -> None:
        self._session.login()

    def request(self, method: str, path: str, body=None) -> list:
        path = API_PREFIX["legacy"].format(site=SITE) + path
        return _legacy_data(method, path, self._session.send(method, path, body))

    def request_v2(self, method: str, path: str, body=None) -> list:
        """v2 answers the object itself (POST, PUT), a list, or nothing (DELETE);
        returned as a list either way. A status outside 2xx is an error."""
        path = API_PREFIX["v2"].format(site=SITE) + path
        resp = self._session.send(method, path, body)
        if resp.status_code // 100 != 2:
            raise ControllerError(f"{method} {path}: controller answered {resp.status_code} {resp.text[:200]!r}",
                                  answered=True)
        if not resp.content:
            return []
        try:
            data = resp.json()
        except ValueError:
            raise ControllerError(f"{method} {path}: controller answered {resp.status_code} with a body that is not JSON",
                                  answered=True) from None
        return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


_CONTROLLER: "Controller | None" = None


def _controller() -> Controller:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = Controller()
    return _CONTROLLER


# --- session cache: what a write may address ------------------------------------------

_SEEN: dict[str, dict[str, tuple[dict, float]]] = {}   # collection -> id -> (stripped obj, ts)
_SEEN_AT: dict[str, float] = {}                        # collection -> when a reader last read all of it


def _remember(collection: str, items) -> None:
    now = _now()
    whole = isinstance(items, list)
    # A whole read replaces what was known: an object the controller no longer
    # lists must not stay addressable. A forward cleared and read again was
    # still planned as "exists", and the set that takes the clear back was
    # aborted at its snapshot (found with k8scc#132's undo). The new bucket is
    # filled first and put in place in one assignment, so a write planning on
    # another thread never finds the collection empty in between.
    bucket = {} if whole else _SEEN.setdefault(collection, {})
    for obj in items if whole else [items]:
        if isinstance(obj, dict):
            key = obj.get("_id") or obj.get("id")
            if key:
                bucket[str(key)] = (obj, now)
    if whole:
        _SEEN[collection] = bucket
        _SEEN_AT[collection] = now


def _seen(collection: str, key: str) -> "dict | None":
    """The object a reader returned for this id within CACHE_TTL, else None."""
    entry = _SEEN.get(collection, {}).get(key)
    if entry is None or _now() - entry[1] > CACHE_TTL:
        return None
    return entry[0]


def _seen_by(collection: str, field_names: tuple, value: str) -> "dict | None":
    """The cached object whose field (first of field_names present) equals value, case-insensitively."""
    want = value.strip().lower()
    for key in list(_SEEN.get(collection, {})):
        obj = _seen(collection, key)
        if obj is None:
            continue
        got = next((obj.get(f) for f in field_names if obj.get(f)), "")
        if str(got).lower() == want:
            return obj
    return None


def _collection_fresh(collection: str) -> bool:
    at = _SEEN_AT.get(collection)
    return at is not None and _now() - at <= CACHE_TTL


# --- read tools ---------------------------------------------------------------------

app = FastMCP("netdev", host="127.0.0.1", port=MCP_PORT)


# --- every tool runs on a worker thread (k8scc#132) ------------------------------------
# FastMCP 1.29.0 calls a plain function on the event loop itself
# (func_metadata.py: `return fn(**arguments)`), and a write waits in
# time.sleep for its witness and its health rounds. So for as long as one
# write ran, no session was served: fleet-ops-1e measured a second session's
# `initialize` unanswered 15 s into a 133 s confirm (jg-jcc2, 2026-11-25),
# which reads the same as a dead sidecar. What is registered is therefore an
# async wrapper that hands the call to a thread; the functions themselves stay
# plain, and the controls call them directly.
def _threaded(fn):
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return run


def _tool(fn):
    app.tool()(_threaded(fn))
    return fn


def _render(api: str, data) -> str:
    return f"[via {api}]\n" + json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def _read_one(collection: str, shape=None) -> str:
    refusal = _not_ready()
    if refusal:
        return refusal
    try:
        api, data = _controller().read(collection)
    except ControllerError as exc:
        return f"[via {ROUTES[collection][0]}]\nerror: {exc}"
    clean = _strip(data)
    # The whole read is remembered, not only the shaped view: a reservation is
    # set on a user that get_dhcp_reservations' view filters out.
    _remember(collection, clean)
    if shape is not None:
        clean = shape(clean)
    return _render(api, clean)


@_tool
def get_controller_info() -> str:
    """UniFi Network 應用程式的版本與 controller 資訊（唯讀）。"""
    return _read_one("info")


@_tool
def get_sites() -> str:
    """controller 上的 site 清單（唯讀）。UNIFI_SITE 決定其他工具讀哪一個。"""
    return _read_one("sites")


@_tool
def get_devices() -> str:
    """UniFi 設備清單：gateway、交換器、AP（唯讀；MAC、IP、型號、狀態）。"""
    return _read_one("devices")


@_tool
def get_clients() -> str:
    """目前連線的 client 清單（唯讀；MAC、IP、名稱、接在哪個設備）。"""
    return _read_one("clients")


@_tool
def get_networks() -> str:
    """網段設定（networkconf：VLAN、子網、DHCP 範圍；唯讀）。"""
    return _read_one("networks")


@_tool
def get_port_forwards() -> str:
    """port forward 規則（唯讀）。"""
    return _read_one("portforwards")


@_tool
def get_dhcp_reservations() -> str:
    """DHCP 固定 IP 保留（設了 use_fixedip 的 client；唯讀）。"""
    return _read_one("users", shape=lambda users: [u for u in users if isinstance(u, dict) and u.get("use_fixedip")])


@_tool
def get_wlans() -> str:
    """無線網路設定（唯讀；passphrase 一律剝掉，顯示為 <stripped>）。"""
    return _read_one("wlans")


@_tool
def get_dns_records() -> str:
    """閘道的靜態 DNS 紀錄（v2 static-dns，唯讀）：A 紀錄與條件式轉發（record_type NS，
    key 是網域、value 是轉發目標）。set_dns_forward／clear_dns_forward 只認這裡讀過的。"""
    return _read_one("dnsrecords")


@_tool
def get_firewall_rules() -> str:
    """防火牆：舊式規則（rest/firewallrule）與 Network 9 以後的 zone-based policy
    （v2 firewall-policies），每條附上展開後的來源／目的位址（唯讀）。兩種都讀：
    舊式那份是空的不代表沒有規則。"""
    refusal = _not_ready()
    if refusal:
        return refusal
    try:
        _, rules = _controller().read("firewallrules")
        _, groups = _controller().read("firewallgroups")
        _, policies = _controller().read("firewallpolicies")
        _, networks = _controller().read("networks")
    except ControllerError as exc:
        return f"[via legacy+v2]\nerror: {exc}"
    zones, zones_note = _read_zones()
    rules, groups, policies, networks = _strip(rules), _strip(groups), _strip(policies), _strip(networks)
    _remember("firewallgroups", groups)
    _remember("networks", networks)
    out_rules = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule = dict(rule)
        for side in ("src", "dst"):
            members, unresolved = _side_members(rule, side, groups, networks)
            rule[f"{side}_expanded"] = members if members is not None else "any"
            if unresolved:
                rule[f"{side}_unresolved"] = unresolved
        out_rules.append(rule)
    out_policies = []
    for pol in policies:
        if not isinstance(pol, dict):
            continue
        pol = dict(pol)
        for side, key in (("src", "source"), ("dst", "destination")):
            members, opaque = _policy_side(pol.get(key), zones, networks)
            pol[f"{side}_expanded"] = members if members is not None else "any"
            if opaque:
                pol[f"{side}_unresolved"] = opaque
        out_policies.append(pol)
    _remember("firewallrules", out_rules)
    _remember("firewallpolicies", out_policies)
    return _render("legacy+v2", {"legacy_rules": out_rules, "policies": out_policies, "zones": zones_note})


def _read_zones() -> "tuple[list | None, str]":
    """Zones are optional: without them a policy side given only as a zone
    cannot be expanded and counts as covering (class A), so their absence
    makes the guard stricter, never blind."""
    try:
        return _strip(_controller().read("firewallzones")[1]), "read"
    except ControllerError as exc:
        return None, f"unreadable ({exc}); a side given only by its zone is shown unresolved"


@_tool
def get_lan_inventory() -> str:
    """controller 側的 LAN 清冊：設備與 client 合併成一張表（MAC、IP、名稱、種類、上聯）。
    nmap／arp-scan 那半邊由你在 app container 自己跑，再照 policy 合併。"""
    refusal = _not_ready()
    if refusal:
        return refusal
    try:
        api, devices = _controller().read("devices")
        _, clients = _controller().read("clients")
    except ControllerError as exc:
        return f"[via {ROUTES['devices'][0]}]\nerror: {exc}"
    devices, clients = _strip(devices), _strip(clients)
    _remember("devices", devices)
    _remember("clients", clients)
    rows = []
    for kind, items in (("device", devices), ("client", clients)):
        for obj in items:
            if not isinstance(obj, dict):
                continue
            rows.append({
                "kind": kind,
                "mac": (obj.get("macAddress") or obj.get("mac") or "").lower(),
                "ip": obj.get("ipAddress") or obj.get("ip") or "",
                "name": obj.get("name") or obj.get("hostname") or "",
                "model": obj.get("model") or "",
                "uplink": obj.get("uplinkDeviceId") or obj.get("sw_mac") or obj.get("ap_mac") or "",
            })
    rows.sort(key=lambda r: (r["kind"], r["ip"], r["mac"]))
    return _render(api, rows)


# --- class A: is a target on im's own path? (pure; used by the write tools) ----------
#
# Inputs are reader output that has already been stripped; no I/O happens
# here, so the controls drive these with plain fixtures. Any input that could
# not be read is None, and None refuses — fail closed.

def _addr(s: str):
    s = s.strip()
    if "-" in s:
        lo, hi = (ipaddress.ip_address(p.strip()) for p in s.split("-", 1))
        return (lo, hi)
    net = ipaddress.ip_network(s, strict=False)
    return (net[0], net[-1])


def _contains(spec: str, ip: str) -> bool:
    try:
        lo, hi = _addr(spec)
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == lo.version and lo <= a <= hi


def _side_members(rule: dict, side: str, groups: list, networks: list) -> "tuple[list[str] | None, list[str]]":
    """The address specs one side of a rule matches, or None for 'any'.
    Second value: group / network ids that could not be resolved."""
    members, unresolved, constrained = [], [], False
    addr = (rule.get(f"{side}_address") or "").strip()
    if addr:
        constrained = True
        members.append(addr)
    by_id = {g.get("_id"): g for g in groups if isinstance(g, dict)}
    for gid in rule.get(f"{side}_firewallgroup_ids") or []:
        group = by_id.get(gid)
        if group is None:
            unresolved.append(gid)
            constrained = True
            continue
        if group.get("group_type", "address-group").startswith("address"):
            constrained = True
            members += [m for m in group.get("group_members") or [] if isinstance(m, str)]
    net_id = rule.get(f"{side}_networkconf_id")
    if net_id:
        constrained = True
        nets = {n.get("_id"): n for n in networks if isinstance(n, dict)}
        subnet = (nets.get(net_id) or {}).get("ip_subnet")
        if subnet:
            members.append(subnet)
        else:
            unresolved.append(net_id)
    return (members if constrained else None), unresolved


def _policy_side(side, zones, networks) -> "tuple[list[str] | None, list[str]]":
    """One side of a zone-based policy (v2 firewall-policies): the address
    specs it matches, or None for 'any address in reach'; second value: what
    could not be resolved (non-empty ⇒ the side counts as covering).

    Field names are fleet-ops-b0's reading of FAC1 (k8scc#67 finding 1:
    zone_id, network_ids, ips, port, matching_target(_type), match_opposite_*);
    the values of matching_target other than ANY/IP/NETWORK are not read, so
    every other target is opaque. A zone's own members come from the zone
    object (network_ids / networkIds); a zone this server did not read, or one
    with no networks (External, Gateway, VPN…), is opaque."""
    if not isinstance(side, dict):
        return None, ["(side missing)"]
    target = str(side.get("matching_target") or "ANY").upper()
    nets = {n.get("_id"): n for n in networks if isinstance(n, dict)}

    def subnets(ids):
        found, missing = [], []
        for nid in ids or []:
            subnet = (nets.get(nid) or {}).get("ip_subnet")
            (found.append(subnet) if subnet else missing.append(nid))
        return found, missing

    if target == "IP":
        ips = [i for i in side.get("ips") or [] if isinstance(i, str)]
        opaque = ["match_opposite_ips"] if side.get("match_opposite_ips") else []
        return ips, opaque or ([] if ips else ["ips (empty)"])
    if target == "NETWORK":
        found, missing = subnets(side.get("network_ids"))
        if side.get("match_opposite_networks"):
            missing.append("match_opposite_networks")
        return found, missing or ([] if found else ["network_ids (empty)"])
    if target != "ANY":
        return None, [f"matching_target {target}"]
    zid = side.get("zone_id")
    if zones is None:
        return None, [f"zone {zid} (zones unreadable)"]
    zone = next((z for z in zones if isinstance(z, dict) and (z.get("_id") or z.get("id")) == zid), None)
    if zone is None:
        return None, [f"zone {zid} (not among the zones read)"]
    found, missing = subnets(zone.get("network_ids") or zone.get("networkIds"))
    if not found and not missing:
        return None, [f"zone {zone.get('name') or zid} (no networks)"]
    return found, missing


def fw_policies_covering(addr_pairs: list, policies, zones, networks) -> "list[dict] | None":
    """Zone-based policies whose source and destination cover any (a, b) pair,
    in either direction. Same rule as fw_rules_covering: an unresolved side
    covers."""
    if policies is None or networks is None:
        return None
    hits = []
    for pol in policies:
        if not isinstance(pol, dict):
            continue
        sides = {s: _policy_side(pol.get(k), zones, networks) for s, k in (("src", "source"), ("dst", "destination"))}

        def matches(side, ip):
            members, opaque = sides[side]
            return bool(opaque) or members is None or any(_contains(m, ip) for m in members)

        if any((matches("src", a) and matches("dst", b)) or (matches("src", b) and matches("dst", a))
               for a, b in addr_pairs):
            hits.append(pol)
    return hits


def own_networks(pod_ip: str, controller_ip: str, api_server_ips: list, networks) -> "list[dict] | None":
    """The network objects containing POD_IP, the controller or an API server endpoint."""
    if networks is None:
        return None
    wanted = [ip for ip in [pod_ip, controller_ip, *api_server_ips] if ip]
    hits = []
    for net in networks:
        subnet = (net or {}).get("ip_subnet") if isinstance(net, dict) else None
        if subnet and any(_contains(subnet, ip) for ip in wanted):
            hits.append(net)
    return hits


def attachment(pod_ip: str, clients, devices) -> "list[str] | None":
    """The device ids / MACs (and port) this node is attached to: its client row's uplink."""
    if clients is None or devices is None:
        return None
    out = []
    for c in clients:
        if not isinstance(c, dict) or (c.get("ipAddress") or c.get("ip")) != pod_ip:
            continue
        for key in ("uplinkDeviceId", "sw_mac", "ap_mac"):
            if c.get(key):
                out.append(str(c[key]).lower())
        if c.get("sw_mac") and c.get("sw_port") is not None:
            out.append(f"{str(c['sw_mac']).lower()}#port{c['sw_port']}")
    return out


GATEWAY_TYPES = ("ugw", "udm", "uxg")   # UniFi's gateway device types


def _device_names(d: dict) -> set:
    return {str(d.get(k)).lower() for k in ("_id", "id", "mac", "macAddress") if d.get(k)}


def uplink_path(attached: list, devices) -> "tuple[list[dict], str]":
    """Every device between this node and the root of the UniFi tree: what the
    node is attached to, then each `uplink.uplink_mac` in turn, up to the device
    that has none, which must be a gateway (ugw/udm/uxg). Read on FAC1's console (UCG Ultra,
    Network 11.0.99, k8scc#104): every legacy stat/device row carries `uplink`,
    and only the gateway's has no `uplink_mac`. Returns (path, "") or ([], why
    the walk could not finish): an attachment or an uplink that names no device
    in the list, a device other than a gateway with no uplink, or a loop. The caller refuses on the second (fail closed): a
    device the walk could not reach may be the one that carries im's path."""
    by_name = {}
    for d in devices or []:
        if isinstance(d, dict):
            for n in _device_names(d):
                by_name[n] = d
    path, done = [], set()      # done: devices already walked to the root from an earlier attachment
    for a in attached:
        start = a.split("#", 1)[0]
        d = by_name.get(start)
        if d is None:
            return [], f"this node's attachment {start} is not in the device list"
        chain = set()
        while id(d) not in done:
            if id(d) in chain:
                return [], f"the uplinks loop at {d.get('name') or d.get('mac')}"
            chain.add(id(d))
            path.append(d)
            up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
            nxt = str(up.get("uplink_mac") or "").lower()
            if not nxt:
                # only a gateway may be the root: any other device with no uplink
                # is a chain cut short, not the top (fleet-ops-d5 on #106)
                if str(d.get("type") or "").lower() in GATEWAY_TYPES:
                    break
                return [], f"{d.get('name') or d.get('mac')} names no uplink and is not a gateway"
            parent = by_name.get(nxt)
            if parent is None:
                return [], f"{d.get('name') or d.get('mac')}'s uplink {nxt} is not in the device list"
            d = parent
        done |= chain
    return path, ""


def fw_rules_covering(addr_pairs: list, rules, groups, networks) -> "list[dict] | None":
    """Rules whose expanded source and destination cover any (a, b) pair, in
    either direction. An unresolved group or network counts as covering: a rule
    this server cannot read is not one it may call harmless. Negation is not
    modelled, so a negated side also counts as covering."""
    if rules is None or groups is None or networks is None:
        return None
    hits = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        sides = {}
        for side in ("src", "dst"):
            members, unresolved = _side_members(rule, side, groups, networks)
            sides[side] = (members, bool(unresolved) or bool(rule.get(f"{side}_address_negate")))

        def matches(side, ip):
            members, opaque = sides[side]
            return opaque or members is None or any(_contains(m, ip) for m in members)

        if any((matches("src", a) and matches("dst", b)) or (matches("src", b) and matches("dst", a))
               for a, b in addr_pairs):
            hits.append(rule)
    return hits


def class_a(target_collection: str, target_id: str, *, pod_ip: str, controller_ip: str, api_server_ips: list,
            networks, clients, devices, rules, groups, policies, zones=None) -> "str | None":
    """None when the target is off im's own path; otherwise what it intersects.
    Missing inputs refuse, naming which. `zones` may be None (see _read_zones);
    `policies` may not: on a zone-based console the legacy rules are empty and
    the policies are the firewall (k8scc#67 finding 1)."""
    missing = [name for name, value in (("networks", networks), ("clients", clients), ("devices", devices),
                                        ("firewall rules", rules), ("firewall groups", groups),
                                        ("firewall policies", policies),
                                        ("API server endpoints", api_server_ips)) if value is None]
    if not pod_ip:
        missing.append("POD_IP")
    if not controller_ip:
        missing.append("controller address")
    if missing:
        return "reader data unavailable (" + ", ".join(missing) + "), refusing (fail closed)"
    if target_collection in ("users", "clients"):
        own = {pod_ip, controller_ip, *api_server_ips}
        for c in clients:
            ip = (c.get("ipAddress") or c.get("ip")) if isinstance(c, dict) else None
            mac = str(c.get("macAddress") or c.get("mac") or "").lower() if isinstance(c, dict) else ""
            if mac and mac == target_id.lower() and ip in own:
                return f"client {target_id} is {ip}, an end of im's own path"
    if target_collection == "networks":
        for net in own_networks(pod_ip, controller_ip, api_server_ips, networks):
            if net.get("_id") == target_id:
                return f"network {net.get('name') or target_id} ({net.get('ip_subnet')}) carries im's own path"
    if target_collection == "devices":
        attached = attachment(pod_ip, clients, devices)
        # the target is named by id or MAC; a legacy client names its uplink
        # by MAC (sw_mac / ap_mac), so compare both forms of the device
        names = {target_id.lower()}
        for d in devices:
            if isinstance(d, dict) and target_id.lower() in _device_names(d):
                names |= _device_names(d)
        if any(a in names or a.split("#", 1)[0] in names for a in attached):
            return f"device {target_id} is what this node is attached to"
        # k8scc#104: the devices between the attachment and the gateway carry
        # im's path too (a node on Wi-Fi: the AP's switch; a second switch tier)
        path, broken = uplink_path(attached, devices)
        if broken:
            return f"the path from this node to the gateway cannot be read ({broken}), refusing every device (fail closed)"
        for d in path:
            if _device_names(d) & names:
                via = " → ".join(str(p.get("name") or p.get("mac")) for p in path)
                return f"device {target_id} is on the path from this node to the gateway ({via})"
    pairs = [p for p in [(pod_ip, controller_ip)] + [(pod_ip, ip) for ip in api_server_ips] if p[1]]
    if target_collection == "firewallrules":
        for rule in fw_rules_covering(pairs, rules, groups, networks):
            if rule.get("_id") == target_id:
                return f"firewall rule {rule.get('name') or target_id} covers im's own traffic"
    if target_collection == "firewallpolicies":
        for pol in fw_policies_covering(pairs, policies, zones, networks):
            if (pol.get("_id") or pol.get("id")) == target_id:
                return f"firewall policy {pol.get('name') or target_id} covers im's own traffic"
    return None


# --- write side: inputs -----------------------------------------------------------------

def _operator_state() -> "tuple[Operator | None, str]":
    """(operator, 'ok') or (None, why not). Absent, empty, malformed and a login
    the controller refuses all end the same way: no write tools."""
    if not OPERATOR_FILE:
        return None, "absent (UNIFI_OPERATOR_FILE unset)"
    if not os.path.isfile(OPERATOR_FILE):
        return None, f"absent ({OPERATOR_FILE})"
    lines = [ln.strip() for ln in _read_secret_file(OPERATOR_FILE).splitlines() if ln.strip()]
    if not lines:
        return None, f"empty ({OPERATOR_FILE})"
    if len(lines) != 2:
        return None, f"invalid ({OPERATOR_FILE}: expected two lines, user and password; found {len(lines)})"
    if _not_ready():
        return None, "not tried (the reader is not usable)"
    try:
        operator = Operator(lines[0], lines[1])
        operator.login()
    except ControllerError as exc:
        return None, f"invalid ({exc})"
    return operator, "ok"


@dataclass(frozen=True)
class Probe:
    raw: str
    kind: str      # "https" (GET, 2xx/3xx) or "tcp" (connect)
    host: str
    port: int


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def _parse_probes(raw: str) -> "tuple[list[Probe], str]":
    items = [p.strip() for p in raw.split(",") if p.strip()]
    if len(items) != 3:
        return [], f"NETDEV_OUTSIDE_PROBES needs exactly three probes, found {len(items)}"
    probes = []
    for item in items:
        if item.startswith("https://"):
            try:   # .port raises on a non-number or out-of-range port; at import that is a crash loop (k8scc#49)
                host, port = urlsplit(item).hostname, urlsplit(item).port
            except ValueError:
                return [], f"probe {item!r} has an invalid port"
            if not host:
                return [], f"probe {item!r} has no host"
            if port == 0:
                return [], f"probe {item!r} has an invalid port"
            if _is_ipv6(host):
                return [], f"probe {item!r}: probes go out over IPv4 only (k8scc#115); use an IPv4 address or a name"
            probes.append(Probe(item, "https", host, port or 443))
            continue
        if "://" in item:
            return [], f"probe {item!r}: only https://host/path or host:port"
        host, _, port = item.rpartition(":")
        if not host or not (port.isascii() and port.isdigit()) or not 0 < int(port) < 65536:   # '²'.isdigit() is True, int('²') raises
            return [], f"probe {item!r}: expected host:port"
        if _is_ipv6(host.strip("[]")):
            return [], f"probe {item!r}: probes go out over IPv4 only (k8scc#115); use an IPv4 address or a name"
        probes.append(Probe(item, "tcp", host, int(port)))
    return probes, ""


def _ticket_refusal() -> str:
    if not TICKET_REPO:
        return "IM_FAC_TICKET_REPO unset"
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", TICKET_REPO):
        return f"IM_FAC_TICKET_REPO {TICKET_REPO!r} is not owner/repo"
    if not TICKET_TOKEN_FILE or not _read_secret_file(TICKET_TOKEN_FILE):
        return f"token missing (IM_FAC_TICKET_TOKEN_FILE{'=' + TICKET_TOKEN_FILE if TICKET_TOKEN_FILE else ''})"
    scheme = urlsplit(TICKET_API).scheme
    if scheme != "https" and not (scheme == "http" and ALLOW_INSECURE_HTTP):
        return f"IM_FAC_TICKET_API {TICKET_API} refused: https only"
    return ""


def _snapshot_dir_refusal() -> str:
    if not SNAPSHOT_DIR:
        return "NETDEV_SNAPSHOT_DIR unset"
    if not os.path.isdir(SNAPSHOT_DIR) or not os.access(SNAPSHOT_DIR, os.W_OK):
        return f"NETDEV_SNAPSHOT_DIR {SNAPSHOT_DIR} is not a writable directory"
    return ""


class TicketError(Exception):
    pass


# The witness is FAC's comment, recognised by its prefix alone: every agent
# shares ferry133's GitHub identity, so the author cannot tell FAC from this
# server. Hence every comment the server writes starts with SERVER_MARK, and
# _comment refuses anything else.
PRE_ACK, POST_ACK = "pre-ack:", "post-ack:"
SERVER_MARK = "netdev-mcp "


class Tickets:
    """The im→FAC channel: GitHub issues on one private repo, straight REST."""

    def __init__(self, token: str):
        self._client = httpx.Client(base_url=TICKET_API, timeout=READ_TIMEOUT, follow_redirects=False,
                                    headers={"Authorization": f"Bearer {token}",
                                             "Accept": "application/vnd.github+json",
                                             "X-GitHub-Api-Version": "2022-11-28"})
        self.own: set = set()   # ids of comments this server wrote

    def _call(self, method: str, path: str, body=None):
        try:
            resp = self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise TicketError(f"{method} {path}: {type(exc).__name__}: {exc}") from None
        if resp.status_code // 100 != 2:
            raise TicketError(f"{method} {path}: GitHub answered {resp.status_code} {resp.text[:200]!r}")
        try:
            return resp.json()
        except ValueError:
            raise TicketError(f"{method} {path}: GitHub answered with a body that is not JSON") from None

    def open(self, title: str, body: str) -> int:
        data = self._call("POST", f"/repos/{TICKET_REPO}/issues", {"title": title, "body": body})
        return int(data["number"])

    def comment(self, number: int, body: str):
        """-> the new comment's id (None when GitHub's answer carried none)."""
        if not body.startswith(SERVER_MARK):
            raise AssertionError("a server comment must start with SERVER_MARK, never with a witness prefix")
        data = self._call("POST", f"/repos/{TICKET_REPO}/issues/{number}/comments", {"body": body})
        self.own.add(data.get("id"))
        return data.get("id")

    def witnessed(self, number: int, prefix: str, after=None) -> bool:
        """With `after` (the id of one of this server's comments), only what
        stands behind that comment in the listing counts, and nothing counts
        when that comment is not in the listing. The order is the listing's
        own, so no clock of ours is compared with GitHub's."""
        comments = self._call("GET", f"/repos/{TICKET_REPO}/issues/{number}/comments?per_page=100")
        if after is not None:
            at = next((i for i, c in enumerate(comments) if isinstance(c, dict) and c.get("id") == after), None)
            comments = [] if at is None else comments[at + 1:]
        return any(isinstance(c, dict) and c.get("id") not in self.own
                   and str(c.get("body") or "").lstrip().startswith(prefix) for c in comments)

    def close(self, number: int) -> None:
        self._call("PATCH", f"/repos/{TICKET_REPO}/issues/{number}", {"state": "closed"})

    def open_issues(self) -> list:
        """The repo's open issues, one page of 100 (pull requests left out)."""
        data = self._call("GET", f"/repos/{TICKET_REPO}/issues?state=open&per_page=100")
        if not isinstance(data, list):
            raise TicketError(f"GET issues: GitHub answered {type(data).__name__}, not a list")
        return [i for i in data if isinstance(i, dict) and "pull_request" not in i]

    def comments(self, number: int) -> list:
        data = self._call("GET", f"/repos/{TICKET_REPO}/issues/{number}/comments?per_page=100")
        if not isinstance(data, list):
            raise TicketError(f"GET comments of #{number}: GitHub answered {type(data).__name__}, not a list")
        return [c for c in data if isinstance(c, dict)]


OPERATOR, OPERATOR_STATE = _operator_state()
PROBES, PROBE_REFUSAL = _parse_probes(PROBES_RAW)
TICKET_REFUSAL = _ticket_refusal()
TICKETS = None if TICKET_REFUSAL else Tickets(_read_secret_file(TICKET_TOKEN_FILE))


def _write_blockers() -> list[str]:
    """Every write input that is not usable. Empty ⇒ the write tools are registered."""
    out = []
    if _not_ready():
        out.append("reader not usable")
    if OPERATOR is None:
        out.append(f"operator credential {OPERATOR_STATE}")
    if TICKET_REFUSAL:
        out.append(f"ticket channel: {TICKET_REFUSAL}")
    if PROBE_REFUSAL:
        out.append(f"outside probes: {PROBE_REFUSAL}")
    if not POD_IP:
        out.append("POD_IP unset")
    refusal = _snapshot_dir_refusal()
    if refusal:
        out.append(refusal)
    return out


WRITE_BLOCKERS = _write_blockers()


# --- write side: readings the gates take ----------------------------------------------------

def _run(argv: list, timeout: int = 20) -> "tuple[int, str, str]":
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{argv[0]}: {exc}"
    return r.returncode, r.stdout, r.stderr


def _ipv4_of(host: str) -> "tuple[str, str]":
    """(first IPv4 address of host, '') or ('', why not). Probes go out over
    IPv4 only (k8scc#115): the node im runs on is IPv4-only, and on jg-jcc2 a
    name publishing A and AAAA resolved to AAAA alone in 8 of 120 lookups
    (fleet-ops-d5), so an unspecified-family lookup failed the connect with
    ENETUNREACH while the network was fine. An A lookup cannot return that."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        return "", f"no IPv4 address for {host} ({exc})"
    return (infos[0][4][0], "") if infos else ("", f"no IPv4 address for {host}")


def _probe(p: Probe) -> str:
    """'' when the probe answered, else why not."""
    ip, why = _ipv4_of(p.host)
    if why:
        return f"{p.raw}: {why}"
    if p.kind == "tcp":
        try:
            with socket.create_connection((ip, p.port), timeout=PROBE_TIMEOUT):
                return ""
        except OSError as exc:
            return f"{p.raw}: {exc}"
    # Connect to the address read above; the name still goes out as SNI and
    # Host, and the certificate is verified against it.
    parts = urlsplit(p.raw)
    url = parts._replace(netloc=f"{ip}:{p.port}").geturl()
    host_header = p.host if p.port == 443 else f"{p.host}:{p.port}"
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            resp = client.send(client.build_request("GET", url, headers={"Host": host_header},
                                                    extensions={"sni_hostname": p.host}))
    except httpx.HTTPError as exc:
        return f"{p.raw}: {type(exc).__name__}: {exc}"
    return "" if resp.status_code // 100 in (2, 3) else f"{p.raw}: answered {resp.status_code}"


def _probes_lost() -> list[str]:
    return [why for why in (_probe(p) for p in PROBES) if why]


def _outbound(lost: list) -> str:
    return "ok" if not lost else ("degraded" if len(lost) == 1 else "lost")


def _api_server_ips() -> "tuple[list[str] | None, str]":
    """KUBERNETES_SERVICE_HOST plus the `kubernetes` endpoints (with hostNetwork
    the ClusterIP is translated to those, so they are the real other end)."""
    ips = [h for h in [os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()] if h]
    rc, out, err = _run([KUBECTL, "get", "endpoints", "kubernetes", "-n", "default", "-o", "json"])
    if rc != 0:
        return None, f"kubectl get endpoints kubernetes: {(err or out).strip() or f'exit {rc}'}"
    try:
        for subset in json.loads(out).get("subsets") or []:
            ips += [a["ip"] for a in subset.get("addresses") or [] if a.get("ip")]
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        return None, f"kubectl get endpoints kubernetes: not an Endpoints object ({exc})"
    return ips, ""


def _controller_ip() -> str:
    host = urlsplit(CONTROLLER_URL).hostname or ""
    try:
        return socket.getaddrinfo(host, None)[0][4][0]
    except (OSError, IndexError):
        return ""


def _apiserver_ready() -> str:
    rc, out, err = _run([KUBECTL, "get", "--raw", "/readyz"], timeout=15)
    return "" if rc == 0 and out.strip() == "ok" else f"/readyz: {(err or out).strip() or f'exit {rc}'}"


# --- DNS (k8scc#109) ---------------------------------------------------------------

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
DNS_RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


def _domain_or_refusal(value: str) -> "tuple[str, str]":
    """(normalised domain, '') or ('', why not). At least two labels: a forward
    of a single label (`com`, `lan`) sends a whole top-level name away."""
    d = (value or "").strip().lower().rstrip(".")
    labels = d.split(".") if d else []
    if not d or len(d) > 253 or not all(_LABEL.match(lb) for lb in labels):
        return "", f"domain {value!r} is not a domain name (letters, digits and hyphens, dot-separated)"
    if len(labels) < 2:
        return "", f"domain {value!r} has one label; forwarding it would send a whole top-level name away"
    return d, ""


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _dns_endpoint(ip: str) -> "tuple[str, int]":
    """Where a DNS query for `ip` goes. A patch point for the controls."""
    return ip, 53


def _dns_name(data: bytes, off: int) -> "tuple[str, int]":
    """A (possibly compressed) name at `off`: (name, offset after it)."""
    labels, jumped, end, hops = [], False, off, 0
    while True:
        n = data[off]
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = off + 2
            off, jumped, hops = ((n & 0x3F) << 8) | data[off + 1], True, hops + 1
            if hops > 32:
                raise ValueError("compression loop")
            continue
        if n == 0:
            return ".".join(labels).lower(), (end if jumped else off + 1)
        labels.append(data[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n


def _dns_soa(server: str, domain: str) -> "tuple[tuple | None, str]":
    """Ask `server` for the SOA of `domain` (one UDP query, standard library,
    PROBE_TIMEOUT). Returns (fingerprint, '') when it answered NOERROR or
    NXDOMAIN, else (None, why not). The fingerprint is (rcode, SOA MNAME, SOA
    serial), from the answer section or else the authority section, so two
    servers can be compared: a forward is in effect when the gateway answers
    as the target does (fleet-ops-d5 on #112: NOERROR alone also holds before
    the forward, through the gateway's public upstream)."""
    qid = secrets.randbits(16)
    qname = b"".join(bytes([len(lb)]) + lb.encode() for lb in domain.split(".")) + b"\x00"
    packet = qid.to_bytes(2, "big") + b"\x01\x00" + b"\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x06\x00\x01"
    host, port = _dns_endpoint(server)
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(PROBE_TIMEOUT)
            sock.sendto(packet, (host, port))
            deadline = _clock() + PROBE_TIMEOUT
            while True:
                sock.settimeout(max(0.01, deadline - _clock()))
                data, _ = sock.recvfrom(4096)
                if len(data) >= 12 and int.from_bytes(data[:2], "big") == qid and data[2] & 0x80:
                    break
    except OSError as exc:
        return None, f"{server}: no DNS answer for {domain} ({type(exc).__name__})"
    rcode = data[3] & 0x0F
    if rcode not in (0, 3):
        return None, f"{server}: answered {DNS_RCODES.get(rcode, rcode)} for {domain}"
    mname, serial = "", None
    try:
        off = 12
        for _ in range(int.from_bytes(data[4:6], "big")):
            off = _dns_name(data, off)[1] + 4
        records = int.from_bytes(data[6:8], "big") + int.from_bytes(data[8:10], "big")
        for _ in range(records):
            off = _dns_name(data, off)[1]
            rtype, rdlen = int.from_bytes(data[off:off + 2], "big"), int.from_bytes(data[off + 8:off + 10], "big")
            rdata = off + 10
            if rtype == 6:
                mname, after = _dns_name(data, rdata)
                after = _dns_name(data, after)[1]
                serial = int.from_bytes(data[after:after + 4], "big")
                break
            off = rdata + rdlen
    except (IndexError, ValueError):
        return None, f"{server}: an answer for {domain} that could not be parsed"
    return (DNS_RCODES[rcode], mname, serial), ""


def _dns_answers(server: str, domain: str) -> str:
    """'' when `server` answers for `domain` (NOERROR or NXDOMAIN), else why not."""
    return _dns_soa(server, domain)[1]


def _dns_effect(plan: "Plan", now: "tuple | None") -> "tuple[str, str]":
    """Whether the gateway's answer `now` shows the change: ('in effect' |
    'not in effect' | 'unmeasured', detail). set: the gateway answers as the
    target does. clear: it answers, and no longer as the old target does.
    When the gateway already answered that way before the apply, the reading
    cannot tell the change happened: unmeasured, never a pass."""
    if plan.tool == "set_dns_forward":
        want, why = _dns_soa(plan.fields.get("value") or "", plan.dns_domain)
        if want is None:
            return "not in effect", f"the target no longer answers ({why})"
        if plan.dns_baseline == want:
            return "unmeasured", f"the gateway answered as the target ({want}) before the apply"
        return (("in effect", f"the gateway answers as the target ({want})") if now == want
                else ("not in effect", f"the gateway answers {now}, the target {want}"))
    # A clear compares MNAME only: a gateway serving a cached answer of the old
    # target carries an older serial, and comparing the serial would read the
    # forward as gone while it is still there (fleet-ops-d5 on #112).
    old, _ = _dns_soa(str((plan.before or {}).get("value") or ""), plan.dns_domain)
    mname = lambda fp: fp[1] if fp and fp[1] else None
    if mname(old) is None or mname(plan.dns_baseline) != mname(old):
        return "unmeasured", (f"before the apply the gateway answered {plan.dns_baseline}, not with the old target's "
                              f"MNAME ({old}), so the forward was not visibly in effect to begin with")
    if mname(now) is None:
        # An answer with no SOA names nobody, so it cannot show the old target
        # is gone (k8scc#113, fleet-ops-d5 on #112): never a pass.
        return "unmeasured", f"the gateway answered {now} with no SOA, so whose answer it is cannot be read"
    return (("in effect", f"the gateway answers {now}, no longer with the old target's MNAME") if mname(now) != mname(old)
            else ("not in effect", f"the gateway still answers as the old target ({mname(old)})"))


def _dns_path_names() -> list[str]:
    """The names im's own path resolves: what this sidecar connects to, plus
    NETDEV_DNS_PROTECT for what it cannot see (chamber, cloudflared…)."""
    names = [urlsplit(CONTROLLER_URL).hostname or "", urlsplit(TICKET_API).hostname or "",
             os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()]
    names += [pr.host for pr in PROBES]
    names += [n.strip() for n in DNS_PROTECT_RAW.split(",")]
    out = []
    for n in names:
        n = n.lower().rstrip(".")
        if n and not _is_ip(n) and n not in out:
            out.append(n)
    return out


def _dns_on_path(domain: str) -> list[str]:
    """The names on im's path that a forward of `domain` would redirect."""
    return [n for n in _dns_path_names() if n == domain or n.endswith("." + domain)]


def _lan_resolver(ctx: dict) -> str:
    """The gateway of the network carrying this node (ip_subnet's host part):
    the resolver UniFi hands out by default. '' when none is found."""
    for net in own_networks(POD_IP, ctx["controller_ip"], ctx["api_server_ips"], ctx["networks"]) or []:
        subnet = str(net.get("ip_subnet") or "")
        if "/" in subnet and _contains(subnet, POD_IP):
            return subnet.split("/", 1)[0]
    return ""


def _class_a_inputs() -> "tuple[dict | None, str]":
    """Everything class A reads, fresh at each write. Any one unreadable ⇒ None
    with the reader's own error — every write then refuses (fail closed)."""
    ctx, errors = {}, []
    for name in ("networks", "clients", "devices", "firewallrules", "firewallgroups", "firewallpolicies"):
        try:
            ctx[name] = _strip(_controller().read(name)[1])
        except ControllerError as exc:
            errors.append(str(exc))
    ctx["firewallzones"], _ = _read_zones()
    ips, err = _api_server_ips()
    if ips is None:
        errors.append(err)
    ctx["api_server_ips"] = ips
    ctx["controller_ip"] = _controller_ip()
    if not ctx["controller_ip"]:
        errors.append(f"controller address: cannot resolve {urlsplit(CONTROLLER_URL).hostname!r}")
    return (None, "; ".join(errors)) if errors else (ctx, "")


def _gateway_ips(networks: list, controller_ip: str) -> set:
    """A network's ip_subnet is gateway/prefix in UniFi's own notation, so its
    host part is the gateway; the console hosting the controller counts too."""
    out = {controller_ip}
    for n in networks:
        subnet = n.get("ip_subnet") if isinstance(n, dict) else None
        if subnet and "/" in subnet:
            out.add(subnet.split("/", 1)[0])
    return out


# --- write side: plans ------------------------------------------------------------------------

@dataclass
class Plan:
    """What one write will do, built by the server from the cache — the model
    never supplies a body or an id it did not get from a reader."""
    tool: str
    collection: str            # the snapshot's collection
    obj_id: str                # '' for a create
    target: str                # audit / ticket / token key
    kind: str                  # put | create | delete | command
    fields: dict = field(default_factory=dict)
    before: "dict | None" = None
    endpoint: str = ""         # command: WRITES key
    command: "dict | None" = None
    inverse: "dict | None" = None   # command: the revert body; None ⇒ cannot be undone
    class_a: "tuple[str, str] | None" = None
    refuse_ips: tuple = ()     # addresses this write would give away (checked against im's own path)
    gateway_mac: str = ""      # restart_device, power_cycle_port: refused when this is the gateway
    unique: str = "name"       # the field a create must not collide with (snapshot check)
    dns_domain: str = ""       # set/clear_dns_forward: the forwarded domain (class A, health)
    dns_resolver: str = ""     # the LAN gateway the health check asks; set by class A
    dns_baseline: "tuple | None" = None   # the gateway's answer just before the apply


def _read_first(reader: str) -> str:
    return (f"read it first: call {reader} in this session — a write addresses only what a reader returned "
            f"within the last {CACHE_TTL} s, never an id or MAC that was not in one of its answers")


def _will_change(plan: Plan) -> dict:
    if plan.kind == "put":
        return {"before": plan.before, "after": {**plan.before, **plan.fields}, "changed": sorted(plan.fields)}
    if plan.kind == "create":
        return {"before": None, "after": dict(plan.fields), "changed": sorted(plan.fields)}
    if plan.kind == "delete":
        return {"before": plan.before, "after": None, "changed": ["(deleted)"]}
    return {"before": plan.before, "after": None, "command": plan.command,
            "undo": plan.inverse or "none: this cannot be undone"}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()


def _ip_or_refusal(value: str, what: str) -> str:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return f"{what} {value!r} is not an IP address"
    return ""


def _plan_reservation(tool: str, mac: str, ip: "str | None") -> "tuple[Plan | None, str]":
    if ip is not None:
        bad = _ip_or_refusal(ip, "ip")
        if bad:
            return None, bad
    user = _seen_by("users", ("mac",), mac)
    if user is None:
        return None, _read_first("get_dhcp_reservations")
    if ip is None:
        if not user.get("use_fixedip"):
            return None, f"{mac} has no DHCP reservation to clear"
        fields = {"use_fixedip": False}
    else:
        fields = {"use_fixedip": True, "fixed_ip": ip.strip()}
    return Plan(tool, "users", str(user["_id"]), f"users/{user['_id']}", "put", fields, before=user,
                class_a=("users", mac), refuse_ips=(ip.strip(),) if ip else ()), ""


def _plan_block(tool: str, mac: str, block: bool) -> "tuple[Plan | None, str]":
    client = _seen_by("clients", ("macAddress", "mac"), mac)
    if client is None:
        return None, _read_first("get_clients")
    mac = mac.strip().lower()
    do, undo = ("block-sta", "unblock-sta") if block else ("unblock-sta", "block-sta")
    cid = str(client.get("id") or client.get("_id"))
    return Plan(tool, "clients", cid, f"clients/{mac}", "command", before=client, endpoint="stamgr",
                command={"cmd": do, "mac": mac}, inverse={"cmd": undo, "mac": mac}, class_a=("clients", mac)), ""


def _plan_device(tool: str, mac: str, port: "int | None") -> "tuple[Plan | None, str]":
    device = _seen_by("devices", ("macAddress", "mac"), mac)
    if device is None:
        return None, _read_first("get_devices")
    mac = mac.strip().lower()
    did = str(device.get("id") or device.get("_id"))
    if port is None:
        return Plan(tool, "devices", did, f"devices/{mac}", "command", before=device, endpoint="devmgr",
                    command={"cmd": "restart", "mac": mac}, class_a=("devices", did), gateway_mac=mac), ""
    if port < 1:
        return None, f"port {port} is not a switch port number (they start at 1)"
    # Any port of the node's switch is refused (fail closed). Legacy stat/sta
    # carries sw_port, but that it names the node's own port on a real
    # console is not read yet (root §6), so the refusal stays whole-switch.
    return Plan(tool, "devices", did, f"devices/{mac}#port{port}", "command", before=device, endpoint="devmgr",
                command={"cmd": "power-cycle", "mac": mac, "port_idx": port}, class_a=("devices", did),
                gateway_mac=mac), ""


def _plan_port_forward(name: str, proto: str, wan_port: int, lan_ip: str, lan_port: int) -> "tuple[Plan | None, str]":
    if proto not in ("tcp", "udp", "tcp_udp"):
        return None, f"proto {proto!r}: one of tcp, udp, tcp_udp"
    for label, port in (("wan_port", wan_port), ("lan_port", lan_port)):
        if not isinstance(port, int) or not 0 < port < 65536:
            return None, f"{label} {port!r} is not a port number"
    bad = _ip_or_refusal(lan_ip, "lan_ip")
    if bad:
        return None, bad
    if not name.strip():
        return None, "name is required"
    if not _collection_fresh("portforwards"):
        return None, _read_first("get_port_forwards")
    fields = {"proto": proto, "dst_port": str(wan_port), "fwd": lan_ip.strip(), "fwd_port": str(lan_port), "enabled": True}
    existing = _seen_by("portforwards", ("name",), name)
    if existing is not None:
        return Plan("set_port_forward", "portforwards", str(existing["_id"]), f"portforwards/{existing['_id']}",
                    "put", fields, before=existing), ""
    return Plan("set_port_forward", "portforwards", "", f"portforwards/new:{name.strip()}", "create",
                {"name": name.strip(), **fields}), ""


def _plan_delete_port_forward(pf_id: str) -> "tuple[Plan | None, str]":
    pf = _seen("portforwards", pf_id.strip())
    if pf is None:
        return None, _read_first("get_port_forwards")
    return Plan("delete_port_forward", "portforwards", pf_id.strip(), f"portforwards/{pf_id.strip()}", "delete",
                before=pf), ""


def _dns_forwards() -> "tuple[list | None, str]":
    """The cached static-dns rows, or (None, read it first)."""
    if not _collection_fresh("dnsrecords"):
        return None, _read_first("get_dns_records")
    rows = [o for o in (_seen("dnsrecords", k) for k in list(_SEEN.get("dnsrecords", {}))) if o is not None]
    return rows, ""


def _plan_dns_forward(domain: str, target: str) -> "tuple[Plan | None, str]":
    d, bad = _domain_or_refusal(domain)
    if bad:
        return None, bad
    t = (target or "").strip()
    if not _is_ip(t):
        return None, f"target {target!r} is not an IP address (the forward goes to a resolver's address, not a name)"
    ip = ipaddress.ip_address(t)
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast:
        return None, f"target {t} is not a resolver anyone in the LAN can reach (unspecified, loopback or multicast)"
    rows, why = _dns_forwards()
    if rows is None:
        return None, why
    same_key = [r for r in rows if str(r.get("key") or "").lower().rstrip(".") == d]
    other = [r for r in same_key if str(r.get("record_type") or "").upper() != "NS"]
    if other:
        kinds = ", ".join(sorted({str(r.get("record_type")) for r in other}))
        return None, f"{d} already has a {kinds} record; a forward beside it would shadow it or be shadowed (refusing)"
    existing = next((r for r in same_key if str(r.get("record_type") or "").upper() == "NS"), None)
    why = _dns_answers(t, d)
    if why:
        return None, (f"target {t} does not answer for {d} ({why}); forwarding to it would make every name under "
                      f"{d} fail in the LAN")
    if existing is not None:
        if existing.get("value") == t and existing.get("enabled") is True:
            return None, f"{d} already forwards to {t}"
        return Plan("set_dns_forward", "dnsrecords", str(existing["_id"]), f"dnsrecords/{d}", "put",
                    {"value": t, "enabled": True}, before=existing, unique="key", dns_domain=d), ""
    return Plan("set_dns_forward", "dnsrecords", "", f"dnsrecords/{d}", "create",
                {"record_type": "NS", "key": d, "value": t, "port": 0, "enabled": True}, unique="key",
                dns_domain=d), ""


def _plan_clear_dns_forward(domain: str) -> "tuple[Plan | None, str]":
    d, bad = _domain_or_refusal(domain)
    if bad:
        return None, bad
    rows, why = _dns_forwards()
    if rows is None:
        return None, why
    existing = next((r for r in rows if str(r.get("key") or "").lower().rstrip(".") == d
                     and str(r.get("record_type") or "").upper() == "NS"), None)
    if existing is None:
        return None, f"{d} has no forward to clear (no NS record with that key in get_dns_records)"
    return Plan("clear_dns_forward", "dnsrecords", str(existing["_id"]), f"dnsrecords/{d}", "delete",
                before=existing, unique="key", dns_domain=d), ""


def _class_a_refusal(plan: Plan, ctx: dict) -> str:
    if plan.dns_domain:
        hit = _dns_on_path(plan.dns_domain)
        if hit:
            return (f"{', '.join(hit)} {'is' if len(hit) == 1 else 'are'} on im's own path and under "
                    f"{plan.dns_domain}; changing how the LAN resolves {plan.dns_domain} would change how im "
                    "reaches them")
        plan.dns_resolver = _lan_resolver(ctx)
        if not plan.dns_resolver:
            return ("no network carrying this node was read, so there is no LAN resolver to check the forward "
                    "against (fail closed)")
    own = {POD_IP, ctx["controller_ip"], *ctx["api_server_ips"]}
    for ip in plan.refuse_ips:
        if ip in own:
            return f"{ip} is an end of im's own path (this node, the controller or an API server)"
    if plan.gateway_mac:
        for d in ctx["devices"]:
            if not isinstance(d, dict):
                continue
            mac = str(d.get("macAddress") or d.get("mac") or "").lower()
            # legacy stat/device's `ip` for a gateway may be its WAN address, so
            # its type counts on its own (ugw/udm/uxg: UniFi's gateway types)
            if mac == plan.gateway_mac and ((d.get("ipAddress") or d.get("ip")) in _gateway_ips(ctx["networks"], ctx["controller_ip"])
                                            or str(d.get("type") or "").lower() in GATEWAY_TYPES):
                if plan.tool == "power_cycle_port":
                    # what hangs off a gateway port is not read (a downstream switch,
                    # the WAN modem), so every port is refused, like the node's switch
                    return (f"device {plan.gateway_mac} is the gateway: what its ports power is not read, "
                            "and one of them may feed the whole LAN or the WAN")
                return f"device {plan.gateway_mac} is the gateway: restarting it takes the whole LAN down, and there is no tool for that"
    if plan.class_a is None:
        return ""
    return class_a(plan.class_a[0], plan.class_a[1], pod_ip=POD_IP, controller_ip=ctx["controller_ip"],
                   api_server_ips=ctx["api_server_ips"], networks=ctx["networks"], clients=ctx["clients"],
                   devices=ctx["devices"], rules=ctx["firewallrules"], groups=ctx["firewallgroups"],
                   policies=ctx["firewallpolicies"], zones=ctx["firewallzones"]) or ""


# --- write side: audit, tickets, snapshot, apply, health, revert ----------------------------------

_AUDIT_OUT = None   # the real stdout; __main__ points sys.stdout at stderr
_AUDIT_LOCK = threading.Lock()   # tools run on threads: a line is written whole


def _audit(tool: str, target: str, phase: str, stated: str, **fields: object) -> None:
    """One JSON line per write phase to stdout — `kubectl logs -c netdev-mcp`
    is the only record of a direct-LAN write outside the ticket."""
    line = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "tool": tool, "target": target,
            "phase": phase, "stated_reason": stated, "user": USER_ID, "snapshot_digest": "", "diff_digest": "",
            "health": [], "outbound": "", "fac_witness": ""}
    line.update(fields)
    out = _AUDIT_OUT or sys.stdout
    with _AUDIT_LOCK:
        out.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.flush()


def _ticket_body(plan: Plan, change: dict, stated: str, deadline: float) -> str:
    when = _dt.datetime.fromtimestamp(deadline, _dt.timezone.utc).isoformat(timespec="seconds")
    return "\n".join([
        f"cluster: {CLUSTER or '(CLUSTER_NAME unset)'}",
        f"tool: {plan.tool}",
        f"target: {plan.target} (collection {plan.collection}, id {plan.obj_id or '(new)'})",
        f"deadline: {when}",
        f"stated_reason: {stated}",
        "",
        "will_change (secrets stripped):",
        "```json",
        json.dumps(change, indent=2, sort_keys=True, ensure_ascii=False),
        "```",
        "",
        f"FAC: comment `{PRE_ACK} <peer name [ref]>` once you can reach this cluster from outside, and "
        f"`{POST_ACK} <peer name [ref]>` after the change if you still can. The change is sent right after this "
        f"sidecar comments `{SERVER_MARK}applying` here: a `{POST_ACK}` counts only when it stands after that "
        "comment, so wait for it, then check, then comment. Without a pre-ack within "
        f"{int(WITNESS_WAIT)} s the write goes ahead unwitnessed. A token unused for {TOKEN_TTL} s closes this "
        "ticket as `result: not-executed`; an open ticket past its deadline with no release comment means this "
        "sidecar could not post one.",
    ])


def _wait_pre_ack(number: int) -> str:
    deadline = _clock() + WITNESS_WAIT
    while True:
        try:
            if TICKETS.witnessed(number, PRE_ACK):
                return "armed"
        except TicketError:
            pass
        if _clock() >= deadline:
            return "unmeasured"
        _sleep(max(0.0, min(WITNESS_POLL, deadline - _clock())))


def _announce_apply(number: int, plan: Plan) -> "tuple[object, str]":
    """Say on the ticket that the change is about to be sent (k8scc#126).
    -> (the comment's id, '') or (None, why). FAC had no way to tell from the
    ticket that the change was applied, and a post-ack left before it counted
    as the witness of what came after. A post-ack now counts only behind this
    comment, so without it there is no write: the same direction as a ticket
    that cannot be opened."""
    lines = [f"{SERVER_MARK}applying",
             f"the change is being sent to the controller now; a `{POST_ACK}` counts only after this comment"]
    if plan.dns_domain:
        lines.append(f"dns: the gateway reloads its resolver; this sidecar waits {int(DNS_SETTLE)} s before its first "
                     "check, and a look from outside before that sees the old state")
    try:
        cid = TICKETS.comment(number, "\n".join(lines))
    except TicketError as exc:
        return None, str(exc)
    return (cid, "") if cid is not None else (None, "GitHub's answer carried no comment id, so nothing could be placed after it")


def _release(number: int, result: str, outbound: str, witness: str, extra: "list[str]", close: bool = True) -> str:
    """'' when the ticket got its comment (and was closed), else why not."""
    head = "revert-failed" if result == "revert-failed" else "release"
    text = "\n".join([f"{SERVER_MARK}{head}", f"result: {result}", f"outbound: {outbound}",
                      f"fac_witness: {witness}", *extra])
    try:
        TICKETS.comment(number, text)
        if close:
            TICKETS.close(number)
    except TicketError as exc:
        return str(exc)
    return ""


@dataclass
class Snapshot:
    obj: "dict | None"
    ids: list
    digest: str
    path: str
    names: list = field(default_factory=list)


def _obj_id(obj) -> str:
    return str(obj.get("_id") or obj.get("id") or "") if isinstance(obj, dict) else ""


def _snapshot(plan: Plan) -> Snapshot:
    """The whole object and its collection's id list, unstripped, into the
    sidecar's own volume. Any failure raises: no snapshot, no write."""
    _, items = _controller().read(plan.collection)
    ids = sorted(i for i in (_obj_id(o) for o in items) if i)
    obj = next((o for o in items if _obj_id(o) == plan.obj_id), None) if plan.obj_id else None
    if plan.obj_id and obj is None:
        raise ControllerError(f"{plan.target} is no longer in {plan.collection}", answered=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record = {"taken_at": stamp, "tool": plan.tool, "target": plan.target, "collection": plan.collection,
              "object": obj, "ids": ids}
    raw = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode()
    path = os.path.join(SNAPSHOT_DIR, f"{stamp}-{plan.tool}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', plan.target)}.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        raise ControllerError(f"snapshot {path}: {exc}") from None
    kept = sorted(f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json"))
    for old in kept[:max(0, len(kept) - SNAPSHOT_KEEP)]:
        try:
            os.remove(os.path.join(SNAPSHOT_DIR, old))
        except OSError:
            pass
    names = [str(o.get(plan.unique)).lower() for o in items if isinstance(o, dict) and o.get(plan.unique)]
    return Snapshot(obj, ids, _digest(raw), path, names)


def _unchanged_since_token(plan: Plan, snap: Snapshot) -> str:
    """'' when the object is still what will_change was computed from."""
    if plan.kind == "create":
        mine = str(plan.fields.get(plan.unique) or "").lower()
        if mine and mine in snap.names:
            return (f"a {plan.collection} object with {plan.unique} {plan.fields[plan.unique]!r} appeared since the "
                    "token; read it again")
        return ""
    if plan.kind == "put":
        now = _strip(snap.obj)
        moved = [f for f in plan.fields if now.get(f) != plan.before.get(f)]
        if moved:
            return f"{plan.target} changed on the controller since the token ({', '.join(moved)}); read it again"
    return ""


def _rest(plan: Plan) -> str:
    return WRITES["rest"].format(collection=LEGACY_NAME[plan.collection])


def _op(plan: Plan, method: str, sub: str = "", body=None) -> list:
    """One object write on the plan's collection: legacy rest, or the
    collection's own v2 path where ROUTES says v2 (dnsrecords)."""
    api, path = ROUTES[plan.collection]
    if api == "v2":
        return OPERATOR.request_v2(method, path + sub, body)
    return OPERATOR.request(method, _rest(plan) + sub, body)


def _apply(plan: Plan, snap: Snapshot) -> "tuple[str, str, bool]":
    """(created _id, rc, maybe_applied). rc is 'ok' or the error; maybe_applied
    is False only when the controller itself refused the request."""
    try:
        if plan.kind == "put":
            _op(plan, "PUT", f"/{plan.obj_id}", {**snap.obj, **plan.fields})
        elif plan.kind == "create":
            data = _op(plan, "POST", "", dict(plan.fields))
            return (_obj_id(data[0]) if data else ""), "ok", True
        elif plan.kind == "delete":
            _op(plan, "DELETE", f"/{plan.obj_id}")
        else:
            OPERATOR.request("POST", WRITES[plan.endpoint], plan.command)
    except ControllerError as exc:
        return "", f"error: {exc}", not exc.answered
    return "", "ok", True


def _readback(plan: Plan, created_id: str) -> "str | None":
    """None when there is nothing to read back (a command), '' when the object
    reads back as will_change's after (the fields this tool sets), else why not."""
    if plan.kind == "command":
        return None
    try:
        _, items = _controller().read(plan.collection)
    except ControllerError as exc:
        return str(exc)
    by_id = {_obj_id(o): o for o in items}
    if plan.kind == "delete":
        return f"{plan.obj_id} is still there" if plan.obj_id in by_id else ""
    oid = plan.obj_id or created_id
    obj = by_id.get(oid)
    if obj is None:
        return f"{oid or '(the create returned no _id)'} is not in {plan.collection}"
    moved = [f for f, v in plan.fields.items() if obj.get(f) != v]
    return f"{', '.join(moved)} did not read back as written" if moved else ""


@dataclass
class Health:
    ok: bool
    passed: list
    failed: list
    outbound: str
    witness: str
    dns_effect: str = ""   # DNS forwards only: 'in effect' or 'unmeasured: …' (k8scc#112)


def _health(plan: Plan, created_id: str, number: int, witness: str, applying=None) -> Health:
    """Every HEALTH_INTERVAL until HEALTH_TIMEOUT: the first round in which every
    check passes ends it. A breakage that shows only after that round is not
    seen here — that is what FAC's post-ack is for."""
    if plan.dns_domain:
        # the forward lands ~30 s late and the gateway's resolver reloads
        # (k8scc#67 finding 8): a round before that would pass on the old state
        _sleep(DNS_SETTLE)
    deadline = _clock() + HEALTH_TIMEOUT
    while True:
        passed, failed = [], []
        try:
            _controller().read("info")
            passed.append("controller")
        except ControllerError as exc:
            failed.append(f"controller: {exc}")
        why = _apiserver_ready()
        (failed.append(f"apiserver: {why}") if why else passed.append("apiserver"))
        why = _readback(plan, created_id)
        if why is not None:
            (failed.append(f"readback: {why}") if why else passed.append("readback"))
        effect = ""
        if plan.dns_domain:
            now, why = _dns_soa(plan.dns_resolver, plan.dns_domain)
            if why:
                failed.append(f"dns: {why}")
            else:
                effect, detail = _dns_effect(plan, now)
                if effect == "in effect":
                    passed.append("dns")
                elif effect == "not in effect":
                    failed.append(f"dns: not in effect: {detail}")
                else:
                    effect = f"unmeasured: {detail}"   # neither a pass nor a failure
        lost = _probes_lost()
        outbound = _outbound(lost)
        (failed.append("probes: " + "; ".join(lost)) if len(lost) >= 2 else passed.append("probes"))
        seen = witness
        if witness == "armed":
            try:
                acked = TICKETS.witnessed(number, POST_ACK, after=applying)
            except TicketError:
                acked = False
            seen = "ok" if acked else "lost"
            (passed.append("post-ack") if acked else failed.append("post-ack: not (yet) on the ticket"))
        if not failed:
            return Health(True, passed, failed, outbound, seen, effect)
        if _clock() >= deadline:
            return Health(False, passed, failed, outbound, seen, effect)
        _sleep(HEALTH_INTERVAL)


def _revert(plan: Plan, snap: Snapshot, created_id: str) -> "tuple[bool, str]":
    """Back to the snapshot, from inside the LAN. (reverted?, what happened)."""
    try:
        if plan.kind == "put":
            _op(plan, "PUT", f"/{plan.obj_id}", snap.obj)
            _, items = _controller().read(plan.collection)
            now = next((o for o in items if _obj_id(o) == plan.obj_id), None) or {}
            moved = [f for f in plan.fields if f in snap.obj and now.get(f) != snap.obj.get(f)]
            if moved:
                return False, f"after the revert PUT, {', '.join(moved)} still differ from the snapshot"
            # The PUT merges, so a key the snapshot lacked can stay behind
            # (k8scc#67 finding 4). Inert only where INERT_RESIDUE says so.
            left = [f for f in plan.fields if f not in snap.obj and f in now]
            live = [f for f in left if not INERT_RESIDUE.get((plan.collection, f), lambda o: False)(now)]
            if live:
                return False, (f"after the revert PUT, {', '.join(live)} remain on {plan.obj_id} although the snapshot "
                               "had no such key: the controller merges a PUT, so the snapshot cannot remove them")
            inert = f"; {', '.join(left)} left behind but switched off" if left else ""
            return True, f"PUT the snapshot of {plan.obj_id} back{inert}"
        if plan.kind == "create":
            if plan.collection not in DELETE_REVERTS:
                return False, (f"a create in {plan.collection} is not reverted by DELETE on this console "
                               f"(k8scc#67 finding 5: DELETE rest/user answered 404); created {created_id or '(no _id)'} "
                               "is still there")
            _, items = _controller().read(plan.collection)
            new = sorted({_obj_id(o) for o in items} - set(snap.ids) - {""})
            if not created_id:
                return False, f"the create response returned no _id; new ids in {plan.collection}: {new}; deleting nothing"
            if new != [created_id]:
                return False, (f"new ids in {plan.collection} are {new}, not only the created {created_id}: someone "
                               "else's object must not be removed by this revert, so nothing was deleted")
            _op(plan, "DELETE", f"/{created_id}")
            _, items = _controller().read(plan.collection)
            ids = sorted(i for i in (_obj_id(o) for o in items) if i)
            return (True, f"DELETEd the created {created_id}") if ids == snap.ids \
                else (False, f"after the DELETE, {plan.collection} ids are {ids}, not the snapshot's {snap.ids}")
        if plan.kind == "delete":
            body = {k: v for k, v in snap.obj.items() if k != "_id"}
            data = _op(plan, "POST", "", body)
            new_id = _obj_id(data[0]) if data else ""
            # Read back like the other two paths: an accepted POST is not an
            # object (fleet-ops-65 measured a re-create that answered ok and
            # stored nothing reading as `reverted`, k8scc#45).
            _, items = _controller().read(plan.collection)
            now = next((o for o in items if new_id and _obj_id(o) == new_id), None)
            if now is None:
                return False, (f"re-create POST answered {new_id or '(no _id)'} but {plan.collection} does not "
                               f"hold it; {plan.obj_id} is still gone")
            moved = [k for k, v in body.items() if now.get(k) != v]
            return (False, f"re-created {plan.obj_id} as {new_id}, but {', '.join(moved)} differ from the snapshot") \
                if moved else (True, f"re-created {plan.obj_id} from the snapshot as {new_id}")
        if plan.inverse is None:
            return False, f"{plan.command['cmd']} cannot be undone; nothing was reverted"
        OPERATOR.request("POST", WRITES[plan.endpoint], plan.inverse)
        return True, f"sent {plan.inverse['cmd']} for {plan.inverse['mac']}"
    except ControllerError as exc:
        return False, str(exc)


# --- write side: the two phases -------------------------------------------------------------------

_TOKENS: dict[str, dict] = {}         # token -> {tool, target, diff, expires, ticket, witness}
_TOKENS_LOCK = threading.Lock()       # the expiry timers below run on their own threads
_LAST_WRITE: dict[str, float] = {}    # target -> _now() at its last execute (in memory, like reboot_node's)
_WRITE_TIMES: list[float] = []        # _now() of every execute, for the hourly ceiling
_LAST_DONE: dict[str, dict] = {}      # target -> {tool, result, undo} of its last execute
TITLE_PREFIX = f"[netdev] {CLUSTER or '(CLUSTER_NAME unset)'} "

# The cooldown is per target, so a set and its clear share it: 19 s after a
# set, fleet-ops-1e's clear was refused with 580 s left (k8scc#132), and a
# wrong reservation could not be taken back with the tool for ten minutes.
# One exception: while the last write of a target stands as `applied`, the
# tool that undoes it may run once inside the cooldown. That undo is a write
# like any other (ticket, snapshot, health), counts against the hourly
# ceiling, and after it both directions wait.
UNDO_OF = {"set_dhcp_reservation": "clear_dhcp_reservation", "clear_dhcp_reservation": "set_dhcp_reservation",
           "block_client": "unblock_client", "unblock_client": "block_client",
           "set_dns_forward": "clear_dns_forward", "clear_dns_forward": "set_dns_forward"}

# One write at a time (k8scc#132). The tools run on threads now, and two
# writes side by side would each read the other's half-applied state as their
# baseline. A second write is refused at once and told who is running; it does
# not queue, since its will_change would be stale by the time it ran.
_WRITE_BUSY = threading.Lock()
_WRITE_NOW: dict = {}


def _is_undo(tool: str, target: str) -> bool:
    done = _LAST_DONE.get(target)
    return bool(done and done["result"] == "applied" and not done["undo"] and UNDO_OF.get(done["tool"]) == tool)


def _prune_tokens(now: float, keep: "str | None" = None) -> None:
    with _TOKENS_LOCK:
        expired = [(t, e) for t, e in _TOKENS.items() if t != keep and now > e["expires"]]
        for token, _ in expired:
            del _TOKENS[token]
    for _, entry in expired:
        _release(entry["ticket"], "not-executed", "ok", entry["witness"], ["the token expired unused"])


def _expire_later(token: str) -> None:
    """Close an unused token's ticket at its own expiry, not at the next call:
    FAC reads a deadline passing without a release as "escalate", and for a
    write that never ran that would be a false page (fleet-ops-65, k8scc#45)."""
    def fire():
        with _TOKENS_LOCK:
            entry = _TOKENS.get(token)
            if entry is None or _now() <= entry["expires"]:
                return
            del _TOKENS[token]
        _release(entry["ticket"], "not-executed", "ok", entry["witness"], ["the token expired unused"])
    timer = threading.Timer(TOKEN_TTL + EXPIRY_GRACE, fire)
    timer.daemon = True
    timer.start()


def _respond(body: dict) -> str:
    return json.dumps(body, indent=2, sort_keys=False, ensure_ascii=False)


INBOX_NOTE = ("Write ~/workspace/inbox/{date}-netdev-revert-failed-report.md NOW, before anything else: the target, the "
              "will_change below, snapshot digest {digest}, what failed, and what you tried. This sidecar cannot "
              "write the inbox; you are the only way a person learns of this. Ticket #{ticket} stays open.")


def _write(tool: str, hint: str, reason: str, confirm: str, build) -> str:
    if not _WRITE_BUSY.acquire(blocking=False):
        running = dict(_WRITE_NOW)
        why = (f"another write is in progress ({running.get('tool')} {running.get('target')}, "
               f"{'confirm' if running.get('confirm') else 'token'} phase, started "
               f"{int(_now() - running.get('since', _now()))} s ago): one write at a time. The read tools still "
               "answer; call this again when that one has returned")
        _audit(tool, hint, "refused", (reason or "").strip(), reason=why)
        return f"refused: {why}"
    try:
        _WRITE_NOW.update(tool=tool, target=hint, confirm=bool(confirm), since=_now())
        return _write_one(tool, hint, reason, confirm, build)
    finally:
        _WRITE_NOW.clear()
        _WRITE_BUSY.release()


def _write_one(tool: str, hint: str, reason: str, confirm: str, build) -> str:
    stated = (reason or "").strip()

    def refuse(why: str, target: str = hint, **extra) -> str:
        _audit(tool, target, "refused", stated, reason=why, **extra)
        return f"refused: {why}"

    # ① a stated reason, before anything is read or sent
    if not stated:
        return refuse("reason is required: state what this change is for; it is written on every audit line")
    # ② the target comes from the cache, the body from the server
    plan, why = build()
    if plan is None:
        return refuse(why)
    target = plan.target
    now = _now()
    # ⑦ cooldown and hourly ceiling
    last = _LAST_WRITE.get(target)
    undo = _is_undo(tool, target)
    if last is not None and now - last < WRITE_COOLDOWN and not undo:
        left = int(WRITE_COOLDOWN - (now - last))
        done = _LAST_DONE.get(target) or {}
        back = UNDO_OF.get(done.get("tool", ""))
        hint_undo = (f"; the one exception is taking that write back: {back}, once" if back and done.get("result") == "applied"
                     and not done.get("undo") else "")
        return refuse(f"{target} was written {int(now - last)} s ago; cooldown is {WRITE_COOLDOWN} s, {left} s left{hint_undo}",
                      target)
    recent = [t for t in _WRITE_TIMES if now - t < 3600]
    if len(recent) >= WRITES_PER_HOUR:
        free_in = int(3600 - (now - min(recent)))
        return refuse(f"hourly ceiling reached: {len(recent)} of {WRITES_PER_HOUR} confirmed writes in the last hour, "
                      f"0 left; the next one frees in {free_in} s", target)
    # ③ class A, from fresh readings
    ctx, err = _class_a_inputs()
    if ctx is None:
        return refuse(f"class A inputs unreadable, refusing every write (fail closed): {err}", target)
    why = _class_a_refusal(plan, ctx)
    if why:
        return refuse(f"class A: {why} — im is the only agent that can revert from inside the LAN, and this "
                      "would lock it out", target)
    change = _will_change(plan)
    diff_digest = _digest(_canonical(change))

    if not confirm:
        # W1: a healthy baseline, and the ticket out of the LAN, before any token
        lost = _probes_lost()
        if lost:
            return refuse("baseline unhealthy, not writing: outside probe(s) not answering: " + "; ".join(lost),
                          target, outbound=_outbound(lost), diff_digest=diff_digest)
        deadline = now + WITNESS_WAIT + TOKEN_TTL + HEALTH_TIMEOUT
        try:
            number = TICKETS.open(f"{TITLE_PREFIX}{tool} {target}",
                                  _ticket_body(plan, _strip(change), stated, deadline))
        except TicketError as exc:
            return refuse(f"could not open the ticket on the im→FAC channel, not writing: {exc}", target,
                          outbound="ok", diff_digest=diff_digest)
        # W3: a witness if FAC is there; its absence is a reading, not a gate
        witness = _wait_pre_ack(number)
        now = _now()
        token = secrets.token_urlsafe(24)
        _TOKENS[token] = {"tool": tool, "target": target, "diff": diff_digest, "expires": now + TOKEN_TTL,
                          "ticket": number, "witness": witness}
        _audit(tool, target, "token", stated, diff_digest=diff_digest, outbound="ok", fac_witness=witness, ticket=number)
        _expire_later(token)
        _prune_tokens(now, keep=token)
        return _respond({
            "token": token, "target": target, "expires_in": TOKEN_TTL, "cooldown_after": WRITE_COOLDOWN,
            "ticket": number, "fac_witness": witness, "will_change": _strip(change),
            "note": ("No change was made. Re-read will_change against your reason; if it still holds, call "
                     f"{tool} again with the same arguments and confirm=<token> within {TOKEN_TTL} s. The token is "
                     "single-use and bound to this target and this will_change. At execute the server snapshots "
                     f"the object, applies, and checks health for up to {int(HEALTH_TIMEOUT)} s; if the controller, "
                     "the API server, the read-back, two of the three outside probes"
                     + (" or FAC's post-ack" if witness == "armed" else "")
                     + " fail, it puts the snapshot back from inside the LAN."),
        })

    # ④ the token: known, this target, in time, this will_change — consumed before acting
    with _TOKENS_LOCK:
        entry = _TOKENS.get(confirm)
        why = ""
        if entry is None:
            why = f"unknown or already-used token — call {tool} again without confirm for a fresh one"
        elif entry["tool"] != tool or entry["target"] != target:
            why = f"token was issued for {entry['tool']} {entry['target']}, not {tool} {target}"
        elif now > entry["expires"]:
            why = f"token expired ({TOKEN_TTL} s) — call {tool} again without confirm for a fresh one"
        elif entry["diff"] != diff_digest:
            why = ("will_change is no longer the one this token was issued for (the arguments or the cached "
                   f"object changed) — call {tool} again without confirm")
        else:
            del _TOKENS[confirm]   # consumed before anything is sent
    if why:
        _prune_tokens(now, keep=None if entry is None or now > entry["expires"] else confirm)
        return refuse(why, target, **({"diff_digest": diff_digest} if "will_change" in why else {}))
    _prune_tokens(now)
    _LAST_WRITE[target] = now
    _WRITE_TIMES.append(now)
    _LAST_DONE[target] = {"tool": tool, "result": "", "undo": undo}
    number, witness = entry["ticket"], entry["witness"]
    common = {"diff_digest": diff_digest, "ticket": number}

    # ⑤ snapshot — no snapshot, no write
    try:
        snap = _snapshot(plan)
        moved = _unchanged_since_token(plan, snap)
        if moved:
            raise ControllerError(moved, answered=True)
    except ControllerError as exc:
        why = f"snapshot failed, nothing was written: {exc}"
        _audit(tool, target, "aborted", stated, reason=why, outbound="ok", fac_witness=witness, **common)
        note = _release(number, "aborted", "ok", witness, [why])
        return _respond({"result": "aborted", "target": target, "ticket": number, "reason": why,
                         **({"release_not_posted": note} if note else {})})
    common["snapshot_digest"] = snap.digest

    applying, why = _announce_apply(number, plan)
    if why:
        why = f"the ticket could not be told that the change is about to be applied, nothing was written: {why}"
        _audit(tool, target, "aborted", stated, reason=why, outbound="ok", fac_witness=witness, **common)
        note = _release(number, "aborted", "ok", witness, [why])
        return _respond({"result": "aborted", "target": target, "ticket": number, "reason": why,
                         **({"release_not_posted": note} if note else {})})

    # The record that a change is being sent, written before it is (k8scc#132).
    # `execute` is written after health, up to NETDEV_HEALTH_TIMEOUT later; a
    # sidecar killed in between used to leave a change on the controller with
    # no audit line at all.
    _audit(tool, target, "applying", stated, outbound="ok", fac_witness=witness, **common)
    if plan.dns_domain:
        plan.dns_baseline = _dns_soa(plan.dns_resolver, plan.dns_domain)[0]
    created_id, rc, maybe_applied = _apply(plan, snap)
    if not maybe_applied:
        why = f"the controller refused the write, nothing changed: {rc}"
        _audit(tool, target, "aborted", stated, reason=why, rc=rc, outbound="ok", fac_witness=witness, **common)
        note = _release(number, "aborted", "ok", witness, [why])
        return _respond({"result": "aborted", "target": target, "ticket": number, "reason": why,
                         **({"release_not_posted": note} if note else {})})

    # ⑥ health, then revert from inside on any failure
    h = _health(plan, created_id, number, witness, applying)
    readings = {"outbound": h.outbound, "fac_witness": h.witness}
    _audit(tool, target, "execute", stated, rc=rc, health=h.passed, **readings, **common)
    if h.ok:
        _LAST_DONE[target]["result"] = "applied"
        effect = [f"dns_effect: {h.dns_effect}"] if h.dns_effect else []
        note = _release(number, "applied", h.outbound, h.witness, ["health: " + ", ".join(h.passed), *effect,
                                                                    f"snapshot_digest: {snap.digest}"])
        body = {"result": "applied", "target": target, "ticket": number, **readings, "health": h.passed,
                "snapshot_digest": snap.digest}
        if h.dns_effect:
            body["dns_effect"] = h.dns_effect
            if h.dns_effect != "in effect":
                body["dns_note"] = ("the record is written, but whether the LAN resolves through it could not be "
                                    "measured here; check it with a lookup from the LAN and say so in your report")
        if h.outbound == "degraded":
            body["note"] = "one outside probe did not answer (outbound: degraded); not reverted, but say so in your report"
        if note:
            body["release_not_posted"] = note
        return _respond(body)

    reverted, detail = _revert(plan, snap, created_id)
    extra = ["failed: " + "; ".join(h.failed), f"revert: {detail}", f"snapshot_digest: {snap.digest}"]
    if reverted:
        _audit(tool, target, "reverted", stated, reason="; ".join(h.failed), health=h.passed, **readings, **common)
        note = _release(number, "reverted", h.outbound, h.witness, extra)
        body = {"result": "reverted", "target": target, "ticket": number, **readings, "failed": h.failed,
                "revert": detail, "snapshot_digest": snap.digest}
        if note:
            body["release_not_posted"] = note
        return _respond(body)
    _audit(tool, target, "revert-failed", stated, reason=f"{'; '.join(h.failed)} | revert: {detail}",
           health=h.passed, **readings, **common)
    note = _release(number, "revert-failed", h.outbound, h.witness, extra, close=False)
    body = {"result": "revert-failed", "target": target, "ticket": number, **readings, "failed": h.failed,
            "revert": detail, "snapshot_digest": snap.digest, "will_change": _strip(change),
            "inbox": INBOX_NOTE.format(date=_dt.date.today().isoformat(), digest=snap.digest, ticket=number)}
    if note:
        body["release_not_posted"] = note
    return _respond(body)


# --- start-up: tickets a previous run left open (k8scc#132) ---------------------------------------

INTERRUPTED = "interrupted"


def _recover_tickets() -> list[str]:
    """Read once at start-up, before any tool is served. Tokens, cooldowns and
    a running health loop are all in memory, so a sidecar that stopped (the
    128Mi limit killed one on jg-jcc2, 2026-11-25) leaves its tickets open and
    nothing that will ever release them:
      - `applying` and no release: the change may be on the controller, nobody
        checked it, nobody reverted it. Say so on the ticket and in the audit
        stream, leave the ticket open, put the target's cooldown back. Nothing
        is reverted here: this process does not know what the controller holds
        now, and a person decides.
      - no `applying`: a token that died with the process. Released as
        not-executed, or FAC reads the deadline passing as "escalate".
    Assumes one sidecar per cluster at a time: a second one starting while the
    first is mid-write would mark that write interrupted."""
    if WRITE_BLOCKERS:
        return []
    try:
        issues = TICKETS.open_issues()
    except TicketError as exc:
        return [f"open tickets: unreadable ({exc}); a write interrupted before this start was not looked for"]
    mine = [i for i in issues if str(i.get("title") or "").startswith(TITLE_PREFIX)]
    lines = [f"open tickets: {len(mine)} of this cluster's" + (" (the listing is one page of 100; there may be more)"
                                                             if len(issues) >= 100 else "")]
    for issue in mine:
        number = issue.get("number")
        tool, _, target = str(issue["title"])[len(TITLE_PREFIX):].partition(" ")
        stated = next((ln[len("stated_reason: "):] for ln in str(issue.get("body") or "").split("\n")
                       if ln.startswith("stated_reason: ")), "")
        try:
            heads = [str(c.get("body") or "").split("\n")[0].strip() for c in TICKETS.comments(number)]
        except TicketError as exc:
            lines.append(f"ticket #{number} ({tool} {target}): comments unreadable ({exc}), left as it is")
            continue
        heads = [h[len(SERVER_MARK):] for h in heads if h.startswith(SERVER_MARK)]
        if "release" in heads or "revert-failed" in heads or INTERRUPTED in heads:
            kind = next(h for h in ("revert-failed", INTERRUPTED, "release") if h in heads)
            lines.append(f"ticket #{number} ({tool} {target}): open with a `{kind}` comment, left as it is")
            continue
        if "applying" in heads:
            now = _now()
            _LAST_WRITE[target] = now
            _WRITE_TIMES.append(now)
            why = ("the sidecar stopped after `applying` and before a release: the change may be on the controller, "
                   "unchecked and not reverted")
            _audit(tool, target, INTERRUPTED, stated, reason=why, ticket=number)
            text = "\n".join([f"{SERVER_MARK}{INTERRUPTED}", f"result: {INTERRUPTED}",
                              "found at start-up: this sidecar stopped after `applying` and before it released this "
                              "ticket. The change may be on the controller; no health check finished and nothing was "
                              "reverted. Nothing is reverted now either: a person decides. The snapshot is in the "
                              "sidecar's snapshot directory, and its digest is on the `applying` audit line if that "
                              "log survived. This ticket stays open."])
            try:
                TICKETS.comment(number, text)
                lines.append(f"ticket #{number} ({tool} {target}): INTERRUPTED write, said so on the ticket, left open; "
                             f"{target} is in cooldown")
            except TicketError as exc:
                lines.append(f"ticket #{number} ({tool} {target}): INTERRUPTED write, and the ticket could not be told "
                             f"({exc}); {target} is in cooldown")
            continue
        note = _release(number, "not-executed", "unmeasured", "unmeasured",
                        ["the sidecar restarted; the token issued for this ticket is gone"])
        lines.append(f"ticket #{number} ({tool} {target}): a token lost in the restart, "
                     + (f"release not posted ({note})" if note else "released as not-executed"))
    return lines


# --- write tools (registered only when every write input is usable) -----------------------------

def set_dhcp_reservation(mac: str, ip: str, reason: str, confirm: str = "") -> str:
    """為一個 client（以 MAC 指定，須先 get_dhcp_reservations 讀過）設定 DHCP 固定 IP。
    只動 use_fixedip 與 fixed_ip。兩段式：不帶 confirm ⇒ 開票、回 token 與 will_change；
    帶 confirm=<token> ⇒ 快照、套用、健檢，失敗就從 LAN 內還原。reason 必填。"""
    return _write("set_dhcp_reservation", f"users/{mac}", reason, confirm, lambda: _plan_reservation("set_dhcp_reservation", mac, ip))


def clear_dhcp_reservation(mac: str, reason: str, confirm: str = "") -> str:
    """取消一個 client 的 DHCP 固定 IP（只把 use_fixedip 設為 false）。兩段式，同 set_dhcp_reservation。"""
    return _write("clear_dhcp_reservation", f"users/{mac}", reason, confirm, lambda: _plan_reservation("clear_dhcp_reservation", mac, None))


def block_client(mac: str, reason: str, confirm: str = "") -> str:
    """封鎖一個 client（以 MAC 指定，須先 get_clients 讀過）。回滾＝解封。兩段式。"""
    return _write("block_client", f"clients/{mac}", reason, confirm, lambda: _plan_block("block_client", mac, True))


def unblock_client(mac: str, reason: str, confirm: str = "") -> str:
    """解封一個 client（須先 get_clients 讀過）。回滾＝再封鎖。兩段式。"""
    return _write("unblock_client", f"clients/{mac}", reason, confirm, lambda: _plan_block("unblock_client", mac, False))


def set_port_forward(name: str, proto: str, wan_port: int, lan_ip: str, lan_port: int, reason: str, confirm: str = "") -> str:
    """新增或修改一條 port forward（以 name 對應；須先 get_port_forwards 讀過）。
    proto：tcp／udp／tcp_udp。新增的在健檢失敗時只刪掉這次建立的那一條。兩段式。"""
    return _write("set_port_forward", f"portforwards/{name}", reason, confirm,
                  lambda: _plan_port_forward(name, proto, wan_port, lan_ip, lan_port))


def delete_port_forward(id: str, reason: str, confirm: str = "") -> str:
    """刪除一條 port forward（以 get_port_forwards 回的 _id 指定）。回滾＝從快照重建（_id 會換）。兩段式。"""
    return _write("delete_port_forward", f"portforwards/{id}", reason, confirm, lambda: _plan_delete_port_forward(id))


def restart_device(mac: str, reason: str, confirm: str = "") -> str:
    """重開一台 AP 或交換器（以 MAC 指定，須先 get_devices 讀過）。gateway 一律拒絕；
    本節點接著的那台也拒絕。重開無法還原：健檢失敗時結果是 revert-failed。兩段式。"""
    return _write("restart_device", f"devices/{mac}", reason, confirm, lambda: _plan_device("restart_device", mac, None))


def power_cycle_port(switch_mac: str, port: int, reason: str, confirm: str = "") -> str:
    """對交換器的一個 PoE port 斷電再送電（須先 get_devices 讀過）。本節點接著的那台交換器
    的任何 port 都拒絕（controller 沒說節點在哪個 port）；gateway 的任何 port 也拒絕。無法還原。兩段式。"""
    return _write("power_cycle_port", f"devices/{switch_mac}#port{port}", reason, confirm,
                  lambda: _plan_device("power_cycle_port", switch_mac, port))


def set_dns_forward(domain: str, target: str, reason: str, confirm: str = "") -> str:
    """新增或修改一條條件式 DNS 轉發：LAN 裡 domain 底下的名字改問 target（一個 IP，
    例如叢集的 k8s-gateway）。須先 get_dns_records 讀過。target 要先能回答這個網域；
    im 自己路徑上的名字在 domain 底下就拒絕。每次套用閘道的 DNS 會重載、整個 LAN 的
    解析短暫中斷，約 30 秒才生效，所以健檢先等 NETDEV_DNS_SETTLE 秒。新建的失敗就刪掉。兩段式。"""
    return _write("set_dns_forward", f"dnsrecords/{domain}", reason, confirm, lambda: _plan_dns_forward(domain, target))


def clear_dns_forward(domain: str, reason: str, confirm: str = "") -> str:
    """刪除一條條件式 DNS 轉發（以網域指定，須先 get_dns_records 讀過）。回滾＝從快照重建。
    套用時整個 LAN 的解析同樣會短暫中斷。兩段式。"""
    return _write("clear_dns_forward", f"dnsrecords/{domain}", reason, confirm, lambda: _plan_clear_dns_forward(domain))


WRITE_TOOLS = [set_dhcp_reservation, clear_dhcp_reservation, block_client, unblock_client,
               set_port_forward, delete_port_forward, restart_device, power_cycle_port,
               set_dns_forward, clear_dns_forward]

# Registered only when every write input is usable: there is no "present but
# refuses for lack of configuration" state.
if not WRITE_BLOCKERS:
    for _fn in WRITE_TOOLS:
        _tool(_fn)


# --- startup readings ------------------------------------------------------------------

def _startup_readings() -> list[str]:
    readings = []
    if URL_REFUSAL:
        readings.append(URL_REFUSAL)
    elif CONTROLLER_URL.startswith("http://"):
        readings.append("insecure http accepted (test only): NETDEV_ALLOW_INSECURE_HTTP=1")
    readings.append("controller: " + (urlsplit(CONTROLLER_URL).netloc or "(unset)") + f", site {SITE}")
    if CONTROLLER_URL.startswith("https://") and not MISSING_CONFIG:
        try:
            readings.append("TLS: " + _tls_context()[1])
        except ControllerError as exc:
            readings.append(f"TLS: unusable ({exc})")
    # exactly "ok": jg-base's daily-check row 31 reads anything else as a fail.
    # "ok" means the account file is usable; whether the console accepts its
    # login shows on the `controller version:` line below (it reads first).
    readings.append("reader credential: " + ("ok" if not MISSING_CONFIG
                                             else "missing " + ", ".join(MISSING_CONFIG)))
    readings.append(f"operator credential: {OPERATOR_STATE}")
    readings.append("ticket channel: " + (f"{TICKET_REPO} via {urlsplit(TICKET_API).netloc}" if not TICKET_REFUSAL
                                          else "missing (" + TICKET_REFUSAL + ")"))
    readings.append("outside probes: " + (", ".join(p.raw for p in PROBES) if not PROBE_REFUSAL else PROBE_REFUSAL))
    readings.append(f"POD_IP: {POD_IP or '(unset)'}")
    readings.append("snapshot dir: " + (_snapshot_dir_refusal() or f"{SNAPSHOT_DIR} (keep {SNAPSHOT_KEEP})"))
    if WRITE_BLOCKERS:
        readings.append("write tools: not registered — " + "; ".join(WRITE_BLOCKERS))
    else:
        readings.append("write tools: registered — " + ", ".join(fn.__name__ for fn in WRITE_TOOLS))
        readings.append("dns: settle " + f"{DNS_SETTLE:g} s; names on im's path a forward may not cover: "
                        + (", ".join(_dns_path_names()) or "none"))
    readings.append("routes: " + ", ".join(f"{c}={api}" for c, (api, _) in sorted(ROUTES.items()))
                    + "; writes=legacy, dnsrecords=v2 (stat/device, stat/sta, self/sites, v2 firewall/zone not yet read on a console)")
    if not WRITE_BLOCKERS:
        lost = _probes_lost()
        readings.append("outside probes baseline: " + (f"{_outbound(lost)} — " + "; ".join(lost) if lost else "ok (3/3)"))
        ctx, err = _class_a_inputs()
        if ctx is None:
            readings.append(f"class A: unreadable, every write will refuse ({err})")
        else:
            nets = own_networks(POD_IP, ctx["controller_ip"], ctx["api_server_ips"], ctx["networks"])
            readings.append("class A: own networks " + (", ".join(f"{n.get('name')} {n.get('ip_subnet')}" for n in nets) or "none")
                            + f"; controller {ctx['controller_ip']}; API server {', '.join(ctx['api_server_ips']) or 'none'}"
                            + f"; attached to {', '.join(attachment(POD_IP, ctx['clients'], ctx['devices'])) or 'nothing read'}")
    if not _not_ready():
        try:
            _, info = _controller().read("info")
            first = info[0] if info and isinstance(info[0], dict) else {}
            version = first.get("version")
            readings.append(f"controller version: {version or 'not reported'}")
        except ControllerError as exc:
            readings.append(f"controller version: unreadable ({exc})")
    return readings


if __name__ == "__main__":
    # stdout is the audit stream and nothing else (spec): keep it for _audit,
    # and give everyone else — uvicorn's access log is a StreamHandler on
    # ext://sys.stdout — stderr instead.
    _AUDIT_OUT = sys.stdout
    sys.stdout = sys.stderr
    for reading in _startup_readings() + _recover_tickets():
        print(f"netdev-mcp: {reading}", file=sys.stderr, flush=True)
    app.run(transport="sse")
