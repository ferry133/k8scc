#!/usr/bin/env python3
"""Controls for what this repo added to the two chamber tools (openspec
im-chamber-client tasks 2.3 and 2.5, k8scc#70):

  chamber           CHAMBER_URL=unix:<path> speaks to a Unix socket and carries
                    no credential
  directive-verify  CHAMBER_SIGNERS_DIR names the signer directory;
                    CHAMBER_TRANSCRIPTS=off answers 2 to every pointer without
                    looking for a file

The fake server answers the same two paths over TCP and over a Unix socket and
records the headers it was sent. Every negative cell has a positive control
that shares the property under test. A missing ssh-keygen is "cannot measure"
and fails the run.

Run: python3 test/test_chamber_tools.py   (from the repo root; also runs in CI)
Exit 0 all matched, 1 mismatch or cannot measure.
"""
import http.server, json, os, shutil, socket, socketserver, subprocess, sys, tempfile, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "chamber", "chamber")
VERIFY = os.path.join(ROOT, "chamber", "directive-verify")
VEC = os.path.join(ROOT, "test", "chamber-vectors", "sk-ecdsa")
# a short path: AF_UNIX paths are limited to about 100 bytes
T = tempfile.mkdtemp(prefix="cht", dir="/tmp")
res = []


def check(name, expect, got):
    ok = expect == got
    res.append(ok)
    print("%s %-74s expect %r  got %r" % ("ok  " if ok else "FAIL", name, expect, got))


class Handler(http.server.BaseHTTPRequestHandler):
    seen = []          # (transport, path, Authorization header or None)
    transport = "?"

    def do_GET(self):
        Handler.seen.append((self.server.transport, self.path, self.headers.get("Authorization")))
        if self.path == "/_matrix/client/v3/account/whoami":
            code, body = 200, {"user_id": "@t:fake"}
        elif self.path == "/_matrix/client/v3/joined_rooms":
            code, body = 403, {"errcode": "M_FORBIDDEN", "error": "no"}
        else:
            code, body = 404, {}
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def address_string(self):   # a Unix peer has no (host, port)
        return "peer"

    def log_message(self, *a):
        pass


class TCP(socketserver.TCPServer):
    transport = "tcp"
    allow_reuse_address = True


class Unix(socketserver.UnixStreamServer):
    transport = "unix"


tcp = TCP(("127.0.0.1", 0), Handler)
SOCK = os.path.join(T, "b.sock")
ux = Unix(SOCK, Handler)
for s in (tcp, ux):
    threading.Thread(target=s.serve_forever, daemon=True).start()
TCP_URL = "http://127.0.0.1:%d" % tcp.server_address[1]
open(os.path.join(T, "tok"), "w").write("the-credential\n")


def client(url, *args, tokfile=os.path.join(T, "tok")):
    env = {"PATH": os.environ["PATH"], "HOME": T, "CHAMBER_URL": url, "CHAMBER_ACCOUNT": "t", "CHAMBER_TOKEN_FILE": tokfile}
    r = subprocess.run([sys.executable, "-I", CLIENT, *args], capture_output=True, env=env, cwd=T, timeout=60)
    return r.returncode, r.stdout.decode().strip(), r.stderr.decode()


def auth(transport):
    return [a for t, p, a in Handler.seen if t == transport]


print("chamber: the socket address (tasks 2.5)")
check("C1 over TCP: whoami (positive control)", (0, "@t:fake"), client(TCP_URL, "whoami")[:2])
check("C2 over TCP: the credential is sent", ["Bearer the-credential"], auth("tcp"))
check("C3 over the socket: whoami, the same answer", (0, "@t:fake"), client("unix:" + SOCK, "whoami")[:2])
check("C4 over the socket: no Authorization header", [None], auth("unix"))
missing = os.path.join(T, "no-such-token")
check("C5a over TCP: an unreadable credential file fails (control)", 1, client(TCP_URL, "whoami", tokfile=missing)[0])
check("C5b over the socket: the credential file is not read", 0, client("unix:" + SOCK, "whoami", tokfile=missing)[0])
check("C6a over TCP: a refusal is 1", 1, client(TCP_URL, "rooms")[0])
check("C6b over the socket: a refusal is 1", 1, client("unix:" + SOCK, "rooms")[0])
rc, o, e = client("unix:" + os.path.join(T, "absent.sock"), "whoami")
check("C7 no socket at that path: cannot measure, not failed", (2, True), (rc, "cannot reach the server" in e))
check("C8 unix: with no path is refused", 1, client("unix:", "whoami")[0])
check("C9 the credential is in no output", False, any("the-credential" in x for x in client(TCP_URL, "whoami")[1:] + client("unix:" + SOCK, "whoami")[1:]))


