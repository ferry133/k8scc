#!/usr/bin/env python3
"""Controls for .github/scripts/bump_content_gate.py (k8scc#153), offline.

Layers are tars made here, so the three cells the opener asked for can be
built without a registry: a docs-only commit (same files, new mtimes) is
`same`; a commit that changes entrypoint.sh is `differs`; a moved base layer
is `differs`. Each `same` cell has a `differs` twin that differs in exactly one
thing, so a gate that answers `same` to everything cannot pass.

The real-image readings (GHCR, 2026-11-27) are in the PR, not here: this file
must run without a network.

Run: python3 test/test_bump_content_gate.py
"""
import gzip
import hashlib
import io
import pathlib
import sys
import tarfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
sys.dont_write_bytecode = True
import bump_content_gate as g  # noqa: E402


def tar(files, mtime=1000, gz=True, mode=0o755):
    """files: list of (name, bytes) or (name, None) for a directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in files:
            ti = tarfile.TarInfo(name)
            ti.mtime = mtime
            ti.mode = mode
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    # gzip with a fixed header time: equal input must give an equal digest
    return gzip.compress(buf.getvalue(), mtime=0) if gz else buf.getvalue()


BASE = [("etc/", None), ("etc/debian_version", b"12.8\n")]
ENTRY = [("usr/local/bin/entrypoint.sh", b"#!/bin/sh\nexec tini -- ttyd\n")]
POLICY = [("etc/claude-code/CLAUDE.md", b"policy\n")]


def image(layers, labels=None, created="2026-11-27T00:00:00Z"):
    """layers: list of tar bytes. Returns (platform -> manifest, blob store)."""
    blobs, mans = {}, {}
    for pf in g.PLATFORMS:
        ls = []
        for t in layers:
            d = "sha256:" + hashlib.sha256(t).hexdigest()
            blobs[d] = t
            ls.append({"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": d, "size": len(t)})
        cfg = {"architecture": pf.split("/")[1], "os": "linux", "created": created,
               "config": {"Entrypoint": ["entrypoint.sh"],
                          "Labels": {"org.opencontainers.image.created": created,
                                     "org.opencontainers.image.revision": created,
                                     "org.opencontainers.image.version": created, **(labels or {})}},
               "rootfs": {"type": "layers", "diff_ids": [hashlib.sha256(t).hexdigest() for t in layers]},
               "history": [{"created": created, "created_by": f"layer {i}"} for i in range(len(layers))]}
        cb = repr(sorted(cfg.items())).encode()
        cd = "sha256:" + hashlib.sha256(cb).hexdigest()
        blobs[cd] = cfg
        mans[pf] = {"config": {"digest": cd}, "layers": ls}
    return mans, blobs


def run(old, new):
    (mo, bo), (mn, bn) = old, new
    store = {**bo, **bn}
    fetched = []

    def layer_entries(layer):
        fetched.append(layer["digest"])
        return g.canonical_entries(io.BytesIO(store[layer["digest"]]), gz=True)

    return g.compare(mo, mn, layer_entries, lambda m: store[m["config"]["digest"]]), fetched


class Cells(unittest.TestCase):
    def test_docs_only_is_same(self):
        old = image([tar(BASE), tar(ENTRY, mtime=1000), tar(POLICY, mtime=1000)])
        new = image([tar(BASE), tar(ENTRY, mtime=2000), tar(POLICY, mtime=2000)], created="2026-11-28T00:00:00Z")
        (verdict, detail), fetched = run(old, new)
        self.assertEqual(verdict, "same", detail)
        self.assertIn("2 layer(s) new bytes", detail)
        self.assertEqual(len(fetched), 2 * 2 * len(g.PLATFORMS), "only the replaced layers are read, both sides")

    def test_entrypoint_change_differs(self):
        old = image([tar(BASE), tar(ENTRY, mtime=1000), tar(POLICY)])
        changed = [("usr/local/bin/entrypoint.sh", b"#!/bin/sh\nexec ttyd\n")]
        new = image([tar(BASE), tar(changed, mtime=2000), tar(POLICY)])
        (verdict, detail), _ = run(old, new)
        self.assertEqual(verdict, "differs")
        self.assertIn("layer 1", detail)

    def test_base_move_differs(self):
        old = image([tar(BASE), tar(ENTRY)])
        new = image([tar([("etc/", None), ("etc/debian_version", b"12.9\n")]), tar(ENTRY)])
        (verdict, detail), _ = run(old, new)
        self.assertEqual(verdict, "differs")
        self.assertIn("layer 0", detail)


class Twins(unittest.TestCase):
    """Things that are not mtime must count."""

    def _differs(self, old_layer, new_layer):
        (verdict, detail), _ = run(image([tar(BASE), old_layer]), image([tar(BASE), new_layer]))
        self.assertEqual(verdict, "differs", detail)

    def test_mode(self):
        self._differs(tar(ENTRY, mode=0o755), tar(ENTRY, mtime=2000, mode=0o644))

    def test_added_file(self):
        self._differs(tar(ENTRY), tar(ENTRY + POLICY, mtime=2000))

    def test_renamed_file(self):
        self._differs(tar(ENTRY), tar([("usr/local/bin/entrypoint", ENTRY[0][1])], mtime=2000))

    def _raw(self, name, mtime, uid=0, gid=0, link=None):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
            ti = tarfile.TarInfo(name)
            ti.mtime, ti.uid, ti.gid = mtime, uid, gid
            if link is None:
                ti.size = 1
                tf.addfile(ti, io.BytesIO(b"x"))
            else:
                ti.type, ti.linkname = tarfile.SYMTYPE, link
                tf.addfile(ti)
        return gzip.compress(buf.getvalue(), mtime=0)

    def test_owner(self):
        # jg-base-d4's m6 on #156: uid/gid dropped from the tuple survived
        self._differs(self._raw("home/claude/f", 1, uid=1000, gid=1000), self._raw("home/claude/f", 2, uid=0, gid=1000))
        self._differs(self._raw("home/claude/f", 1, uid=1000, gid=1000), self._raw("home/claude/f", 2, uid=1000, gid=0))
        (v, d), _ = run(image([tar(BASE), self._raw("home/claude/f", 1, uid=1000, gid=1000)]),
                        image([tar(BASE), self._raw("home/claude/f", 2, uid=1000, gid=1000)]))
        self.assertEqual(v, "same", d)   # control: the same owner with a new mtime

    def test_symlink_target(self):
        # jg-base-d4's m7 on #156: linkname dropped from the tuple survived
        self._differs(self._raw("usr/bin/python3", 1, link="python3.11"), self._raw("usr/bin/python3", 2, link="python3.12"))
        (v, d), _ = run(image([tar(BASE), self._raw("usr/bin/python3", 1, link="python3.11")]),
                        image([tar(BASE), self._raw("usr/bin/python3", 2, link="python3.11")]))
        self.assertEqual(v, "same", d)   # control: the same target with a new mtime

    def test_layer_count(self):
        (verdict, detail), _ = run(image([tar(BASE), tar(ENTRY)]), image([tar(BASE), tar(ENTRY), tar(POLICY)]))
        self.assertEqual(verdict, "differs")
        self.assertIn("2 -> 3 layers", detail, "said by the count, not left to the config's history")

    def test_config_beyond_build_fields(self):
        old = image([tar(BASE), tar(ENTRY)])
        new = image([tar(BASE), tar(ENTRY, mtime=2000)], labels={"something.else": "x"})
        (verdict, detail), _ = run(old, new)
        self.assertEqual(verdict, "differs")
        self.assertIn("config", detail)

    def test_build_labels_alone_are_same(self):
        (verdict, detail), _ = run(image([tar(BASE), tar(ENTRY)]),
                                   image([tar(BASE), tar(ENTRY, mtime=5)], created="later"))
        self.assertEqual(verdict, "same", detail)

    def test_missing_platform(self):
        mo, bo = image([tar(BASE)])
        mn, bn = image([tar(BASE)])
        mn.pop("linux/arm64")
        (verdict, _), _ = run((mo, bo), (mn, bn))
        self.assertEqual(verdict, "differs")

    def test_pax_mtime_ignored_but_other_pax_kept(self):
        def with_pax(pax, mtime):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
                ti = tarfile.TarInfo("f")
                ti.size, ti.mtime, ti.pax_headers = 1, mtime, pax
                tf.addfile(ti, io.BytesIO(b"x"))
            return gzip.compress(buf.getvalue(), mtime=0)
        (v1, _), _ = run(image([with_pax({"mtime": "1.5"}, 1)]), image([with_pax({"mtime": "9.5"}, 9)]))
        self.assertEqual(v1, "same")
        (v2, _), _ = run(image([with_pax({"SCHILY.xattr.security.capability": "a"}, 1)]),
                         image([with_pax({"SCHILY.xattr.security.capability": "b"}, 9)]))
        self.assertEqual(v2, "differs", "a file capability (ping's setcap) is content")


class Unmeasured(unittest.TestCase):
    def test_cli_without_network_is_unmeasured_not_same(self):
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / ".github/scripts/bump_content_gate.py"), "zzzzzzz", "yyyyyyy"],
                           capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "https_proxy": "http://127.0.0.1:9"},
                           timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith("unmeasured\t"), r.stdout)

    def test_unknown_layer_type_is_unmeasured(self):
        def boom(layer):
            raise g.Unmeasured("layer type x not read")
        mo, bo = image([tar(BASE), tar(ENTRY)])
        mn, bn = image([tar(BASE), tar(ENTRY, mtime=2)])
        store = {**bo, **bn}
        with self.assertRaises(g.Unmeasured):
            g.compare(mo, mn, boom, lambda m: store[m["config"]["digest"]])


if __name__ == "__main__":
    unittest.main(verbosity=1)
