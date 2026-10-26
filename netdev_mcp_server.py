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
          UNIFI_READER_KEY_FILE — the Integration API key (`X-API-Key`), read
          from the file at startup, never from env or argv.
          When any of the three is missing every tool answers "not configured",
          naming what is missing, and makes no request.

Optional: UNIFI_SITE — the legacy site name (default `default`); the
          Integration API's site id is looked up from it.
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
          2xx/3xx) or `host:port` (TCP connect).
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
Their values are PROVISIONAL until root spike 1.1 reads FAC1's real console;
every read response says which API answered (`[via integration]` /
`[via legacy]`), so a route the spike moves shows up in the output, not only
in this file.

stdout carries the audit lines and nothing else: __main__ points sys.stdout at
stderr before the server starts (uvicorn's access log is a StreamHandler on
ext://sys.stdout, resolved when uvicorn configures logging) and keeps the real
stdout for _audit alone.
"""
import datetime as _dt
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP

CONTROLLER_URL = os.environ.get("UNIFI_CONTROLLER_URL", "").strip().rstrip("/")
SITE = os.environ.get("UNIFI_SITE", "").strip() or "default"
CA_FILE = os.environ.get("UNIFI_CA_FILE", "").strip()
READER_KEY_FILE = os.environ.get("UNIFI_READER_KEY_FILE", "").strip()
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

# collection -> (api, path). `integration` paths are under
# /proxy/network/integration/v1 and take {site_id}; `legacy` paths are under
# /proxy/network/api/s/{site}. PROVISIONAL — root spike 1.1 (FAC1) has not
# read which API serves which collection, nor whether the reader's API key is
# accepted on the legacy paths. Change the data here, not the tools.
ROUTES = {
    "info":           ("integration", "/info"),
    "sites":          ("integration", "/sites"),
    "devices":        ("integration", "/sites/{site_id}/devices"),
    "clients":        ("integration", "/sites/{site_id}/clients"),
    "networks":       ("legacy", "/rest/networkconf"),
    "firewallrules":  ("legacy", "/rest/firewallrule"),
    "firewallgroups": ("legacy", "/rest/firewallgroup"),
    "portforwards":   ("legacy", "/rest/portforward"),
    "users":          ("legacy", "/rest/user"),
    "wlans":          ("legacy", "/rest/wlanconf"),
}
API_PREFIX = {"integration": "/proxy/network/integration/v1", "legacy": "/proxy/network/api/s/{site}"}

# The operator's side, all legacy (cookie + CSRF from a local account).
# PROVISIONAL for the same reason as ROUTES: these are UniFi's community-
# documented paths, not FAC1's reading (root spike 1.1, sub-change 1.2).
LOGIN_PATH = "/api/auth/login"
WRITES = {
    "rest":   "/rest/{collection}",          # PUT …/{_id}, POST (create), DELETE …/{_id}
    "stamgr": "/cmd/stamgr",                  # block-sta / unblock-sta
    "devmgr": "/cmd/devmgr",                  # restart / power-cycle
}
LEGACY_NAME = {"networks": "networkconf", "firewallrules": "firewallrule", "firewallgroups": "firewallgroup",
               "portforwards": "portforward", "users": "user", "wlans": "wlanconf"}
PAGE_LIMIT = 200
MAX_PAGES = 50


# --- configuration -----------------------------------------------------------

def _read_secret_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _missing_config() -> list[str]:
    missing = []
    if not CONTROLLER_URL:
        missing.append("controller URL (UNIFI_CONTROLLER_URL)")
    if not READER_KEY_FILE or not _read_secret_file(READER_KEY_FILE):
        # absent, unreadable and 0 bytes are the same thing: a Secret key
        # rendered from an unset value mounts as an empty file
        missing.append(f"reader API key (UNIFI_READER_KEY_FILE{'=' + READER_KEY_FILE if READER_KEY_FILE else ''})")
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
_READER_KEY = "" if MISSING_CONFIG else _read_secret_file(READER_KEY_FILE)


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

_SECRET_KEY = re.compile(r"^x_|_secret$|_key$|passphrase|password|psk", re.I)
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


def _controller_client(headers: dict) -> httpx.Client:
    verify = CA_FILE if CONTROLLER_URL.startswith("https://") else True
    try:
        return httpx.Client(base_url=CONTROLLER_URL, verify=verify, timeout=READ_TIMEOUT,
                            headers={"Accept": "application/json", **headers}, follow_redirects=False)
    except (OSError, ValueError) as exc:   # ssl.SSLError is an OSError
        raise ControllerError(f"controller CA {CA_FILE} (UNIFI_CA_FILE) cannot be loaded: {exc}") from None


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


class Controller:
    """The reader: one HTTP client, two API shapes, the reader key sent as
    `X-API-Key` on both. Whether the legacy paths accept it is root spike 1.1's
    reading — if they do not, the answer is a 401 naming the route, not a
    silent empty list. Writes never go through this client (see Operator)."""

    def __init__(self):
        self._client = _controller_client({"X-API-Key": _READER_KEY})
        self._site_id = ""

    def _get(self, path: str):
        try:
            resp = self._client.get(path)
        except httpx.HTTPError as exc:
            # the exception text names the URL and the failure, never a header
            raise ControllerError(f"GET {path}: {type(exc).__name__}: {exc}") from None
        if resp.status_code // 100 != 2:
            raise ControllerError(f"GET {path}: controller answered {resp.status_code} {resp.text[:200]!r}")
        try:
            return resp.json()
        except ValueError:
            raise ControllerError(f"GET {path}: controller answered {resp.status_code} with a body that is not JSON") from None

    def _path(self, collection: str) -> "tuple[str, str]":
        api, path = ROUTES[collection]
        prefix = API_PREFIX[api].format(site=SITE)
        if "{site_id}" in path:
            path = path.format(site_id=self.site_id())
        return api, prefix + path

    def site_id(self) -> str:
        """The Integration API's id for UNIFI_SITE (matched on internalReference, then name)."""
        if not self._site_id:
            sites = self._integration_list(API_PREFIX["integration"] + ROUTES["sites"][1])
            for s in sites:
                if SITE in (s.get("internalReference"), s.get("name")):
                    self._site_id = s.get("id") or ""
                    break
            if not self._site_id:
                names = ", ".join(str(s.get("internalReference") or s.get("name")) for s in sites) or "none"
                raise ControllerError(f"site {SITE!r} (UNIFI_SITE) not found among the controller's sites ({names})")
        return self._site_id

    def _integration_list(self, path: str) -> list:
        items, offset = [], 0
        for _ in range(MAX_PAGES):
            sep = "&" if "?" in path else "?"
            page = self._get(f"{path}{sep}offset={offset}&limit={PAGE_LIMIT}")
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ControllerError(f"GET {path}: not an Integration API page (no data list)")
            items += page["data"]
            offset += len(page["data"])
            total = page.get("totalCount")
            if not page["data"] or (isinstance(total, int) and offset >= total):
                return items
        raise ControllerError(f"GET {path}: more than {MAX_PAGES} pages of {PAGE_LIMIT}, stopped")

    def read(self, collection: str) -> "tuple[str, object]":
        """(api, data) for one collection, unstripped."""
        api, path = self._path(collection)
        if api == "integration":
            if collection == "info":
                return api, self._get(path)
            return api, self._integration_list(path)
        try:
            resp = self._client.get(path)
        except httpx.HTTPError as exc:
            raise ControllerError(f"GET {path}: {type(exc).__name__}: {exc}") from None
        return api, _legacy_data("GET", path, resp)


class Operator:
    """The writer: the local operator account over the legacy API, cookie +
    CSRF. Its own HTTP client, so the reader key is never sent on a write and
    the operator cookie never on a read. Neither the password nor the login
    response body is ever put into a message."""

    def __init__(self, user: str, password: str):
        self._user, self._password = user, password
        self._client = _controller_client({})
        self._csrf = ""

    def _take_csrf(self, resp: httpx.Response) -> None:
        self._csrf = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token") or self._csrf

    def login(self) -> None:
        try:
            resp = self._client.post(LOGIN_PATH, json={"username": self._user, "password": self._password})
        except httpx.HTTPError as exc:
            raise ControllerError(f"POST {LOGIN_PATH}: {type(exc).__name__}: {exc}") from None
        if resp.status_code // 100 != 2:
            raise ControllerError(f"POST {LOGIN_PATH}: controller refused the operator login ({resp.status_code})")
        self._take_csrf(resp)

    def request(self, method: str, path: str, body=None) -> list:
        """One legacy call; a 401 logs in again once (the session expired)."""
        path = API_PREFIX["legacy"].format(site=SITE) + path
        for attempt in (0, 1):
            try:
                resp = self._client.request(method, path, json=body,
                                            headers={"X-CSRF-Token": self._csrf} if self._csrf else {})
            except httpx.HTTPError as exc:
                raise ControllerError(f"{method} {path}: {type(exc).__name__}: {exc}") from None
            self._take_csrf(resp)
            if resp.status_code == 401 and attempt == 0:
                self.login()
                continue
            return _legacy_data(method, path, resp)
        raise AssertionError("unreachable")


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
    if isinstance(items, list):
        _SEEN_AT[collection] = now
    bucket = _SEEN.setdefault(collection, {})
    for obj in items if isinstance(items, list) else [items]:
        if isinstance(obj, dict):
            key = obj.get("_id") or obj.get("id")
            if key:
                bucket[str(key)] = (obj, now)


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


@app.tool()
def get_controller_info() -> str:
    """UniFi Network 應用程式的版本與 controller 資訊（唯讀）。"""
    return _read_one("info")


@app.tool()
def get_sites() -> str:
    """controller 上的 site 清單（唯讀）。UNIFI_SITE 決定其他工具讀哪一個。"""
    return _read_one("sites")


@app.tool()
def get_devices() -> str:
    """UniFi 設備清單：gateway、交換器、AP（唯讀；MAC、IP、型號、狀態）。"""
    return _read_one("devices")


@app.tool()
def get_clients() -> str:
    """目前連線的 client 清單（唯讀；MAC、IP、名稱、接在哪個設備）。"""
    return _read_one("clients")


@app.tool()
def get_networks() -> str:
    """網段設定（networkconf：VLAN、子網、DHCP 範圍；唯讀）。"""
    return _read_one("networks")


@app.tool()
def get_port_forwards() -> str:
    """port forward 規則（唯讀）。"""
    return _read_one("portforwards")


@app.tool()
def get_dhcp_reservations() -> str:
    """DHCP 固定 IP 保留（設了 use_fixedip 的 client；唯讀）。"""
    return _read_one("users", shape=lambda users: [u for u in users if isinstance(u, dict) and u.get("use_fixedip")])


@app.tool()
def get_wlans() -> str:
    """無線網路設定（唯讀；passphrase 一律剝掉，顯示為 <stripped>）。"""
    return _read_one("wlans")


@app.tool()
def get_firewall_rules() -> str:
    """防火牆規則，每條附上 group 展開後的來源／目的位址（唯讀）。"""
    refusal = _not_ready()
    if refusal:
        return refusal
    try:
        api, rules = _controller().read("firewallrules")
        _, groups = _controller().read("firewallgroups")
        _, networks = _controller().read("networks")
    except ControllerError as exc:
        return f"[via {ROUTES['firewallrules'][0]}]\nerror: {exc}"
    rules, groups, networks = _strip(rules), _strip(groups), _strip(networks)
    _remember("firewallgroups", groups)
    _remember("networks", networks)
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule = dict(rule)
        for side in ("src", "dst"):
            members, unresolved = _side_members(rule, side, groups, networks)
            rule[f"{side}_expanded"] = members if members is not None else "any"
            if unresolved:
                rule[f"{side}_unresolved"] = unresolved
        out.append(rule)
    _remember("firewallrules", out)
    return _render(api, out)


@app.tool()
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
            networks, clients, devices, rules, groups) -> "str | None":
    """None when the target is off im's own path; otherwise what it intersects.
    Missing inputs refuse, naming which."""
    missing = [name for name, value in (("networks", networks), ("clients", clients), ("devices", devices),
                                        ("firewall rules", rules), ("firewall groups", groups),
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
        tid = target_id.lower()
        if any(a == tid or a.startswith(tid + "#") for a in attached):
            return f"device {target_id} is what this node is attached to"
    if target_collection == "firewallrules":
        pairs = [(pod_ip, controller_ip)] + [(pod_ip, ip) for ip in api_server_ips]
        for rule in fw_rules_covering([p for p in pairs if p[1]], rules, groups, networks):
            if rule.get("_id") == target_id:
                return f"firewall rule {rule.get('name') or target_id} covers im's own traffic"
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


def _parse_probes(raw: str) -> "tuple[list[Probe], str]":
    items = [p.strip() for p in raw.split(",") if p.strip()]
    if len(items) != 3:
        return [], f"NETDEV_OUTSIDE_PROBES needs exactly three probes, found {len(items)}"
    probes = []
    for item in items:
        if item.startswith("https://"):
            host = urlsplit(item).hostname
            if not host:
                return [], f"probe {item!r} has no host"
            probes.append(Probe(item, "https", host, urlsplit(item).port or 443))
            continue
        if "://" in item:
            return [], f"probe {item!r}: only https://host/path or host:port"
        host, _, port = item.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            return [], f"probe {item!r}: expected host:port"
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

    def comment(self, number: int, body: str) -> None:
        if not body.startswith(SERVER_MARK):
            raise AssertionError("a server comment must start with SERVER_MARK, never with a witness prefix")
        data = self._call("POST", f"/repos/{TICKET_REPO}/issues/{number}/comments", {"body": body})
        self.own.add(data.get("id"))

    def witnessed(self, number: int, prefix: str) -> bool:
        comments = self._call("GET", f"/repos/{TICKET_REPO}/issues/{number}/comments?per_page=100")
        return any(isinstance(c, dict) and c.get("id") not in self.own
                   and str(c.get("body") or "").lstrip().startswith(prefix) for c in comments)

    def close(self, number: int) -> None:
        self._call("PATCH", f"/repos/{TICKET_REPO}/issues/{number}", {"state": "closed"})


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


def _probe(p: Probe) -> str:
    """'' when the probe answered, else why not."""
    if p.kind == "tcp":
        try:
            with socket.create_connection((p.host, p.port), timeout=PROBE_TIMEOUT):
                return ""
        except OSError as exc:
            return f"{p.raw}: {exc}"
    try:
        resp = httpx.get(p.raw, timeout=PROBE_TIMEOUT, follow_redirects=False)
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


def _class_a_inputs() -> "tuple[dict | None, str]":
    """Everything class A reads, fresh at each write. Any one unreadable ⇒ None
    with the reader's own error — every write then refuses (fail closed)."""
    ctx, errors = {}, []
    for name in ("networks", "clients", "devices", "firewallrules", "firewallgroups"):
        try:
            ctx[name] = _strip(_controller().read(name)[1])
        except ControllerError as exc:
            errors.append(str(exc))
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
    gateway_mac: str = ""      # restart_device: refused when this is the gateway


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
    # Integration clients name the uplink device, not the port, so class A
    # cannot tell which port the node is on: any port of that switch is
    # refused (fail closed) until root spike 1.1 shows a port field.
    return Plan(tool, "devices", did, f"devices/{mac}#port{port}", "command", before=device, endpoint="devmgr",
                command={"cmd": "power-cycle", "mac": mac, "port_idx": port}, class_a=("devices", did)), ""


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


def _class_a_refusal(plan: Plan, ctx: dict) -> str:
    own = {POD_IP, ctx["controller_ip"], *ctx["api_server_ips"]}
    for ip in plan.refuse_ips:
        if ip in own:
            return f"{ip} is an end of im's own path (this node, the controller or an API server)"
    if plan.gateway_mac:
        for d in ctx["devices"]:
            if not isinstance(d, dict):
                continue
            mac = str(d.get("macAddress") or d.get("mac") or "").lower()
            if mac == plan.gateway_mac and (d.get("ipAddress") or d.get("ip")) in _gateway_ips(ctx["networks"], ctx["controller_ip"]):
                return f"device {plan.gateway_mac} is the gateway: restarting it takes the whole LAN down, and there is no tool for that"
    if plan.class_a is None:
        return ""
    return class_a(plan.class_a[0], plan.class_a[1], pod_ip=POD_IP, controller_ip=ctx["controller_ip"],
                   api_server_ips=ctx["api_server_ips"], networks=ctx["networks"], clients=ctx["clients"],
                   devices=ctx["devices"], rules=ctx["firewallrules"], groups=ctx["firewallgroups"]) or ""


# --- write side: audit, tickets, snapshot, apply, health, revert ----------------------------------

_AUDIT_OUT = None   # the real stdout; __main__ points sys.stdout at stderr


def _audit(tool: str, target: str, phase: str, stated: str, **fields: object) -> None:
    """One JSON line per write phase to stdout — `kubectl logs -c netdev-mcp`
    is the only record of a direct-LAN write outside the ticket."""
    line = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "tool": tool, "target": target,
            "phase": phase, "stated_reason": stated, "user": USER_ID, "snapshot_digest": "", "diff_digest": "",
            "health": [], "outbound": "", "fac_witness": ""}
    line.update(fields)
    print(json.dumps(line, ensure_ascii=False), file=_AUDIT_OUT or sys.stdout, flush=True)


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
        f"`{POST_ACK} <peer name [ref]>` after the change if you still can. Without a pre-ack within "
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
    names = [o.get("name") for o in items if isinstance(o, dict) and o.get("name")]
    return Snapshot(obj, ids, _digest(raw), path, names)


def _unchanged_since_token(plan: Plan, snap: Snapshot) -> str:
    """'' when the object is still what will_change was computed from."""
    if plan.kind == "create":
        if plan.fields.get("name") in snap.names:
            return f"a {plan.collection} object named {plan.fields['name']!r} appeared since the token; read it again"
        return ""
    if plan.kind == "put":
        now = _strip(snap.obj)
        moved = [f for f in plan.fields if now.get(f) != plan.before.get(f)]
        if moved:
            return f"{plan.target} changed on the controller since the token ({', '.join(moved)}); read it again"
    return ""


def _rest(plan: Plan) -> str:
    return WRITES["rest"].format(collection=LEGACY_NAME[plan.collection])


def _apply(plan: Plan, snap: Snapshot) -> "tuple[str, str, bool]":
    """(created _id, rc, maybe_applied). rc is 'ok' or the error; maybe_applied
    is False only when the controller itself refused the request."""
    try:
        if plan.kind == "put":
            OPERATOR.request("PUT", f"{_rest(plan)}/{plan.obj_id}", {**snap.obj, **plan.fields})
        elif plan.kind == "create":
            data = OPERATOR.request("POST", _rest(plan), dict(plan.fields))
            return (_obj_id(data[0]) if data else ""), "ok", True
        elif plan.kind == "delete":
            OPERATOR.request("DELETE", f"{_rest(plan)}/{plan.obj_id}")
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


def _health(plan: Plan, created_id: str, number: int, witness: str) -> Health:
    """Every HEALTH_INTERVAL until HEALTH_TIMEOUT: the first round in which every
    check passes ends it. A breakage that shows only after that round is not
    seen here — that is what FAC's post-ack is for."""
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
        lost = _probes_lost()
        outbound = _outbound(lost)
        (failed.append("probes: " + "; ".join(lost)) if len(lost) >= 2 else passed.append("probes"))
        seen = witness
        if witness == "armed":
            try:
                acked = TICKETS.witnessed(number, POST_ACK)
            except TicketError:
                acked = False
            seen = "ok" if acked else "lost"
            (passed.append("post-ack") if acked else failed.append("post-ack: not (yet) on the ticket"))
        if not failed:
            return Health(True, passed, failed, outbound, seen)
        if _clock() >= deadline:
            return Health(False, passed, failed, outbound, seen)
        _sleep(HEALTH_INTERVAL)


def _revert(plan: Plan, snap: Snapshot, created_id: str) -> "tuple[bool, str]":
    """Back to the snapshot, from inside the LAN. (reverted?, what happened)."""
    try:
        if plan.kind == "put":
            OPERATOR.request("PUT", f"{_rest(plan)}/{plan.obj_id}", snap.obj)
            _, items = _controller().read(plan.collection)
            now = next((o for o in items if _obj_id(o) == plan.obj_id), None) or {}
            moved = [f for f in plan.fields if now.get(f) != snap.obj.get(f)]
            return (False, f"after the revert PUT, {', '.join(moved)} still differ from the snapshot") if moved \
                else (True, f"PUT the snapshot of {plan.obj_id} back")
        if plan.kind == "create":
            _, items = _controller().read(plan.collection)
            new = sorted({_obj_id(o) for o in items} - set(snap.ids) - {""})
            if not created_id:
                return False, f"the create response returned no _id; new ids in {plan.collection}: {new}; deleting nothing"
            if new != [created_id]:
                return False, (f"new ids in {plan.collection} are {new}, not only the created {created_id}: someone "
                               "else's object must not be removed by this revert, so nothing was deleted")
            OPERATOR.request("DELETE", f"{_rest(plan)}/{created_id}")
            _, items = _controller().read(plan.collection)
            ids = sorted(i for i in (_obj_id(o) for o in items) if i)
            return (True, f"DELETEd the created {created_id}") if ids == snap.ids \
                else (False, f"after the DELETE, {plan.collection} ids are {ids}, not the snapshot's {snap.ids}")
        if plan.kind == "delete":
            body = {k: v for k, v in snap.obj.items() if k != "_id"}
            data = OPERATOR.request("POST", _rest(plan), body)
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


INBOX_NOTE = ("Write ~/workspace/inbox/{date}-netdev-revert-failed.md NOW, before anything else: the target, the "
              "will_change below, snapshot digest {digest}, what failed, and what you tried. This sidecar cannot "
              "write the inbox; you are the only way a person learns of this. Ticket #{ticket} stays open.")


def _write(tool: str, hint: str, reason: str, confirm: str, build) -> str:
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
    if last is not None and now - last < WRITE_COOLDOWN:
        left = int(WRITE_COOLDOWN - (now - last))
        return refuse(f"{target} was written {int(now - last)} s ago; cooldown is {WRITE_COOLDOWN} s, {left} s left", target)
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
            number = TICKETS.open(f"[netdev] {CLUSTER or '(CLUSTER_NAME unset)'} {tool} {target}",
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

    created_id, rc, maybe_applied = _apply(plan, snap)
    if not maybe_applied:
        why = f"the controller refused the write, nothing changed: {rc}"
        _audit(tool, target, "aborted", stated, reason=why, rc=rc, outbound="ok", fac_witness=witness, **common)
        note = _release(number, "aborted", "ok", witness, [why])
        return _respond({"result": "aborted", "target": target, "ticket": number, "reason": why,
                         **({"release_not_posted": note} if note else {})})

    # ⑥ health, then revert from inside on any failure
    h = _health(plan, created_id, number, witness)
    readings = {"outbound": h.outbound, "fac_witness": h.witness}
    _audit(tool, target, "execute", stated, rc=rc, health=h.passed, **readings, **common)
    if h.ok:
        note = _release(number, "applied", h.outbound, h.witness, ["health: " + ", ".join(h.passed),
                                                                    f"snapshot_digest: {snap.digest}"])
        body = {"result": "applied", "target": target, "ticket": number, **readings, "health": h.passed,
                "snapshot_digest": snap.digest}
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
    的任何 port 都拒絕（controller 沒說節點在哪個 port）。無法還原。兩段式。"""
    return _write("power_cycle_port", f"devices/{switch_mac}#port{port}", reason, confirm,
                  lambda: _plan_device("power_cycle_port", switch_mac, port))


WRITE_TOOLS = [set_dhcp_reservation, clear_dhcp_reservation, block_client, unblock_client,
               set_port_forward, delete_port_forward, restart_device, power_cycle_port]

# set_dns_forward is not here: whether UniFi exposes conditional forwarding
# through an API at all is root spike 1.1's reading (sub-change 1.2). No
# reading, no tool.

# Registered only when every write input is usable: there is no "present but
# refuses for lack of configuration" state.
if not WRITE_BLOCKERS:
    for _fn in WRITE_TOOLS:
        app.tool()(_fn)


# --- startup readings ------------------------------------------------------------------

def _startup_readings() -> list[str]:
    readings = []
    if URL_REFUSAL:
        readings.append(URL_REFUSAL)
    elif CONTROLLER_URL.startswith("http://"):
        readings.append("insecure http accepted (test only): NETDEV_ALLOW_INSECURE_HTTP=1")
    readings.append("controller: " + (urlsplit(CONTROLLER_URL).netloc or "(unset)") + f", site {SITE}")
    readings.append("reader credential: " + ("ok" if not MISSING_CONFIG else "missing " + ", ".join(MISSING_CONFIG)))
    readings.append(f"operator credential: {OPERATOR_STATE}")
    readings.append("ticket channel: " + (f"{TICKET_REPO} via {urlsplit(TICKET_API).netloc}" if not TICKET_REFUSAL
                                          else "missing (" + TICKET_REFUSAL + ")"))
    readings.append("outside probes: " + (", ".join(p.raw for p in PROBES) if not PROBE_REFUSAL else PROBE_REFUSAL))
    readings.append(f"POD_IP: {POD_IP or '(unset)'}")
    readings.append("snapshot dir: " + (_snapshot_dir_refusal() or f"{SNAPSHOT_DIR} (keep {SNAPSHOT_KEEP})"))
    if WRITE_BLOCKERS:
        readings.append("write tools: not registered — " + "; ".join(WRITE_BLOCKERS))
    else:
        readings.append("write tools: registered — " + ", ".join(fn.__name__ for fn in WRITE_TOOLS)
                        + "; set_dns_forward not shipped (root spike 1.1)")
    readings.append("routes: provisional, not measured (root spike 1.1) — "
                    + ", ".join(f"{c}={api}" for c, (api, _) in sorted(ROUTES.items())) + "; writes=legacy")
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
            version = info.get("applicationVersion") if isinstance(info, dict) else None
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
    for reading in _startup_readings():
        print(f"netdev-mcp: {reading}", file=sys.stderr, flush=True)
    app.run(transport="sse")
