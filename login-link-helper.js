/*
 * login-link-helper — injected into ttyd's index.html (see patch-ttyd-index.py).
 *
 * Claude Code's OAuth login URL is hard-wrapped by its TUI (Ink inserts real
 * newlines at the terminal width), so xterm.js link detection only ever sees
 * the first physical line and clicking opens a truncated, broken URL.
 *
 * This script watches the ttyd WebSocket output stream, reassembles the full
 * URL across wrapped lines (a line that fills the whole terminal width
 * continues onto the next — the same rule the terminal itself uses), then:
 *   1. overlays a real clickable "sign in" button on the page, and
 *   2. wraps window.open() so clicking the truncated in-terminal link
 *      navigates to the reassembled full URL instead.
 *
 * Must run BEFORE the ttyd client bundle so the WebSocket wrapper is in
 * place when the client connects.
 */
(function () {
  'use strict';

  var BTN_ID = 'claude-login-link-helper';
  var LOGIN_URL_RE = /https:\/\/(?:claude\.(?:ai|com)|console\.anthropic\.com)\/[^\s]*oauth[^\s]*/;

  var cols = 0;          // terminal width, learned from outgoing resize frames
  var rawBuf = '';       // rolling buffer of decoded terminal output
  var captured = '';     // most recently reassembled login URL
  var scanTimer = null;
  var decoder = new TextDecoder('utf-8');

  // --- terminal output → plain lines -------------------------------------

  // SGR/erase sequences vanish; any cursor-movement sequence breaks the line,
  // so absolute-positioned redraws can't glue unrelated text together.
  function toLines(raw) {
    var text = raw
      .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?/g, '')        // OSC
      .replace(/\x1b[PX^_][\s\S]*?\x1b\\/g, '')                  // DCS/PM/APC
      .replace(/\x1b\[[0-9;?]*[mKJ]/g, '')                       // SGR + erase
      .replace(/\x1b\[[0-9;?!"'#$%&*+\-.\/ ]*[@-~]/g, '\n')      // other CSI = cursor move
      .replace(/\x1b[@-Z\\-_=><]/g, '')                          // 2-char ESC
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '')
      .replace(/\r/g, '');
    return text.split('\n');
  }

  // A URL fragment continues onto the next line iff it runs to the edge of a
  // full-width line. Scan bottom-up so the latest rendered frame wins.
  function extractLoginUrl(lines, effCols) {
    for (var i = lines.length - 1; i >= 0; i--) {
      var line = lines[i];
      var m = line.match(LOGIN_URL_RE);
      if (!m) continue;
      var url = m[0];
      var atEdge = effCols > 0 &&
        line.length >= effCols &&
        m.index + m[0].length >= line.replace(/\s+$/, '').length;
      var k = i;
      while (atEdge && k + 1 < lines.length) {
        var cm = lines[k + 1].match(/^[^\s]+/);
        if (!cm) break;
        url += cm[0];
        k++;
        atEdge = cm[0].length >= effCols;
      }
      if (url.length > 80) return url;
    }
    return '';
  }

  function effectiveCols(lines) {
    if (cols > 0) return cols;
    var max = 0;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].length > max) max = lines[i].length;
    }
    return max >= 40 ? max : 0;
  }

  function scan() {
    scanTimer = null;
    var lines = toLines(rawBuf);
    var url = extractLoginUrl(lines, effectiveCols(lines));
    if (url && url !== captured) {
      captured = url;
      showButton(url);
    }
  }

  function scheduleScan() {
    if (!scanTimer) scanTimer = setTimeout(scan, 150);
  }

  // --- overlay button ------------------------------------------------------

  function showButton(url) {
    var box = document.getElementById(BTN_ID);
    if (!box) {
      box = document.createElement('div');
      box.id = BTN_ID;
      box.style.cssText =
        'position:fixed;top:12px;right:12px;z-index:2147483647;display:flex;' +
        'align-items:center;gap:8px;background:#d97757;color:#fff;' +
        'padding:10px 14px;border-radius:10px;box-shadow:0 4px 14px rgba(0,0,0,.45);' +
        'font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;';
      var a = document.createElement('a');
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = '🔗 開啟 Claude 登入頁 / Open sign-in page';
      a.style.cssText = 'color:#fff;text-decoration:none;font-weight:600;';
      var x = document.createElement('span');
      x.textContent = '✕';
      x.style.cssText = 'cursor:pointer;opacity:.75;padding:0 2px;';
      x.onclick = function () { hideButton(); };
      box.appendChild(a);
      box.appendChild(x);
      document.body.appendChild(box);
    }
    box.querySelector('a').href = url;
    box.style.display = 'flex';
  }

  function hideButton() {
    var box = document.getElementById(BTN_ID);
    if (box) box.style.display = 'none';
  }

  // --- WebSocket tap -------------------------------------------------------

  function onFrame(data) {
    if (typeof data === 'string') return;
    if (typeof Blob !== 'undefined' && data instanceof Blob) {
      data.arrayBuffer().then(function (ab) { onFrame(ab); });
      return;
    }
    var u8 = new Uint8Array(data);
    if (u8.length < 2 || u8[0] !== 0x30) return; // '0' = OUTPUT
    var chunk = decoder.decode(u8.subarray(1), { stream: true });
    rawBuf += chunk;
    if (rawBuf.length > 65536) rawBuf = rawBuf.slice(-32768);
    if (/Login successful|登入成功/.test(chunk)) {
      captured = '';
      hideButton();
      return;
    }
    scheduleScan();
  }

  function sniffCols(payload) {
    try {
      var s = typeof payload === 'string'
        ? payload
        : (payload && payload.byteLength > 0 && payload.byteLength < 4096
            ? new TextDecoder('utf-8').decode(payload)
            : '');
      // only auth ('{...}') and resize ('1{...}') frames, never input ('0...')
      if (!s || (s[0] !== '{' && s[0] !== '1')) return;
      var m = s.match(/"columns":\s*(\d+)/);
      if (m) cols = parseInt(m[1], 10) || cols;
    } catch (e) { /* ignore */ }
  }

  var NativeWS = window.WebSocket;
  function PatchedWS(url, protocols) {
    var ws = protocols !== undefined ? new NativeWS(url, protocols) : new NativeWS(url);
    var nativeSend = ws.send.bind(ws);
    ws.send = function (d) {
      sniffCols(d instanceof Uint8Array ? d : (typeof d === 'string' ? d : ''));
      return nativeSend(d);
    };
    ws.addEventListener('message', function (ev) {
      try { onFrame(ev.data); } catch (e) { /* never break the client */ }
    });
    return ws;
  }
  PatchedWS.prototype = NativeWS.prototype;
  PatchedWS.CONNECTING = NativeWS.CONNECTING;
  PatchedWS.OPEN = NativeWS.OPEN;
  PatchedWS.CLOSING = NativeWS.CLOSING;
  PatchedWS.CLOSED = NativeWS.CLOSED;
  window.WebSocket = PatchedWS;

  // --- window.open facade --------------------------------------------------
  // WebLinksAddon opens links as: w = window.open(); w.opener = null;
  // w.location.href = url;  — hand it a facade so a truncated claude URL
  // (prefix of the captured one) is upgraded to the full URL.

  var nativeOpen = window.open.bind(window);
  window.open = function (url, name, features) {
    if (url !== undefined || !captured) return nativeOpen(url, name, features);
    var real = nativeOpen();
    if (!real) return real;
    return {
      get opener() { return real.opener; },
      set opener(v) { try { real.opener = v; } catch (e) { /* ignore */ } },
      location: {
        get href() { return real.location.href; },
        set href(v) {
          if (typeof v === 'string' && captured &&
              v.length < captured.length && captured.indexOf(v) === 0) {
            v = captured;
          }
          real.location.href = v;
        }
      },
      focus: function () { real.focus(); },
      close: function () { real.close(); }
    };
  };
})();