def verify(*args, env=None, stdin=None):
    e = {"PATH": os.environ["PATH"], "HOME": T}
    e.update(env or {})
    r = subprocess.run([sys.executable, "-I", VERIFY, *args], input=stdin, capture_output=True, env=e, cwd=T, timeout=60)
    return r.returncode, r.stdout, r.stderr.decode()


print("directive-verify: the signer directory (tasks 2.3)")
if not shutil.which("ssh-keygen"):
    res.append(False)
    print("FAIL CANNOT MEASURE: no ssh-keygen on this machine; the signer-directory cells did not run")
else:
    D = os.path.join(VEC, "directive.txt")
    check("V1 CHAMBER_SIGNERS_DIR names the directory", 0, verify(D, env={"CHAMBER_SIGNERS_DIR": VEC})[0])
    check("V2 unset: the home default, which is absent here (control)", 2, verify(D)[0])
    empty = os.path.join(T, "emptydir")
    os.mkdir(empty)
    check("V3 a directory without a list: cannot measure", 2, verify(D, env={"CHAMBER_SIGNERS_DIR": empty})[0])
    check("V4 --signers wins over the variable", 0, verify("--signers", os.path.join(VEC, "allowed_signers"), D, env={"CHAMBER_SIGNERS_DIR": empty})[0])
    check("V5 an empty variable is the same as unset", 2, verify(D, env={"CHAMBER_SIGNERS_DIR": ""})[0])

print("directive-verify: transcript pointers switched off (tasks 2.3)")
SES, REC = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TR = os.path.join(T, "tr")
os.makedirs(os.path.join(TR, "proj"))
open(os.path.join(TR, "proj", SES + ".jsonl"), "w").write(json.dumps({
    "uuid": REC, "type": "user", "origin": {"kind": "human"}, "promptSource": "typed",
    "timestamp": "2026-11-19T00:00:00Z", "message": {"role": "user", "content": "restart the test pod"}}) + "\n")
PTR = "transcript:%s/%s" % (SES, REC)
base = ("--transcripts", TR, "--ledger", os.path.join(T, "ledger"))
rc, o, e = verify(*base, PTR)
check("T1 the pointer passes when the switch is absent (positive control)", (0, b"restart the test pod"), (rc, o))
rc, o, e = verify(*base, PTR, env={"CHAMBER_TRANSCRIPTS": "off"})
check("T2 off: the same pointer is cannot-measure, no words", (2, b"", True), (rc, o, "not read on this host" in e))
rc, o, e = verify(*base, "-", env={"CHAMBER_TRANSCRIPTS": "off"}, stdin=("please do it\n%s\n" % PTR).encode())
check("T3 off: a pointer inside a message, the same", (2, b""), (rc, o))
check("T4 off is not case-sensitive", 2, verify(*base, PTR, env={"CHAMBER_TRANSCRIPTS": "OFF"})[0])
check("T5 another value does not switch it off (control)", 0, verify(*base, PTR, env={"CHAMBER_TRANSCRIPTS": "on"})[0])
rc, o, e = verify("--transcripts", os.path.join(T, "nowhere"), PTR, env={"CHAMBER_TRANSCRIPTS": "off"})
check("T6 off: it says it did not look, not that it found nothing", (2, True, False), (rc, "not read on this host" in e, "no transcript" in e))
rc, o, e = verify("--transcripts", os.path.join(T, "nowhere"), PTR)
check("T7 without the switch the same call says it found nothing (control)", (2, True), (rc, "no transcript" in e))
check("T8 --use with the switch off records nothing", (2, False), (verify(*base, "--use", PTR, env={"CHAMBER_TRANSCRIPTS": "off"})[0], os.path.exists(os.path.join(T, "ledger"))))

print("image defaults")
df = open(os.path.join(ROOT, "Dockerfile")).read()
check("I1 the image sets the signer directory", True, "ENV CHAMBER_SIGNERS_DIR=/etc/chamber/signers \\\n    CHAMBER_TRANSCRIPTS=off\n" in df)
check("I2 control: a value nobody set", False, "CHAMBER_TRANSCRIPTS=on\n" in df)

for s in (tcp, ux):
    s.shutdown()
shutil.rmtree(T, ignore_errors=True)
print("\nchamber tool controls: %d passed, %d failed" % (res.count(True), res.count(False)))
sys.exit(0 if all(res) else 1)
