#!/usr/bin/env python3
"""Is the image NEW the same content as OLD? (k8scc#153)

A docs-only k8scc commit still publishes a new digest: every checkout gives the
COPYed files a new mtime, so the COPY layers are new bytes with the same files
in them, and the index carries fresh provenance besides (k8scc#3 is why that
cannot be made reproducible). jg-base-d4 measured it on jg-base#282 and #283:
amd64 layers 29-34 replaced, every file's sha256 and mode equal, arm64 0
layers, configs equal without the created and revision labels -- and both
production im pods restarted twice for that.

So before the bump job opens or rewrites a jg-base PR it asks this script:

  same        every platform's layers hold the same files (name, type, mode,
              owner, size, link target, content sha256; NOT mtime) and the
              configs are equal once the build-time fields are dropped
  differs     anything else, with where
  unmeasured  could not tell (network, a tag that is gone, a layer type it
              cannot read, the download budget); the caller opens the PR as
              before and says so -- "could not tell" never suppresses a bump

Output is one line `<verdict>\t<detail>`; exit 0 same, 1 differs, 2 unmeasured.

Controls: python3 test/test_bump_content_gate.py (offline, tars made at test time).
"""
from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from bump_explain import IMAGE, INDEX_TYPES, PLATFORMS, _ghcr_token  # noqa: E402

LAYER_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.oci.image.layer.v1.tar",
}
# Labels a build stamps on its own: the time, the commit, the tag. Equal content
# from two commits differs here by construction.
BUILD_LABELS = ("org.opencontainers.image.created", "org.opencontainers.image.revision",
                "org.opencontainers.image.version")
BUDGET = 3 << 30   # bytes downloaded per run, both sides together


class Unmeasured(Exception):
    pass


# --- pure part (what the controls drive) -----------------------------------------

def canonical_entries(fileobj, gz: bool) -> list[tuple]:
    """Every tar entry as the tuple that matters for content; mtime left out."""
    out = []
    with tarfile.open(fileobj=fileobj, mode="r|gz" if gz else "r|") as tf:
        for m in tf:
            pax = tuple(sorted((k, v) for k, v in m.pax_headers.items()
                               if k not in ("mtime", "atime", "ctime")))
            digest = ""
            if m.isreg():
                f = tf.extractfile(m)
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                digest = h.hexdigest()
            out.append((m.name, m.type, m.mode, m.uid, m.gid, m.uname, m.gname,
                        m.size, m.linkname, m.devmajor, m.devminor, pax, digest))
    return out


def normalized_config(cfg: dict) -> dict:
    """The image config minus what a build of the same content changes anyway."""
    c = json.loads(json.dumps(cfg))
    c.pop("created", None)
    for h in c.get("history") or []:
        h.pop("created", None)
    (c.get("rootfs") or {}).pop("diff_ids", None)   # digests of the mtime-bearing tars
    labels = (c.get("config") or {}).get("Labels") or {}
    for k in BUILD_LABELS:
        labels.pop(k, None)
    return c


def compare(old: dict, new: dict, layer_entries, config_of) -> tuple[str, str]:
    """old/new: platform -> manifest dict. layer_entries(layer) and config_of(manifest)
    fetch; either may raise Unmeasured. Returns (verdict, detail)."""
    if set(old) != set(new) or not set(PLATFORMS) <= set(new):
        return "differs", f"platforms {sorted(old)} -> {sorted(new)}"
    replaced = {}
    for pf in PLATFORMS:
        lo, ln = old[pf]["layers"], new[pf]["layers"]
        if len(lo) != len(ln):
            return "differs", f"{pf}: {len(lo)} -> {len(ln)} layers"
        if normalized_config(config_of(old[pf])) != normalized_config(config_of(new[pf])):
            return "differs", f"{pf}: the config differs beyond the build-time fields"
        n = 0
        for i, (a, b) in enumerate(zip(lo, ln)):
            if a["digest"] == b["digest"]:
                continue
            n += 1
            if layer_entries(a) != layer_entries(b):
                return "differs", f"{pf}: layer {i} holds different files"
        replaced[pf] = n
    return "same", "; ".join(f"{pf}: {replaced[pf]} layer(s) new bytes, same files" for pf in PLATFORMS)


# --- I/O ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class _Budget:
    def __init__(self, raw, state):
        self.raw, self.state = raw, state

    def read(self, n=-1):
        b = self.raw.read(n)
        self.state["used"] += len(b)
        if self.state["used"] > BUDGET:
            raise Unmeasured(f"download budget of {BUDGET >> 30} GiB spent")
        return b


def _blob(repo: str, digest: str, token: str):
    req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/blobs/{digest}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        return _opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            # the storage URL is signed; the registry token must not follow it
            return urllib.request.urlopen(e.headers["Location"], timeout=60)
        raise


def _manifests(repo: str, ref: str, token: str) -> dict:
    req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/manifests/{ref}",
                                 headers={"Accept": INDEX_TYPES, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        index = json.loads(r.read())
    out = {}
    for m in index.get("manifests", []):
        pf = m.get("platform") or {}
        key = f"{pf.get('os')}/{pf.get('architecture')}"
        if key in PLATFORMS:
            req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/manifests/{m['digest']}",
                                         headers={"Accept": INDEX_TYPES, "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out[key] = json.loads(r.read())
    return out


def main(old: str, new: str) -> tuple[str, str]:
    try:
        token = _ghcr_token(IMAGE)
        mo, mn = _manifests(IMAGE, old, token), _manifests(IMAGE, new, token)
        state = {"used": 0}

        def layer_entries(layer):
            mt = layer.get("mediaType", "")
            if mt not in LAYER_TYPES:
                raise Unmeasured(f"layer type {mt or '?'} not read")
            with _blob(IMAGE, layer["digest"], token) as r:
                return canonical_entries(_Budget(r, state), gz=mt.endswith("gzip"))

        def config_of(man):
            with _blob(IMAGE, man["config"]["digest"], token) as r:
                return json.loads(r.read())

        verdict, detail = compare(mo, mn, layer_entries, config_of)
        return verdict, f"{detail} ({state['used'] / (1 << 20):.1f} MiB read)"
    except Unmeasured as e:
        return "unmeasured", str(e)
    except Exception as e:   # network, auth, a tag that is gone, a tar it cannot parse
        return "unmeasured", f"{type(e).__name__}: {str(e)[:200]}"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: bump_content_gate.py OLD_SHORT NEW_SHORT")
    verdict, detail = main(sys.argv[1], sys.argv[2])
    print(f"{verdict}\t{detail}")
    sys.exit({"same": 0, "differs": 1}.get(verdict, 2))
