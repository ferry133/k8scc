#!/usr/bin/env python3
"""Build-time step: extract ttyd's embedded index.html and inject
login-link-helper.js, producing the page served via `ttyd --index`.

ttyd embeds its single-file web client gzip-compressed inside the binary;
scan for gzip members and take the largest one that decompresses to HTML.
The helper <script> is inserted before the client bundle's <script> so its
WebSocket wrapper is installed before the client connects.

Usage: patch-ttyd-index.py <ttyd-binary> <helper-js> <output-html>
"""
import sys
import zlib

ttyd_bin, helper_js, out_html = sys.argv[1], sys.argv[2], sys.argv[3]

data = open(ttyd_bin, "rb").read()
best = b""
pos = 0
while True:
    pos = data.find(b"\x1f\x8b\x08", pos)
    if pos < 0:
        break
    try:
        blob = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[pos:])
        if b"<html" in blob[:2000].lower() and len(blob) > len(best):
            best = blob
    except Exception:
        pass
    pos += 1

if not best:
    sys.exit("error: no embedded index.html found in " + ttyd_bin)

html = best.decode("utf-8")
helper = open(helper_js, encoding="utf-8").read()
if "</script" in helper:
    sys.exit("error: helper JS must not contain '</script'")

inject = "<script>/* login-link-helper */\n" + helper + "</script>"
idx = html.find("<script")
if idx < 0:
    sys.exit("error: no <script> tag found in extracted index.html")
html = html[:idx] + inject + html[idx:]

open(out_html, "w", encoding="utf-8").write(html)
print(f"patched index.html written to {out_html} ({len(html)} bytes)")
