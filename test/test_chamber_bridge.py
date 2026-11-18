#!/usr/bin/env python3
"""Controls for chamber-bridge (openspec im-chamber-client tasks 3.1 and 3.2,
k8scc#70). The bridge runs as shipped, as a real process, against a fake Matrix
server on a local TCP port; the real `chamber` client talks to it over the
Unix socket.

The fake server is hand-written from the paths the client uses. It is not a
Matrix server: what these cells measure is the bridge, not the protocol.

Every negative cell has a positive control that shares the property under
test. A listing tool that is missing is "cannot measure" and fails the run.

Run: python3 test/test_chamber_bridge.py   (from the repo root; also runs in CI)
Exit 0 all matched, 1 mismatch or cannot measure.
"""
import http.client, http.server, json, os, re, shutil, socket, socketserver, stat, subprocess, sys, tempfile, threading, time, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "chamber", "chamber-bridge")
CLIENT = os.path.join(ROOT, "chamber", "chamber")
T = tempfile.mkdtemp(prefix="chb", dir="/tmp")     # short: AF_UNIX paths are limited to about 100 bytes
CRED = "the-credential-0123456789"
SECRET_TEXT = "the words of a message nobody may log"
SENDER = "@fac-agent:fake"
P = "/_matrix/client/v3"
res = []


def check(name, expect, got):
    ok = expect == got
    res.append(ok)
    print("%s %-78s expect %r  got %r" % ("ok  " if ok else "FAIL", name, expect, got))


class Fake:
    """State of the fake server; one instance per listening port."""
    def __init__(self):
        self.seen = []                 # (method, path?query, Authorization)
        self.user = "@im-test:fake"
        self.batches = []              # sync answers to hand out, in order
        self.n = 0
        self.redirect_to = None
        self.presence = "offline"
        self.lock = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    def reply(self, code, body, headers=()):
        raw = json.dumps(body).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def any(self):
        f = self.server.fake
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with f.lock:
            f.seen.append((self.command, self.path, self.headers.get("Authorization")))
        path, _, query = self.path.partition("?")
        if f.redirect_to and path == P + "/joined_rooms":
            return self.reply(302, {}, [("Location", f.redirect_to + path)])
        if self.command == "GET" and path == P + "/account/whoami":
            return self.reply(200, {"user_id": f.user})
        if self.command == "GET" and path == P + "/joined_rooms":
            return self.reply(200, {"joined_rooms": ["!r1:fake"]})
        if self.command == "GET" and path.endswith("/state/m.room.name"):
            return self.reply(200, {"name": "room one"})
        if self.command == "GET" and path.endswith("/joined_members"):
            return self.reply(200, {"joined": {f.user: {}, SENDER: {}}})
        if self.command == "GET" and "/presence/" in path:
            return self.reply(200, {"presence": f.presence})
        if self.command == "GET" and path == P + "/sync":
            q = dict(urllib.parse.parse_qsl(query))
            with f.lock:
                batch = f.batches.pop(0) if (f.batches and "since" in q) else None
                f.n += 1
                n_ = f.n
            if batch is None:
                if "since" in q:
                    time.sleep(0.15)        # an idle long poll, shortened
                return self.reply(200, {"next_batch": "s%d" % n_})
            return self.reply(200, {"next_batch": "s%d" % n_, "rooms": {"join": {"!r1:fake": {"timeline": {"events": batch}}}}})
        if self.command == "PUT" and "/send/m.room.message/" in path:
            return self.reply(200, {"event_id": "$sent"})
        return self.reply(418, {"errcode": "FAKE_REACHED", "error": "the fake was reached on a path it does not serve"})

    do_GET = do_PUT = do_POST = do_DELETE = any

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):               # no reverse lookup (slow on some laptops)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def fake_server():
    s = S(("127.0.0.1", 0), H)
    s.fake = Fake()
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, s.fake, "http://127.0.0.1:%d" % s.server_address[1]


srv, fake, FAKE_URL = fake_server()
other, other_fake, OTHER_URL = fake_server()
open(os.path.join(T, "tok"), "w").write(CRED + "\n")
procs = []


