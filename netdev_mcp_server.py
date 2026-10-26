#!/usr/bin/env python3
"""MCP Netdev Server — the customer's UniFi controller, for im.

Runs as its own sidecar container (`netdev-mcp`, jg-base#222), isolated from
the `app` container the terminal user/agent shell runs in, the same split as
talos-mcp: the UniFi credentials are mounted only here. The root change is
fleet-ops `openspec/changes/im-unifi-management`; this server's contract is
k8scc `openspec/changes/im-netdev-mcp` (spec `netdev-mcp-server`).

Start: python3 netdev_mcp_server.py  (HTTP/SSE transport, binds 127.0.0.1 only)

THIS IMAGE IS THE READER-ONLY RELEASE (root Migration 2). The ten read tools
are here; the write tools, the inward dead-man and the ticket channel arrive
in a later image. The operator credential and the ticket channel are not read
by this build at all, and the startup readings say so — there is no state in
which a write tool is registered but refuses every call.

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

Which API serves which collection is data, not code: ROUTES below. Its values
are PROVISIONAL until root spike 1.1 reads FAC1's real console; every response
says which API answered (`[via integration]` / `[via legacy]`), so a route the
spike moves shows up in the output, not only in this file.
"""
import ipaddress
import json
import logging
import os
import re
import sys
import time
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

CACHE_TTL = 600   # seconds a reader result can address a write (spec: session cache)

# httpx logs every request at INFO; the startup readings and the tool
# responses already say what was asked, and a request line per call drowns
# them in `kubectl logs -c netdev-mcp`. Warnings and errors still show.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Patch point for the controls in test/ (cache expiry without sleeping).
_now = time.time

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
    pass


class Controller:
    """One HTTP client, two API shapes. Only the reader key is used by this
    image; it is sent as `X-API-Key` on both shapes. Whether the legacy paths
    accept it is root spike 1.1's reading — if they do not, the answer is a
    401 naming the route, not a silent empty list."""

    def __init__(self):
        verify = CA_FILE if CONTROLLER_URL.startswith("https://") else True
        try:
            self._client = httpx.Client(base_url=CONTROLLER_URL, verify=verify, timeout=READ_TIMEOUT,
                                        headers={"X-API-Key": _READER_KEY, "Accept": "application/json"},
                                        follow_redirects=False)
        except (OSError, ValueError) as exc:   # ssl.SSLError is an OSError
            raise ControllerError(f"controller CA {CA_FILE} (UNIFI_CA_FILE) cannot be loaded: {exc}") from None
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
        body = self._get(path)
        if not isinstance(body, dict) or (body.get("meta") or {}).get("rc") != "ok" or not isinstance(body.get("data"), list):
            msg = (body.get("meta") or {}).get("msg") if isinstance(body, dict) else None
            raise ControllerError(f"GET {path}: legacy API did not answer rc=ok ({msg or 'no meta'})")
        return api, body["data"]


_CONTROLLER: "Controller | None" = None


def _controller() -> Controller:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = Controller()
    return _CONTROLLER


# --- session cache: what a write may address (a later image writes) ---------------

_SEEN: dict[str, dict[str, tuple[dict, float]]] = {}   # collection -> id -> (stripped obj, ts)


def _remember(collection: str, items) -> None:
    now = _now()
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
    if shape is not None:
        clean = shape(clean)
    _remember(collection, clean)
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
                                        ("firewall rules", rules), ("firewall groups", groups)) if value is None]
    if not pod_ip:
        missing.append("POD_IP")
    if missing:
        return "reader data unavailable (" + ", ".join(missing) + "), refusing (fail closed)"
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


# --- startup readings ------------------------------------------------------------------

def _startup_readings() -> list[str]:
    readings = []
    if URL_REFUSAL:
        readings.append(URL_REFUSAL)
    elif CONTROLLER_URL.startswith("http://"):
        readings.append("insecure http accepted (test only): NETDEV_ALLOW_INSECURE_HTTP=1")
    readings.append("controller: " + (urlsplit(CONTROLLER_URL).netloc or "(unset)") + f", site {SITE}")
    readings.append("reader credential: " + ("ok" if not MISSING_CONFIG else "missing " + ", ".join(MISSING_CONFIG)))
    readings.append("operator credential: not read by this image (reader-only release, root Migration 2)")
    readings.append("write tools: not registered (reader-only release)")
    readings.append("routes: provisional, not measured (root spike 1.1) — "
                    + ", ".join(f"{c}={api}" for c, (api, _) in sorted(ROUTES.items())))
    if not _not_ready():
        try:
            _, info = _controller().read("info")
            version = info.get("applicationVersion") if isinstance(info, dict) else None
            readings.append(f"controller version: {version or 'not reported'}")
        except ControllerError as exc:
            readings.append(f"controller version: unreadable ({exc})")
    return readings


if __name__ == "__main__":
    for reading in _startup_readings():
        print(f"netdev-mcp: {reading}", file=sys.stderr, flush=True)
    app.run(transport="sse")
