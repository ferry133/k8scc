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
 *   1. overlays a clickable "sign in" button while the login screen is
 *      visible (hidden again once the login screen is gone),
 *   2. wraps window.open() so clicking the truncated in-terminal link
 *      navigates to the reassembled full URL instead, and
 *   3. auto-submits the authentication code: when the user returns to this
 *      tab with the code in the clipboard (copied from the Claude callback
 *      page), it is validated against the OAuth `state` parameter of the
 *      captured URL and typed into the terminal automatically. A manual
 *      "paste code" button covers browsers that block clipboard reads
 *      without a user gesture.
 *
 * Must run BEFORE the ttyd client bundle so the WebSocket wrapper is in
 * place when the client connects.
 */
(function () {
  'use strict';

  var BOX_ID = 'claude-login-link-helper';
  var URL_RE = /https:\/\/(?:claude\.(?:ai|com)|console\.anthropic\.com)\/[^\s]*oauth[^\s]*/;
  var URL_ANY_RE = /https:\/\/(?:claude\.(?:ai|com)|console\.anthropic\.com)\//;
  var DONE_RE = /Login successful|Logged in|登入成功|Welcome back|Tips for getting started|\? for shortcuts/;

  var cols = 0;
  var rows = 0;
  var loginActive = false; // login screen currently on-screen
  var rawBuf = '';       // rolling buffer of decoded terminal output
  var captured = '';     // most recently reassembled login URL
  var oauthState = '';   // `state` param of the captured URL
  var codeSent = false;  // auth code already auto-submitted for this URL
  var clearSeen = false; // screen-clear arrived; re-evaluate visibility
  var activeWs = null;   // current ttyd WebSocket, for typing the code
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
      var m = line.match(URL_RE);
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

  function lastMatchIndex(text, re) {
    var g = new RegExp(re.source, 'g');
    var idx = -1;
    var m;
    while ((m = g.exec(text)) !== null) {
      idx = m.index;
      g.lastIndex = m.index + 1;
    }
    return idx;
  }

  function retire() {
    captured = '';
    oauthState = '';
    loginActive = false;
    hideBox();
  }

  function scan() {
    scanTimer = null;
    var lines = toLines(rawBuf);
    var text = lines.join('\n');
    // The login screen is "current" iff a login URL appears later in the
    // stream than the last post-login marker — ordering, not proximity, so
    // partial redraws can't confuse it.
    var lastUrl = lastMatchIndex(text, URL_ANY_RE);
    var lastDone = lastMatchIndex(text, DONE_RE);

    if (lastUrl >= 0 && lastUrl > lastDone) {
      var url = extractLoginUrl(lines, effectiveCols(lines));
      if (url && url !== captured) {
        captured = url;
        var sm = url.match(/[?&]state=([A-Za-z0-9_-]+)/);
        oauthState = sm ? sm[1] : '';
        codeSent = false;
      }
      if (captured) {
        loginActive = true;
        showBox(captured);
      }
      clearSeen = false;
      return;
    }
    if (captured && (lastDone > lastUrl || (clearSeen && lastUrl < 0))) {
      retire();
    }
    clearSeen = false;
  }

  function scheduleScan() {
    if (!scanTimer) scanTimer = setTimeout(scan, 150);
  }

  // --- auth code auto-submit ----------------------------------------------

  // The Claude callback page's "Copy code" yields "<code>#<state>"; the state
  // must match the captured URL's, so nothing else can ever be typed into
  // the terminal.
  function looksLikeAuthCode(t) {
    if (!t || !oauthState) return false;
    t = t.trim();
    var h = t.lastIndexOf('#');
    return h > 0 && t.length < 4096 && !/\s/.test(t) &&
      t.slice(h + 1) === oauthState;
  }

  function typeIntoTerminal(text) {
    if (!activeWs) return false;
    try {
      var payload = new TextEncoder().encode('0' + text);
      activeWs.send(payload);
      return true;
    } catch (e) { return false; }
  }

  function submitCode(t) {
    if (codeSent || !loginActive || !looksLikeAuthCode(t)) return false;
    if (typeIntoTerminal(t.trim() + '\r')) {
      codeSent = true;
      flashBox('✓ 驗證碼已自動送出 / Code submitted');
      return true;
    }
    return false;
  }

  function tryClipboard(manual) {
    if (codeSent || !captured || !oauthState) return;
    var nav = window.navigator;
    if (!nav || !nav.clipboard || !nav.clipboard.readText) return;
    nav.clipboard.readText().then(function (t) {
      if (!submitCode(t) && manual) {
        flashBox('剪貼簿沒有本次的驗證碼 / No matching code in clipboard', true);
      }
    }).catch(function () {
      if (manual) {
        flashBox('無法讀取剪貼簿，請直接貼進終端機 / Clipboard blocked — paste into the terminal', true);
      }
    });
  }

  // Returning to this tab after copying the code on the callback page.
  window.addEventListener('focus', function () { tryClipboard(false); });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) tryClipboard(false);
  });

  // --- overlay ------------------------------------------------------------

  function makeBtn(text) {
    var el = document.createElement('a');
    el.textContent = text;
    el.style.cssText =
      'color:#fff;text-decoration:none;font-weight:600;cursor:pointer;';
    return el;
  }

  function showBox(url) {
    var box = document.getElementById(BOX_ID);
    if (!box) {
      box = document.createElement('div');
      box.id = BOX_ID;
      box.style.cssText =
        'position:fixed;top:12px;right:12px;z-index:2147483647;display:flex;' +
        'align-items:center;gap:14px;background:#d97757;color:#fff;' +
        'padding:10px 14px;border-radius:10px;box-shadow:0 4px 14px rgba(0,0,0,.45);' +
        'font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;';

      var open = makeBtn('🔗 開啟 Claude 登入頁 / Open sign-in page');
      open.className = 'cllh-open';
      open.target = '_blank';
      open.rel = 'noopener noreferrer';

      var paste = makeBtn('📋 貼上驗證碼 / Paste code');
      paste.className = 'cllh-paste';
      paste.onclick = function () { tryClipboard(true); };

      var x = document.createElement('span');
      x.textContent = '✕';
      x.style.cssText = 'cursor:pointer;opacity:.75;padding:0 2px;';
      x.onclick = function () { hideBox(); };

      box.appendChild(open);
      box.appendChild(paste);
      box.appendChild(x);
      document.body.appendChild(box);
    }
    box.querySelector('.cllh-open').href = url;
    box.style.display = 'flex';
  }

  function hideBox() {
    var box = document.getElementById(BOX_ID);
    if (box) box.style.display = 'none';
  }

  function flashBox(msg, transientOnly) {
    var note = document.getElementById(BOX_ID + '-note');
    if (!note) {
      note = document.createElement('div');
      note.id = BOX_ID + '-note';
      note.style.cssText =
        'position:fixed;top:60px;right:12px;z-index:2147483647;' +
        'background:#2d7d46;color:#fff;padding:8px 14px;border-radius:10px;' +
        'box-shadow:0 4px 14px rgba(0,0,0,.45);' +
        'font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;';
      document.body.appendChild(note);
    }
    note.style.background = transientOnly ? '#a05a2c' : '#2d7d46';
    note.textContent = msg;
    note.style.display = 'block';
    clearTimeout(note._t);
    note._t = setTimeout(function () { note.style.display = 'none'; }, 5000);
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
    // Full screen clear / alt-screen switch: what came before is gone.
    if (/\x1b\[[23]J|\x1bc|\x1b\[\?1049[hl]/.test(chunk)) {
      rawBuf = '';
      clearSeen = true;
    }
    rawBuf += chunk;
    if (rawBuf.length > 65536) rawBuf = rawBuf.slice(-32768);
    scheduleScan();
  }

  function sniffSize(payload) {
    try {
      var s = typeof payload === 'string'
        ? payload
        : (payload && payload.byteLength > 0 && payload.byteLength < 4096
            ? new TextDecoder('utf-8').decode(payload)
            : '');
      // only auth ('{...}') and resize ('1{...}') frames, never input ('0...')
      if (!s || (s[0] !== '{' && s[0] !== '1')) return;
      var mc = s.match(/"columns":\s*(\d+)/);
      if (mc) cols = parseInt(mc[1], 10) || cols;
      var mr = s.match(/"rows":\s*(\d+)/);
      if (mr) rows = parseInt(mr[1], 10) || rows;
    } catch (e) { /* ignore */ }
  }

  var NativeWS = window.WebSocket;
  function PatchedWS(url, protocols) {
    var ws = protocols !== undefined ? new NativeWS(url, protocols) : new NativeWS(url);
    var nativeSend = ws.send.bind(ws);
    ws.send = function (d) {
      sniffSize(d instanceof Uint8Array ? d : (typeof d === 'string' ? d : ''));
      return nativeSend(d);
    };
    ws.addEventListener('message', function (ev) {
      try { onFrame(ev.data); } catch (e) { /* never break the client */ }
    });
    ws.addEventListener('close', function () {
      if (activeWs === ws) activeWs = null;
    });
    activeWs = ws;
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
