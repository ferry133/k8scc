#!/usr/bin/env python3
"""Controls: chamber/chamber against a real Matrix server (openspec
im-chamber-client tasks 2.2, k8scc#70).

Every other cell of the client in this repo runs against a hand-written fake
(test/test_chamber_tools.py), and a fake answers what its author thought the
server answers. These are the operator side's cells (chamber-bench
`chamber-test.py`, 2026-11-15), ported: a throwaway Synapse is generated,
started on a loopback port and filled at test time (five accounts, three
rooms), and the client as shipped is run against it. Nothing is kept.

Three outcomes:
  0  every cell matched
  1  a cell did not match
  2  CANNOT MEASURE: no Synapse to start (`SYNAPSE_PYTHON`, or the running
     interpreter, must be able to `import synapse`), or it did not come up, or
     the accounts and rooms could not be made. CI counts that as red: a run
     that measured nothing is not a pass.

What the port changed: the account names (no person's or cluster's name: this
repo is public), the server is made here instead of by `mx-bench.sh`, and it
has no guard module (that module is the production server's own rule about
who may invite; these cells are about the client).

Not covered here: https (the server listens on plain http on loopback), the
bridge's socket form against a real server, `members`, `--unanswered`.
"""

import hashlib
import hmac
import json
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CLIENT = os.environ.get("CHAMBER_BIN") or str(HERE.parent / "chamber" / "chamber")   # CHAMBER_BIN: the mutation runner
SYN_PY = os.environ.get("SYNAPSE_PYTHON") or sys.executable
PERSON, FO, PEER, IMA, IMB = "person", "fo", "peer", "ima", "imb"
ROOMS = {"fac": [FO, PEER], "cust-a": [FO, IMA], "cust-b": [FO, IMB]}


