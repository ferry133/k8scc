#!/usr/bin/env python3
"""Why did the digest change? Three readings for the jg-base bump PR (k8scc#15).

A bump PR used to say only "bump im pin to <sha>", and the digest always
changes, so the PR could not tell a reader whether anything in the image did.
3a23d9d was the case that showed the gap: no image-definition file changed, yet
20 of 22 layers were different bytes, because debian:12-slim itself had moved.
"Definition unchanged" alone would have been false comfort.

So the body carries three cells, each computed, each able to be false on its
own (jgb-handler [20db54]'s acceptance condition: three fields printing one
judgement are no better than one field):

  1. image definition changed?  git diff OLD..NEW over the Dockerfile and every
                                file the `base` stage COPYs (read from the
                                Dockerfile, not from a list kept here)
  2. layers replaced            per platform, layers of NEW whose digest is not
                                among OLD's layers
  3. base moved?                OLD's layer 0 vs NEW's layer 0, and NEW's layer 0
                                vs the upstream base image's layer 0 today

A reading that could not be taken is printed as "not measured (<why>)" -- never
folded into "no change".

Run: python3 .github/scripts/bump_explain.py OLD_SHORT NEW_SHORT   (from a k8scc
checkout with both commits; needs network for GHCR and Docker Hub)
Controls: python3 test/test_bump_explain.py (offline, recorded manifests)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request

IMAGE = "ferry133/claude-code"
PLATFORMS = ("linux/amd64", "linux/arm64")
INDEX_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


# --- pure part (what the controls drive) -----------------------------------------

def base_stage_inputs(dockerfile: str) -> tuple[str, list[str]]:
    """(the `base` stage's FROM image, the repo paths it COPYs). Only the base
    stage: the pin is the base image, and a factory-only change does not touch it."""
    from_image, sources, in_base = "", [], False
    for raw in dockerfile.splitlines():
        line = raw.strip()
        m = re.match(r"FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
        if m:
            in_base = (m.group(2) or "").lower() == "base"
            if in_base:
                from_image = m.group(1)
            continue
        if in_base and re.match(r"(COPY|ADD)\s", line, re.I) and "--from=" not in line:
            words = [w for w in line.split()[1:] if not w.startswith("--")]
            sources += words[:-1]
    return from_image, sources


def layers_replaced(old: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, tuple[int, int] | None]:
    """platform -> (layers of NEW not in OLD, layers of NEW); None if either side lacks it."""
    out: dict[str, tuple[int, int] | None] = {}
    for p in PLATFORMS:
        if p not in old or p not in new:
            out[p] = None
            continue
        old_set = set(old[p])
        out[p] = (sum(1 for d in new[p] if d not in old_set), len(new[p]))
    return out


def render(old: str, new: str, changed: list[str] | None, changed_err: str,
           replaced: dict[str, tuple[int, int] | None], replaced_err: str,
           old_l0: str | None, new_l0: str | None, upstream_l0: str | None,
           from_image: str, base_err: str) -> str:
    def short(d: str | None) -> str:
        return d.split(":", 1)[-1][:12] if d else "?"

    if changed is None:
        c1 = f"not measured ({changed_err})"
    elif changed:
        c1 = f"**yes** — {len(changed)} file(s): " + ", ".join(f"`{f}`" for f in changed)
    else:
        c1 = "**no** — no diff in `Dockerfile` or any file the base stage COPYs"

    if replaced_err:
        c2 = f"not measured ({replaced_err})"
    else:
        parts = []
        for p in PLATFORMS:
            r = replaced.get(p)
            parts.append(f"{p}: not measured (platform missing)" if r is None else f"{p}: **{r[0]}/{r[1]}** replaced")
        c2 = "; ".join(parts)

    if base_err or old_l0 is None or new_l0 is None:
        c3 = f"not measured ({base_err or 'layer 0 unreadable'})"
    else:
        moved = old_l0 != new_l0
        c3 = (f"**{'yes' if moved else 'no'}** — layer 0 `{short(old_l0)}` → `{short(new_l0)}`"
              if moved else f"**no** — layer 0 `{short(new_l0)}` on both")
        if upstream_l0 is None:
            c3 += f"; `{from_image}` today: not measured"
        else:
            c3 += f"; `{from_image}` today is `{short(upstream_l0)}`" + (
                " (= new)" if upstream_l0 == new_l0 else " (≠ new: upstream has moved again since this build)")

    return "\n".join([
        f"**Why the digest changed, `{old}` → `{new}`** (k8scc#15; computed by `.github/scripts/bump_explain.py`, "
        "each cell independent — a reading that could not be taken says so):",
        "",
        "| | reading |",
        "|---|---|",
        f"| image definition changed? | {c1} |",
        f"| layers replaced (linux) | {c2} |",
        f"| base image moved? | {c3} |",
    ])


# --- I/O ---------------------------------------------------------------------------

def _get(url: str, token: str | None = None, accept: str = INDEX_TYPES) -> tuple[dict, str]:
    req = urllib.request.Request(url, headers={"Accept": accept})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()), r.headers.get("Content-Type", "")


def _platform_layers(registry: str, repo: str, ref: str, token: str) -> dict[str, list[str]]:
    index, ctype = _get(f"https://{registry}/v2/{repo}/manifests/{ref}", token)
    if "index" not in ctype and "list" not in ctype:
        raise RuntimeError(f"{repo}:{ref} is not a multi-platform index ({ctype})")
    out = {}
    for m in index.get("manifests", []):
        pf = m.get("platform") or {}
        key = f"{pf.get('os')}/{pf.get('architecture')}"
        if key in PLATFORMS:   # attestation manifests are unknown/unknown
            man, _ = _get(f"https://{registry}/v2/{repo}/manifests/{m['digest']}", token)
            out[key] = [layer["digest"] for layer in man["layers"]]
    return out


def _ghcr_token(repo: str) -> str:
    tok, _ = _get(f"https://ghcr.io/token?scope=repository:{repo}:pull", accept="application/json")
    return tok["token"]


def _hub_layers(image: str) -> dict[str, list[str]]:
    name, _, tag = image.partition(":")
    repo = name if "/" in name else f"library/{name}"
    tok, _ = _get(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull",
                  accept="application/json")
    return _platform_layers("registry-1.docker.io", repo, tag or "latest", tok["token"])


def main(old: str, new: str) -> str:
    dockerfile = subprocess.run(["git", "show", f"{new}:Dockerfile"], capture_output=True, text=True, check=True).stdout
    from_image, sources = base_stage_inputs(dockerfile)

    changed, changed_err = None, ""
    try:
        diff = subprocess.run(["git", "diff", "--name-only", f"{old}..{new}", "--", "Dockerfile", *sources],
                              capture_output=True, text=True, check=True).stdout
        changed = [f for f in diff.splitlines() if f]
    except subprocess.CalledProcessError as exc:
        changed_err = (exc.stderr or str(exc)).strip().splitlines()[0]

    replaced, replaced_err = {}, ""
    old_l0 = new_l0 = upstream_l0 = None
    base_err = ""
    try:
        tok = _ghcr_token(IMAGE)
        old_layers = _platform_layers("ghcr.io", IMAGE, old, tok)
        new_layers = _platform_layers("ghcr.io", IMAGE, new, tok)
        replaced = layers_replaced(old_layers, new_layers)
        old_l0 = (old_layers.get("linux/amd64") or [None])[0]
        new_l0 = (new_layers.get("linux/amd64") or [None])[0]
    except Exception as exc:   # network, auth, a tag that is gone
        replaced_err = base_err = f"GHCR: {exc}"
    if not base_err:
        try:
            upstream_l0 = (_hub_layers(from_image).get("linux/amd64") or [None])[0]
        except Exception:
            upstream_l0 = None
    return render(old, new, changed, changed_err, replaced, replaced_err,
                  old_l0, new_l0, upstream_l0, from_image, base_err)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: bump_explain.py OLD_SHORT NEW_SHORT")
    print(main(sys.argv[1], sys.argv[2]))