def start(name, **over):
    """Start the bridge as shipped. -> (process, socket path, log path)."""
    d = os.path.join(T, name)
    os.makedirs(d, exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": T, "CHAMBER_URL": FAKE_URL, "CHAMBER_ACCOUNT": "im-test",
           "CHAMBER_TOKEN_FILE": os.path.join(T, "tok"), "CHAMBER_SOCKET": os.path.join(d, "b.sock"),
           "CHAMBER_BEAT": "0.3", "CHAMBER_ALLOW_INSECURE_HTTP": "1"}
    env.update(over)
    env = {k: v for k, v in env.items() if v is not None}
    logp = os.path.join(d, "log")
    p = subprocess.Popen([sys.executable, "-I", BRIDGE], env=env, stdout=open(logp, "a"), stderr=subprocess.STDOUT, cwd=T)
    procs.append(p)
    return p, env["CHAMBER_SOCKET"], logp


def logged(logp):
    return open(logp).read()


def until(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def stop(p):
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
    return p.returncode


def raw(sock, method, path, headers=None, body=None):
    """One request straight to the socket. -> (status, body bytes, all header text) or (0, b'', '')."""
    c = http.client.HTTPConnection("localhost", timeout=10)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.sock.settimeout(10)
        c.sock.connect(sock)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(), str(r.headers)
    except Exception:
        return 0, b"", ""
    finally:
        c.close()


def client(sock, *args, stdin=None):
    env = {"PATH": os.environ["PATH"], "HOME": T, "CHAMBER_URL": "unix:" + sock, "CHAMBER_ACCOUNT": "im-test"}
    r = subprocess.run([sys.executable, "-I", CLIENT, *args], capture_output=True, env=env, cwd=T, timeout=60, input=stdin)
    return r.returncode, r.stdout.decode().strip(), r.stderr.decode()


def count(method=None, path_part=""):
    with fake.lock:
        return sum(1 for m, p, a in fake.seen if (method is None or m == method) and path_part in p)


# ---------------------------------------------------------------- start states
print("start states (nothing is opened unless everything is there)")
for name, over, word in (
        ("s-unset", {"CHAMBER_URL": None}, "not configured"),
        ("s-empty", {"CHAMBER_URL": ""}, "not configured"),
        ("s-notok", {"CHAMBER_TOKEN_FILE": os.path.join(T, "absent-token")}, "absent-token is absent or unreadable"),
        ("s-emptytok", {"CHAMBER_TOKEN_FILE": os.devnull}, "is empty"),
        ("s-noacct", {"CHAMBER_ACCOUNT": None}, "CHAMBER_ACCOUNT is not set"),
        ("s-mode", {"CHAMBER_MODE": "loud"}, "CHAMBER_MODE must be observe or deliver"),
        ("s-http", {"CHAMBER_ALLOW_INSECURE_HTTP": None}, "CHAMBER_URL must be https://"),
        ("s-nodir", {"CHAMBER_SOCKET": os.path.join(T, "no-such-dir", "b.sock")}, "is absent or not writable")):
    before = count()
    p, sock, logp = start(name, **over)
    said = until(lambda: word in logged(logp) and "mode=unconfigured" in logged(logp))
    time.sleep(0.4)
    check("S %-10s says why, stays alive, opens no socket, asks the server nothing" % name,
          (True, None, False, 0), (said, p.poll(), os.path.exists(sock), count() - before))
    check("S %-10s ends on SIGTERM" % name, 0, stop(p))

# ---------------------------------------------------------------- observe mode
print("observe mode")
p, SOCK, LOG = start("observe")
up = until(lambda: os.path.exists(SOCK) and "the server confirms the account" in logged(LOG))
check("B1 started: says the mode and where the credential is", (True, True, True),
      (up, "mode=observe" in logged(LOG), "the credential is in this container only" in logged(LOG)))
st = os.lstat(SOCK)
check("B2 the socket is a socket, mode 0600, owned by the bridge's user", (True, 0o600, os.getuid()),
      (stat.S_ISSOCK(st.st_mode), stat.S_IMODE(st.st_mode), st.st_uid))
check("B3 the real client through the socket: whoami", (0, "@im-test:fake"), client(SOCK, "whoami")[:2])
check("B4 the real client through the socket: rooms", (0, True), (lambda r: (r[0], "room one" in r[1]))(client(SOCK, "rooms")))
with fake.lock:
    auths = {a for m, pth, a in fake.seen}
check("B5 every request the server saw carries the bridge's credential", {"Bearer " + CRED}, auths)
n0 = len(fake.seen)
s_, b_, h_ = raw(SOCK, "GET", P + "/account/whoami", {"Authorization": "Bearer from-the-caller"})
with fake.lock:
    new = [a for m, pth, a in fake.seen[n0:] if pth == P + "/account/whoami"]
check("B6 a caller's own Authorization header is not passed on", (200, True, False), (s_, "Bearer " + CRED in new, "Bearer from-the-caller" in new))

print("paths outside the list (the server must see none of them)")
outside = [("GET", "/_synapse/admin/v1/users"), ("POST", P + "/account/password"), ("POST", P + "/logout"),
           ("POST", P + "/createRoom"), ("POST", P + "/rooms/!r1:fake/invite"), ("POST", P + "/rooms/!r1:fake/leave"),
           ("PUT", P + "/rooms/!r1:fake/state/m.room.power_levels"), ("PUT", P + "/rooms/!r1:fake/send/m.room.redaction/1"),
           ("PUT", P + "/presence/@im-test:fake/status"), ("DELETE", P + "/account/whoami"), ("POST", P + "/account/whoami"),
           ("GET", P + "/rooms/!r1:fake/event/../../../account/3pid"), ("GET", P + "/account/whoami/"),
           ("GET", P + "//account/whoami"), ("GET", "/_matrix/client/v3/login"), ("GET", "/")]
before = count()
got = [raw(SOCK, m, pth, body=b"{}" if m != "GET" else None)[0] for m, pth in outside]
check("P1 %d paths outside the list are all answered 404 by the bridge" % len(outside), [404] * len(outside), got)
check("P2 the server saw none of them", 0, count() - before)
before = count("GET", "/joined_rooms")
s_ = raw(SOCK, "GET", P + "/joined_rooms")[0]
check("P3 control: a listed path is forwarded (the server sees it)", (200, 1), (s_, count("GET", "/joined_rooms") - before))


def direct(server, method, path):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    c.request(method, path, body=b"{}" if method != "GET" else None)
    return c.getresponse().status


before = count("POST", "/createRoom")
check("P4 control: asked directly, the fake is reached on an unlisted path and records it", (418, 1),
      (direct(srv, "POST", P + "/createRoom"), count("POST", "/createRoom") - before))

print("observe: receives and counts, refuses to send, never asks to be online")
before_put = count("PUT")
rc, o, e = client(SOCK, "send", "room one", "--to", "fac-agent", "hello")
check("O1 chamber send is refused (1), and the server saw no PUT", (1, 0), (rc, count("PUT") - before_put))
raw(SOCK, "GET", P + "/sync?timeout=0&set_presence=online")
# the bridge's own syncs must be in the list too, or this cell reads only the request above
until(lambda: sum(1 for m, pth, a in fake.seen if pth.startswith(P + "/sync")) >= 3)
with fake.lock:
    syncs = [pth for m, pth, a in fake.seen if pth.startswith(P + "/sync")]
check("O2 every sync the server saw asks for presence offline, none for online", (True, True, False),
      (len(syncs) >= 3, all("set_presence=offline" in s for s in syncs), any("set_presence=online" in s for s in syncs)))
ev = lambda to, sender=SENDER: {"type": "m.room.message", "sender": sender, "event_id": "$e", "origin_server_ts": 1,
                               "content": {"msgtype": "m.text", "body": SECRET_TEXT, "m.mentions": to}}
with fake.lock:
    fake.batches.append([ev({"user_ids": ["@im-test:fake"]}), ev({"room": True}), ev({"user_ids": ["@someone:fake"]}),
                         ev({"room": True}, sender="@im-test:fake")])
check("O3 counts per room: two for the im, one for someone else, its own not counted", True,
      until(lambda: "received room=room_one for-me=2 other=1" in logged(LOG)))
check("O4 the pod log has neither the message text nor the sender", (False, False), (SECRET_TEXT in logged(LOG), SENDER in logged(LOG)))
line = [l for l in logged(LOG).splitlines() if l.startswith("chamber-status ")][-1]
check("O5 the status line: every field, in order", True,
      bool(re.fullmatch(r"chamber-status ts=\d+ interval=0\.3 mode=observe server=ok rooms=room_one online=no", line)))
with fake.lock:
    fake.presence = "online"
check("O6 control: online is a reading — it follows what the server says", True,
      until(lambda: logged(LOG).splitlines()[-1].endswith("online=yes") or any(l.endswith("online=yes") for l in logged(LOG).splitlines()[-3:])))
with fake.lock:
    fake.presence = "offline"

print("no TCP listener")
def listeners(pid):
    """-> the set of TCP ports this pid listens on, or None when it cannot be measured."""
    if shutil.which("lsof"):
        r = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
        return {l.split()[-2].rsplit(":", 1)[-1] for l in r.stdout.splitlines()[1:]}
    if shutil.which("ss"):
        r = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True)
        if r.returncode:
            return None
        return {l.split()[3].rsplit(":", 1)[-1] for l in r.stdout.splitlines() if "pid=%d," % pid in l}
    return None