def cannot(why, detail=""):
    print(f"CANNOT MEASURE: {why}")
    if detail:
        print(detail[-3000:])
    sys.exit(2)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self):
        probe = subprocess.run([SYN_PY, "-c", "import synapse, yaml"], capture_output=True, text=True)
        if probe.returncode != 0:
            cannot(f"{SYN_PY} cannot import synapse (set SYNAPSE_PYTHON to an interpreter that has matrix-synapse)",
                   probe.stderr)
        self.dir = tempfile.mkdtemp(prefix="chamber-synapse-")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.shared = secrets.token_hex(24)     # made here, dies with the directory
        self.tokens = {}
        self.proc = None
        os.mkdir(os.path.join(self.dir, "tokens"), 0o700)

    def start(self):
        cfg = os.path.join(self.dir, "homeserver.yaml")
        gen = subprocess.run([SYN_PY, "-m", "synapse.app.homeserver", "--server-name", "localhost", "--config-path", cfg,
                              "--generate-config", "--report-stats=no", "--data-directory", os.path.join(self.dir, "data")],
                             capture_output=True, text=True, cwd=self.dir)
        if gen.returncode != 0:
            cannot("synapse --generate-config failed", gen.stdout + gen.stderr)
        edit = ("import os, sys, yaml\n"
                "p = sys.argv[1]; c = yaml.safe_load(open(p))\n"
                "c['listeners'] = [{'bind_addresses': ['127.0.0.1'], 'port': int(sys.argv[2]), 'type': 'http', 'tls': False,\n"
                "                   'x_forwarded': False, 'resources': [{'names': ['client'], 'compress': False}]}]\n"
                "c['trusted_key_servers'] = []; c['federation_domain_whitelist'] = []; c['enable_registration'] = False\n"
                "c['presence'] = {'enabled': True}\n"
                "fast = {'per_second': 100, 'burst_count': 100}\n"
                "c['rc_login'] = {'address': fast, 'account': fast}; c['rc_message'] = fast\n"
                "c['rc_joins'] = {'local': fast, 'remote': fast}; c['rc_invites'] = {'per_room': fast, 'per_user': fast}\n"
                "c['registration_shared_secret'] = os.environ['RIG_SHARED']\n"
                "yaml.safe_dump(c, open(p, 'w'))\n")
        done = subprocess.run([SYN_PY, "-c", edit, cfg, str(self.port)], capture_output=True, text=True,
                              env=dict(os.environ, RIG_SHARED=self.shared))
        if done.returncode != 0:
            cannot("the generated config could not be edited", done.stderr)
        self.log = open(os.path.join(self.dir, "run.log"), "w")
        self.proc = subprocess.Popen([SYN_PY, "-m", "synapse.app.homeserver", "--config-path", cfg], cwd=self.dir,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while True:
            try:
                urllib.request.urlopen(self.url + "/_matrix/client/versions", timeout=2).close()
                return
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    cannot("synapse did not start", open(os.path.join(self.dir, "run.log")).read())
                time.sleep(0.5)

    def call(self, method, path, token=None, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def fill(self):
        for user in (PERSON, FO, PEER, IMA, IMB):
            status, d = self.call("GET", "/_synapse/admin/v1/register")
            if status != 200:
                cannot(f"no registration nonce ({status})")
            password = secrets.token_urlsafe(18)
            admin = user == PERSON
            mac = hmac.new(self.shared.encode(), digestmod=hashlib.sha1)
            mac.update(b"\x00".join([d["nonce"].encode(), user.encode(), password.encode(),
                                     b"admin" if admin else b"notadmin"]))
            status, d = self.call("POST", "/_synapse/admin/v1/register", body={
                "nonce": d["nonce"], "username": user, "password": password, "admin": admin, "mac": mac.hexdigest()})
            if status != 200 or "access_token" not in d:
                cannot(f"account {user} could not be made ({status}, {d.get('errcode')})")
            self.tokens[user] = d["access_token"]
            path = self.tokfile(user)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(d["access_token"])
        for name, members in ROOMS.items():
            status, d = self.call("POST", "/_matrix/client/v3/createRoom", self.tokens[PERSON],
                                  {"name": name, "preset": "private_chat", "power_level_content_override": {"invite": 100}})
            if status != 200:
                cannot(f"room {name} could not be made ({status}, {d.get('errcode')})")
            rid = urllib.parse.quote(d["room_id"], safe="")
            for user in members:
                s1, _ = self.call("POST", f"/_matrix/client/v3/rooms/{rid}/invite", self.tokens[PERSON],
                                  {"user_id": f"@{user}:localhost"})
                s2, _ = self.call("POST", f"/_matrix/client/v3/join/{rid}", self.tokens[user], {})
                if (s1, s2) != (200, 200):
                    cannot(f"{user} could not be put into {name} (invite {s1}, join {s2})")

    def tokfile(self, user):
        return os.path.join(self.dir, "tokens", user)

    def bodies(self, user, room):
        """Every message text in a room, read with the API directly, not with the client under test."""
        tok = self.tokens[user]
        joined = self.call("GET", "/_matrix/client/v3/joined_rooms", tok)[1]["joined_rooms"]
        rid = next(r for r in joined if self.call(
            "GET", "/_matrix/client/v3/rooms/%s/state/m.room.name" % urllib.parse.quote(r, safe=""), tok)[1].get("name") == room)
        d = self.call("GET", "/_matrix/client/v3/rooms/%s/messages?dir=b&limit=100" % urllib.parse.quote(rid, safe=""), tok)[1]
        return [e["content"].get("body") for e in d["chunk"] if e["type"] == "m.room.message"]

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def main():
    srv = Server()
    res = []

    def env(user, url=None, tokfile=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CHAMBER_")}
        e.update(CHAMBER_URL=url or srv.url, CHAMBER_TOKEN_FILE=tokfile or srv.tokfile(user), HOME=srv.dir)
        return e

    def ch(user, *args, stdin=None, url=None, tokfile=None, timeout=90):
        p = subprocess.run([sys.executable, "-I", CLIENT, *args], env=env(user, url, tokfile), input=stdin,
                           capture_output=True, timeout=timeout, cwd=srv.dir)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def bg(user, *args):
        return subprocess.Popen([sys.executable, "-I", CLIENT, *args], env=env(user), cwd=srv.dir,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def check(name, expect, got, ok):
        res.append(ok)
        print("%s %-56s expect: %-34s got: %s" % ("ok  " if ok else "FAIL", name, expect, got), flush=True)

    try:
        srv.start()
        srv.fill()
        b = srv.bodies

        rc, out, _ = ch(FO, "whoami")
        check("1 whoami", "@fo:localhost rc0", (out.strip(), rc), rc == 0 and out.strip() == "@fo:localhost")
        rc, out, _ = ch(IMA, "rooms")
        check("2 im sees only its own room", "cust-a only", out.split(), out.split() == ["cust-a"])
        n0 = len(b(FO, "fac"))
        rc, out, err = ch(FO, "send", "fac", "no recipient")
        check("3 send without --to is refused, nothing sent", "rc1, unchanged, says why",
              (rc, len(b(FO, "fac")) - n0, "without a recipient" in err),
              rc == 1 and len(b(FO, "fac")) == n0 and "without a recipient" in err)
        rc, out, err = ch(FO, "send", "fac", "--to", PEER, "--ref", "abc123", "T4 for peer")
        check("4 recipient never connected -> left, offline", "rc3 and message is in the room",
              (rc, "T4 for peer" in b(FO, "fac")), rc == 3 and "T4 for peer" in b(FO, "fac"))
        rc, out, _ = ch(PEER, "read", "fac")
        check("5a addressee reads it (positive control)", "shown", "T4 for peer" in out, "T4 for peer" in out)
        rc, out, err = ch(PERSON, "read", "fac")
        check("5b other member, default read", "hidden, with a notice", ("T4 for peer" in out, "left out" in err),
              "T4 for peer" not in out and "left out" in err)
        rc, out, _ = ch(PERSON, "read", "fac", "--everything")
        check("5c other member, --everything (filter, not isolation)", "shown", "T4 for peer" in out, "T4 for peer" in out)
        ch(FO, "send", "fac", "--to", "all", "T6 for all")
        a6 = "T6 for all" in ch(PEER, "read", "fac")[1]
        b6 = "T6 for all" in ch(PERSON, "read", "fac")[1]
        check("6 --to all reaches every member", "both read it", (a6, b6), a6 and b6)
        w = bg(PEER, "wait", "fac", "25")
        time.sleep(4)
        rc, out, err = ch(FO, "send", "fac", "--to", PEER, "T7 while online")
        wo, _ = w.communicate(timeout=40)
        check("7a recipient connected -> online", "rc0", (rc, (out.strip().splitlines() or [""])[-1].strip()), rc == 0)
        check("7b the waiting recipient receives it", "wait rc0 with the text",
              (w.returncode, "T7 while online" in wo.decode()), w.returncode == 0 and "T7 while online" in wo.decode())
        # An initial sync just before the stale message: a server that answers the next
        # initial sync from its cache hands back this older position, and a wait that
        # trusted it would replay the stale message (the client's own comment: measured
        # on Synapse 1.165.0, 2026-11-15).
        w = bg(IMA, "wait", "cust-a", "1")
        w.communicate(timeout=20)
        ch(FO, "send", "cust-a", "--to", IMA, "T8 stale, sent before the wait began")   # must NOT be replayed below
        w = bg(IMA, "wait", "cust-a", "8")
        time.sleep(3)
        ch(FO, "send", "cust-a", "--to", PERSON, "T8 not for the im")
        wo, _ = w.communicate(timeout=30)
        check("8a wait ignores others' and earlier messages", "rc4, nothing printed",
              (w.returncode, wo.decode()[:40]), w.returncode == 4 and wo.decode() == "")
        w = bg(IMA, "wait", "cust-a", "15")
        time.sleep(3)
        ch(FO, "send", "cust-a", "--to", IMA, "T8 for the im")
        wo, _ = w.communicate(timeout=30)
        check("8b same wait, addressed (positive control)", "rc0 with the text",
              (w.returncode, "T8 for the im" in wo.decode()), w.returncode == 0 and "T8 for the im" in wo.decode())
        n0 = len(b(FO, "fac"))
        rc, out, err = ch(FO, "send", "fac", "--to", IMA, "T9")
        check("9 recipient not in that room -> nothing sent", "rc1, count unchanged",
              (rc, len(b(FO, "fac")) - n0), rc == 1 and len(b(FO, "fac")) == n0)
        rc, out, err = ch(IMA, "send", "fac", "--to", FO, "T10")
        check("10 sender not in that room", "rc1, not in the room", (rc, "T10" in b(FO, "fac")),
              rc == 1 and "T10" not in b(FO, "fac"))
        rc, out, err = ch(FO, "send", "fac", "--to", PEER, "T11", url=f"http://127.0.0.1:{free_port()}")   # a port nobody listens on, picked now
        check("11 server unreachable is NOT reported as offline", "rc2", rc, rc == 2)
        exact = "id: t1\nexpires: 2026-11-16\n---\n測試指令 trailing space \n-----SIGNATURE-----\nAAAA==\n"
        rc, out, err = ch(FO, "send", "fac", "--to", PEER, "-", stdin=exact.encode())
        check("12 stdin text arrives byte-for-byte", "identical body", exact in b(PEER, "fac"), exact in b(PEER, "fac"))
        rc, out, err = ch(IMA, "presence", PEER)
        check("13 presence of someone you share no room with", "rc2 (cannot measure), not offline", rc, rc == 2)
        bad = srv.tokfile(".bad")
        with open(bad, "w") as fh:
            fh.write("not-a-token")
        os.chmod(bad, 0o600)
        rc, out, err = ch(FO, "whoami", tokfile=bad)
        check("14 wrong credential", "rc1", rc, rc == 1)
        os.remove(bad)
        shown = [ch(FO, "whoami"), ch(FO, "rooms"), ch(FO, "send", "fac", "--to", "all", "T15"), ch(FO, "read", "fac")]
        leak = any(srv.tokens[FO] in (o + e) for _, o, e in shown)
        check("15 credential never appears in output", "absent", leak, not leak)
        # k8scc#160: a reply to a message that is itself inside a thread. The in-thread
        # message is made with the API directly, in a stock client's shape (Element's
        # "reply in thread": rel m.thread + a fallback m.in_reply_to), not by the client.
        tok = srv.tokens[PERSON]
        frid = next(r for r in srv.call("GET", "/_matrix/client/v3/joined_rooms", tok)[1]["joined_rooms"]
                    if srv.call("GET", "/_matrix/client/v3/rooms/%s/state/m.room.name" % urllib.parse.quote(r, safe=""), tok)[1].get("name") == "fac")
        qr = urllib.parse.quote(frid, safe="")
        root = srv.call("PUT", f"/_matrix/client/v3/rooms/{qr}/send/m.room.message/r17", tok,
                        {"msgtype": "m.text", "body": "T17 root", "m.mentions": {"user_ids": ["@fo:localhost"]}})[1]["event_id"]
        inner = srv.call("PUT", f"/_matrix/client/v3/rooms/{qr}/send/m.room.message/i17", tok,
                         {"msgtype": "m.text", "body": "T17 in the thread", "m.mentions": {"user_ids": ["@fo:localhost"]},
                          "m.relates_to": {"rel_type": "m.thread", "event_id": root, "is_falling_back": True,
                                           "m.in_reply_to": {"event_id": root}}})[1]["event_id"]
        rc, out, err = ch(FO, "send", "fac", "--to", PERSON, "--reply-to", inner, "T17 answer")
        sent = next((l.split()[1] for l in out.splitlines() if l.startswith("sent ")), None)
        got = srv.call("GET", f"/_matrix/client/v3/rooms/{qr}/event/{urllib.parse.quote(sent, safe='')}", tok)[1] if sent else {}
        relates = (got.get("content") or {}).get("m.relates_to") or {}
        check("17 --reply-to a message inside a thread is sent into that thread", "rc0/3, root and in_reply_to",
              (rc, relates.get("event_id") == root, (relates.get("m.in_reply_to") or {}).get("event_id") == inner, err.strip()[-80:]),
              rc in (0, 3) and relates.get("rel_type") == "m.thread" and relates.get("event_id") == root
              and (relates.get("m.in_reply_to") or {}).get("event_id") == inner)
        rc, out, _ = ch(FO, "read", "fac", "20", "--everything")
        hdr = lambda eid: next((l for l in out.splitlines() if " %s " % eid in l), "")
        check("17b read marks the in-thread message answered, not the root", "inner marked, root not",
              ("answered" in hdr(inner), "answered" in hdr(root)), "answered" in hdr(inner) and "answered" not in hdr(root))
        t0 = time.time()
        state = None
        while time.time() - t0 < 90:
            state = ch(FO, "presence", PEER)[1].strip()
            if state == "offline":
                break
            time.sleep(3)
        check("16 recipient stops syncing -> shows offline", "offline within 90 s",
              (state, "%.0f s" % (time.time() - t0)), state == "offline")
    except subprocess.TimeoutExpired as exc:
        srv.stop()
        cannot(f"a client call did not return within its time limit ({exc.cmd[-3:]}, {exc.timeout} s)")
    finally:
        srv.stop()
    print("\n%d/%d matched" % (sum(res), len(res)))
    sys.exit(0 if res and all(res) else 1)


if __name__ == "__main__":
    main()
