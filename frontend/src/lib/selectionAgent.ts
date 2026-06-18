/**
 * In-frame selection agent — injected as a `<script>` into preview iframes.
 *
 * The script is a self-contained IIFE that:
 *  1. Listens for host→frame commands (arm / disarm / walkup).
 *  2. On arm: installs mousemove + click interceptors and draws a hover ring.
 *  3. On click: captures the bounding rect + resolver attributes and postMessages
 *     a `disco:selection` envelope to the parent host window.
 *  4. On walkup: re-targets to the current element's parentElement.
 *  5. On disarm: removes all handlers and hides the ring.
 *
 * Security inside the frame:
 *  - Validates incoming commands: `e.source === window.parent` + `data.v === 1`
 *    + `data.channel === "disco-select"`.
 *  - On arm, stores the session nonce; all outgoing frames carry it so the host
 *    can reject replays.
 *  - Outgoing postMessages target `"*"` because the parent may be on a different
 *    origin from the sandboxed frame.
 *
 * Clean-room reimplementation of the Onlook (Apache-2.0) hover-ring + selector
 * behaviour. No upstream code is transcribed.
 *
 * @module selectionAgent
 */

/**
 * JavaScript IIFE source, exported as a plain string for injection into
 * `<script>` tags via `deriveSrcDoc` (srcdoc path) or the `preview.py` proxy
 * response rewrite (live-proxy path).
 */
export const SELECTION_AGENT_SCRIPT: string = `
(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────────────────── */
  var _armed   = false;
  var _nonce   = null;
  var _hover   = null;   // element currently under the cursor
  var _sel     = null;   // most recently clicked element
  var _ring    = null;   // the absolutely-positioned hover ring div

  /* ── Ring ────────────────────────────────────────────────────────────────── */

  function ensureRing() {
    if (_ring) return _ring;
    var el = document.createElement('div');
    el.setAttribute('id', '__disco_selection_ring__');
    el.setAttribute('aria-hidden', 'true');
    el.style.cssText = [
      'position:fixed',
      'pointer-events:none',
      'z-index:2147483647',
      'border:2px solid #6366f1',
      'border-radius:3px',
      'background:rgba(99,102,241,0.07)',
      'box-sizing:border-box',
      'transition:top 60ms ease,left 60ms ease,width 60ms ease,height 60ms ease',
      'display:none',
    ].join(';');
    document.documentElement.appendChild(el);
    _ring = el;
    return el;
  }

  function placeRing(el) {
    var r = el.getBoundingClientRect();
    var ring = ensureRing();
    ring.style.left   = r.left   + 'px';
    ring.style.top    = r.top    + 'px';
    ring.style.width  = r.width  + 'px';
    ring.style.height = r.height + 'px';
    ring.style.display = 'block';
  }

  function hideRing() {
    if (_ring) _ring.style.display = 'none';
  }

  /* ── Resolver attributes ─────────────────────────────────────────────────── */

  /**
   * Walk up the DOM from el to find the nearest resolver attribute.
   * Returns a DeckRef (data-element-id + data-slide-id) or SourceRef
   * (data-oid), or a generic SourceRef with empty fields when neither is found.
   */
  function resolveRef(el) {
    var cur = el;
    while (cur && cur !== document.documentElement) {
      var eid = cur.getAttribute && cur.getAttribute('data-element-id');
      var sid = cur.getAttribute && cur.getAttribute('data-slide-id');
      if (eid && sid) {
        return { kind: 'deck', slide_id: sid, element_id: eid };
      }
      var oid = cur.getAttribute && cur.getAttribute('data-oid');
      if (oid) {
        var parts = oid.split(':');
        return {
          kind: 'source',
          oid:  oid,
          file: parts[0] || '',
          line: parseInt(parts[1], 10) || 0,
        };
      }
      cur = cur.parentElement;
    }
    return { kind: 'source', oid: '', file: '', line: 0 };
  }

  function humanLabel(el) {
    var text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    var tag  = (el.tagName  || 'unknown').toLowerCase();
    var cls  = Array.prototype.slice.call(el.classList || [], 0, 2).join('.');
    var base = tag + (cls ? '.' + cls : '');
    return text ? base + ' \\u2014 \\u201c' + text + '\\u201d' : base;
  }

  /* ── postMessage helpers ─────────────────────────────────────────────────── */

  function emit(payload) {
    window.parent.postMessage(payload, '*');
  }

  function sendSelection(el) {
    var r = el.getBoundingClientRect();
    emit({
      v: 1,
      channel: 'disco-select',
      nonce: _nonce,
      type: 'disco:selection',
      selection_ref: resolveRef(el),
      human_label: humanLabel(el),
      rect: { x: r.left, y: r.top, width: r.width, height: r.height },
    });
  }

  function sendHover(el) {
    var r = el.getBoundingClientRect();
    emit({
      v: 1,
      channel: 'disco-select',
      nonce: _nonce,
      type: 'disco:hover',
      rect: { x: r.left, y: r.top, width: r.width, height: r.height },
    });
  }

  /* ── Event handlers ──────────────────────────────────────────────────────── */

  function onMouseMove(e) {
    if (!_armed) return;
    var el = document.elementFromPoint(e.clientX, e.clientY);
    if (!el || el === _ring) return;
    if (el !== _hover) {
      _hover = el;
      placeRing(el);
      sendHover(el);
    }
  }

  function onClick(e) {
    if (!_armed) return;
    e.preventDefault();
    e.stopPropagation();
    var el = e.target;
    if (!el || el === _ring) return;
    _sel = el;
    placeRing(el);
    sendSelection(el);
  }

  /* ── Arm / disarm ────────────────────────────────────────────────────────── */

  function arm(nonce) {
    if (_armed) disarm();          // clean up previous session if re-armed
    _nonce = nonce;
    _armed = true;
    document.addEventListener('mousemove', onMouseMove, true);
    document.addEventListener('click',     onClick,     true);
  }

  function disarm() {
    _armed = false;
    _nonce = null;
    _hover = null;
    _sel   = null;
    hideRing();
    document.removeEventListener('mousemove', onMouseMove, true);
    document.removeEventListener('click',     onClick,     true);
  }

  function walkUp() {
    if (!_armed || !_sel) return;
    var parent = _sel.parentElement;
    if (!parent || parent === document.body || parent === document.documentElement) return;
    _sel = parent;
    placeRing(_sel);
    sendSelection(_sel);
  }

  /* ── Host → frame command listener ──────────────────────────────────────── */

  window.addEventListener('message', function (e) {
    /* Validate the message is from our direct host frame */
    if (e.source !== window.parent) return;
    var d = e.data;
    if (!d || typeof d !== 'object') return;
    if (d.v !== 1 || d.channel !== 'disco-select') return;

    if (d.type === 'disco:overlay:arm'    && typeof d.nonce === 'string') { arm(d.nonce); return; }
    if (d.type === 'disco:overlay:disarm')                                { disarm();     return; }
    if (d.type === 'disco:overlay:walkup')                                { walkUp();     return; }
  });

})();
`;