mine, theirs = listeners(os.getpid()), listeners(p.pid)
if mine is None or theirs is None:
    res.append(False)
    print("FAIL CANNOT MEASURE: neither lsof nor ss could list listening sockets")
else:
    check("L1 control: the same listing shows the fake server's port in this process", True, str(srv.server_address[1]) in mine)
    check("L2 the bridge listens on no TCP port", set(), theirs)

print("the credential stays in the bridge")
answers = b"".join(x[1] + x[2].encode() for x in (raw(SOCK, "GET", P + "/account/whoami"), raw(SOCK, "GET", "/nothing"),
                                                    raw(SOCK, "PUT", P + "/rooms/!r1:fake/send/m.room.message/1", body=b"{}")))
check("K1 the credential is in no answer", False, CRED.encode() in answers)
check("K2 the credential is not in the pod log", False, CRED in logged(LOG))
check("K3 control: the server did receive it", True, any(a and CRED in a for m, pth, a in fake.seen))

print("a redirect is not followed")
with fake.lock:
    fake.redirect_to = OTHER_URL
s_ = raw(SOCK, "GET", P + "/joined_rooms")[0]
time.sleep(0.5)
with fake.lock:
    fake.redirect_to = None
check("R1 the redirect comes back as it is, and the other server was asked nothing", (302, 0), (s_, len(other_fake.seen)))
check("R2 control: the other server answers when asked directly", 200, direct(other, "GET", P + "/account/whoami"))

