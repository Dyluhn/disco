/* disco in-frame selection agent — CANONICAL SOURCE (single source of truth).
 *
 * This file is the ONE place the selection-agent IIFE lives. It is consumed by:
 *   - the frontend (current/frontend/src/lib/selectionAgent.ts imports it via Vite `?raw`
 *     and re-exports it as SELECTION_AGENT_SCRIPT), and
 *   - the agent-server preview-edit route (routes/preview_edit.py reads it via
 *     importlib.resources as a co-located package asset, mirroring reference.docx).
 *
 * It is shipped as a package data file alongside the disco.agent_server module so
 * an installed package can still find it (importlib.resources), exactly like the
 * bundled reference.docx. Editing the script here updates BOTH consumers — there
 * is no second copy to keep in sync.
 *
 * Self-contained IIFE that:
 *  1. Listens for host->frame commands (arm / disarm / walkup).
 *  2. On arm: installs mousemove + click interceptors and draws a hover ring.
 *  3. On click: captures the bounding rect + resolver attributes and postMessages
 *     a `disco:selection` envelope to the parent host window.
 *  4. On walkup: re-targets to the current element's parentElement.
 *  5. On disarm: removes all handlers and hides the ring.
 *
 * Clean-room reimplementation of the Onlook (Apache-2.0) hover-ring + selector
 * behaviour. No upstream code is transcribed.
 */
(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────────────────── */
  var _armed   = false;
  var _nonce   = null;
  var _hover   = null;   // element currently under the cursor
  var _sel     = null;   // most recently clicked element
  var _ring    = null;   // the absolutely-positioned hover ring div

  /* The host owns static refreshes. Report the real in-frame route so a reload
   * preserves navigation instead of reminting the application root. This is
   * observational only: the page retains complete routing authority. */
  function reportLocation() {
    window.parent.postMessage({
      v: 1,
      channel: 'disco-preview',
      type: 'location',
      path: window.location.pathname + window.location.search + window.location.hash,
    }, '*');
  }

  ['pushState', 'replaceState'].forEach(function (name) {
    var original = window.history[name];
    if (typeof original !== 'function') return;
    window.history[name] = function () {
      var result = original.apply(window.history, arguments);
      reportLocation();
      return result;
    };
  });
  window.addEventListener('popstate', reportLocation);
  window.addEventListener('hashchange', reportLocation);
  reportLocation();

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
   * Walk up the DOM from el to resolve a typed target ref. Priority:
   *   deck (data-element-id + data-slide-id) > semantic (data-disco-* — AppKit,
   *   P8) > source (data-oid) > empty source.
   * The semantic anchor is gathered ACROSS the walk-up because a field
   * (data-disco-field) and its owning section (data-disco-section) sit on
   * different elements — first-seen wins per attribute.
   */
  function attrOf(cur, name) {
    return (cur && cur.getAttribute) ? cur.getAttribute(name) : null;
  }

  function resolveRef(el) {
    var cur = el;
    // first-seen semantic attrs (a field is nested inside its section)
    var dSection = null, dField = null, dCollection = null, dIndex = null, dScreen = null;
    var dOid = null;
    while (cur && cur !== document.documentElement) {
      var eid = attrOf(cur, 'data-element-id');
      var sid = attrOf(cur, 'data-slide-id');
      if (eid && sid) {
        return { kind: 'deck', slide_id: sid, element_id: eid };
      }
      if (dSection == null)    dSection    = attrOf(cur, 'data-disco-section');
      if (dField == null)      dField      = attrOf(cur, 'data-disco-field');
      if (dCollection == null) dCollection = attrOf(cur, 'data-disco-collection');
      if (dIndex == null)      dIndex      = attrOf(cur, 'data-disco-index');
      if (dScreen == null)     dScreen     = attrOf(cur, 'data-disco-screen-label');
      if (dOid == null)        dOid        = attrOf(cur, 'data-oid');
      cur = cur.parentElement;
    }
    if (dSection) {
      var ref = { kind: 'semantic', section_id: dSection };
      if (dField)   ref.field_id = dField;
      if (dCollection) ref.collection_id = dCollection;
      if (dIndex != null && dIndex !== '') {
        var idx = parseInt(dIndex, 10);
        if (idx >= 0) ref.index = idx;
      }
      if (dScreen)  ref.screen_label = dScreen;
      return ref;
    }
    if (dOid) {
      var parts = dOid.split(':');
      return {
        kind: 'source',
        oid:  dOid,
        file: parts[0] || '',
        line: parseInt(parts[1], 10) || 0,
      };
    }
    return { kind: 'source', oid: '', file: '', line: 0 };
  }

  function humanLabel(el) {
    var text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    var tag  = (el.tagName  || 'unknown').toLowerCase();
    var cls  = Array.prototype.slice.call(el.classList || [], 0, 2).join('.');
    var base = tag + (cls ? '.' + cls : '');
    return text ? base + ' — “' + text + '”' : base;
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