print("the sync position survives a restart")
pos = os.path.join(os.path.dirname(SOCK), "sync-position")
until(lambda: os.path.exists(pos), 3)
saved = open(pos).read() if os.path.exists(pos) else ""
check("N1 a position is saved, mode 0600", (True, 0o600), (saved.startswith("s"), stat.S_IMODE(os.stat(pos).st_mode) if os.path.exists(pos) else None))
stop(p)
check("N2 the socket is gone after the bridge ends", False, os.path.exists(SOCK))
with fake.lock:
    mark = len(fake.seen)
p, SOCK, LOG = start("observe")
until(lambda: any(pth.startswith(P + "/sync") for m, pth, a in fake.seen[mark:]))
with fake.lock:
    first = [pth for m, pth, a in fake.seen[mark:] if pth.startswith(P + "/sync")][0]
check("N3 after a restart the first sync continues from the saved position", True, "since=" in first)
stop(p)
with fake.lock:
    mark = len(fake.seen)
p2, sock2, log2 = start("fresh")
until(lambda: any(pth.startswith(P + "/sync") for m, pth, a in fake.seen[mark:]))
with fake.lock:
    first = [pth for m, pth, a in fake.seen[mark:] if pth.startswith(P + "/sync")][0]
check("N4 control: with no saved position the first sync has no since", (False, True), ("since=" in first, until(lambda: "receiving from now" in logged(log2))))
stop(p2)

# ---------------------------------------------------------------- deliver mode
print("deliver mode")
p, SOCK, LOG = start("deliver", CHAMBER_MODE="deliver", CHAMBER_BRIDGE_IN_APP="1")
until(lambda: os.path.exists(SOCK) and "the server confirms the account" in logged(LOG))
check("D1 the start line says the credential is inside app when told so", True, "the credential is inside app (no sidecar)" in logged(LOG))
before_put = count("PUT")
rc, o, e = client(SOCK, "send", "room one", "--to", "fac-agent", "hello")
check("D2 chamber send goes through (3: left in the room, the fake says the recipient is offline)", (3, 1), (rc, count("PUT") - before_put))
with fake.lock:
    syncs = [pth for m, pth, a in fake.seen if pth.startswith(P + "/sync")]
check("D3 deliver without an alive signal still asks for presence offline", True, all("set_presence=offline" in s for s in syncs))
def announce_only(sock, length):
    """Send the request line and a Content-Length, no body. -> the status line's code."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(sock)
    c.sendall(("PUT %s/rooms/!r1:fake/send/m.room.message/2 HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (P, length)).encode())
    try:
        return int(c.recv(64).split()[1])
    except Exception:
        return 0
    finally:
        c.close()


before_put = count("PUT")
check("D4 a send announcing a body over the limit is refused here, and the server sees no PUT", (413, 0),
      (announce_only(SOCK, (1 << 20) + 1), count("PUT") - before_put))
before_put = count("PUT")
check("D5 control: a send within the limit reaches the server", (200, 1),
      (raw(SOCK, "PUT", P + "/rooms/!r1:fake/send/m.room.message/3", body=b"{}")[0], count("PUT") - before_put))

print("server unreachable: the client says cannot measure, not refused")
check("U1 control: reachable ⇒ 0", 0, client(SOCK, "whoami")[0])
stop(p)
dead = socket.socket()
dead.bind(("127.0.0.1", 0))                 # a port with nothing listening
dead_url = "http://127.0.0.1:%d" % dead.getsockname()[1]
dead.close()
p, SOCK, LOG = start("down", CHAMBER_URL=dead_url)
until(lambda: os.path.exists(SOCK) and "server=unreachable" in logged(LOG))
check("U2 the status line says unreachable, the rest unmeasured", True,
      any(l.endswith("mode=observe server=unreachable rooms=unmeasured online=unmeasured") for l in logged(LOG).splitlines()))
rc, o, e = client(SOCK, "whoami")
check("U3 the client through the bridge ⇒ 2", (2, True), (rc, "cannot reach the server" in e))
stop(p)
gone, gone_fake, GONE_URL = fake_server()
p, SOCK, LOG = start("gone", CHAMBER_URL=GONE_URL)
until(lambda: os.path.exists(SOCK) and "the server confirms the account" in logged(LOG))
check("U4 control: confirmed and reachable ⇒ 0", 0, client(SOCK, "whoami")[0])
gone.shutdown()
gone.server_close()
rc, o, e = client(SOCK, "whoami")
check("U5 the server goes away after the account was confirmed ⇒ 2, not 1", (2, True), (rc, "cannot reach the server" in e))
stop(p)

print("the credential is someone else's")
with fake.lock:
    fake.user = "@another:fake"
p, SOCK, LOG = start("wrong")
until(lambda: "forwarding is off" in logged(LOG))
before = count("GET", "/joined_rooms")
check("W1 says so, forwards nothing, the status line says refused", (True, 0, True),
      ("belongs to another account" in logged(LOG), raw(SOCK, "GET", P + "/joined_rooms")[0],
       until(lambda: "server=refused rooms=unmeasured online=unmeasured" in logged(LOG))))
check("W2 the server was not asked for the rooms", 0, count("GET", "/joined_rooms") - before)
stop(p)
with fake.lock:
    fake.user = "@im-test:fake"

for q in procs:
    if q.poll() is None:
        q.kill()
srv.shutdown()
other.shutdown()
shutil.rmtree(T, ignore_errors=True)
print("\nchamber-bridge controls: %d passed, %d failed" % (res.count(True), res.count(False)))
sys.exit(0 if all(res) else 1)
