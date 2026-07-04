/* disco element mention picker - canonical package asset.
 *
 * Self-contained IIFE injected into preview HTML. It is inert until the host
 * sends {type:"disco-element-mention:arm", nonce}. While armed it highlights
 * hovered elements and posts one structured element mention to window.parent.
 * No eval, no network fetches, no dependencies.
 */
(function () {
  'use strict';

  var armed = false;
  var nonce = '';
  var hover = null;
  var ring = null;

  function ensureRing() {
    if (ring) return ring;
    var el = document.createElement('div');
    el.setAttribute('id', '__disco_element_mention_ring__');
    el.setAttribute('aria-hidden', 'true');
    el.style.cssText = [
      'position:fixed',
      'pointer-events:none',
      'z-index:2147483647',
      'border:2px solid #10b981',
      'border-radius:3px',
      'background:rgba(16,185,129,0.08)',
      'box-sizing:border-box',
      'transition:top 60ms ease,left 60ms ease,width 60ms ease,height 60ms ease',
      'display:none',
    ].join(';');
    document.documentElement.appendChild(el);
    ring = el;
    return el;
  }

  function hideRing() {
    if (ring) ring.style.display = 'none';
  }

  function placeRing(el) {
    var r = el.getBoundingClientRect();
    var box = ensureRing();
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = r.width + 'px';
    box.style.height = r.height + 'px';
    box.style.display = 'block';
  }

  function collapseText(value, limit) {
    return String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  }

  function segmentFor(el) {
    var tag = (el.tagName || 'unknown').toLowerCase();
    var id = el.id ? '#' + String(el.id).replace(/\s+/g, '') : '';
    var classes = Array.prototype.slice.call(el.classList || [], 0, 4)
      .map(function (c) { return String(c).replace(/\s+/g, ''); })
      .filter(Boolean)
      .join('.');
    return tag + id + (classes ? '.' + classes : '');
  }

  function domPathFor(el) {
    var out = [];
    var cur = el;
    while (cur && cur.nodeType === 1 && out.length < 8) {
      out.push(segmentFor(cur));
      cur = cur.parentElement;
    }
    return out;
  }

  function screenLabelFor(el) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      var label =
        cur.getAttribute('data-screen-label') ||
        cur.getAttribute('data-disco-screen-label');
      if (label) return collapseText(label, 120);
      cur = cur.parentElement;
    }
    return null;
  }

  function reactNameFromFiber(fiber) {
    var cur = fiber;
    var guard = 0;
    while (cur && guard < 12) {
      var t = cur.elementType || cur.type;
      if (typeof t === 'function') return t.displayName || t.name || null;
      if (t && typeof t === 'object') {
        if (typeof t.displayName === 'string') return t.displayName;
        if (typeof t.name === 'string') return t.name;
        if (typeof t.render === 'function') {
          return t.render.displayName || t.render.name || null;
        }
      }
      cur = cur.return;
      guard += 1;
    }
    return null;
  }

  function reactNameFor(el) {
    var keys = Object.keys(el);
    for (var i = 0; i < keys.length; i += 1) {
      if (keys[i].indexOf('__reactFiber$') === 0) {
        return reactNameFromFiber(el[keys[i]]);
      }
    }
    return null;
  }

  function reactPathFor(el) {
    var out = [];
    var cur = el;
    while (cur && cur.nodeType === 1 && out.length < 8) {
      var name = reactNameFor(cur);
      if (name && out[out.length - 1] !== name) out.push(name);
      cur = cur.parentElement;
    }
    return out;
  }

  function hrefFor(el) {
    var anchor = el.closest ? el.closest('a[href]') : null;
    if (!anchor) return null;
    return anchor.href || anchor.getAttribute('href') || null;
  }

  function srcFor(el) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      if (cur.getAttribute && cur.getAttribute('src')) {
        return cur.currentSrc || cur.src || cur.getAttribute('src');
      }
      cur = cur.parentElement;
    }
    return null;
  }

  function payloadFor(el) {
    var r = el.getBoundingClientRect();
    var payload = {
      domPath: domPathFor(el),
      screenLabel: screenLabelFor(el),
      text: collapseText(el.textContent || '', 120),
      rect: { x: r.left, y: r.top, w: r.width, h: r.height },
    };
    var reactPath = reactPathFor(el);
    if (reactPath.length) payload.reactPath = reactPath;
    var href = hrefFor(el);
    if (href) payload.href = String(href).slice(0, 500);
    var src = srcFor(el);
    if (src) payload.src = String(src).slice(0, 500);
    return payload;
  }

  function pickElement(e) {
    if (!armed) return;
    e.preventDefault();
    e.stopPropagation();
    var el = e.target && e.target.nodeType === 1 ? e.target : null;
    if (!el || el === ring || el === document.documentElement) return;
    placeRing(el);
    window.parent.postMessage({
      type: 'disco-element-mention',
      nonce: nonce,
      payload: payloadFor(el),
    }, '*');
    disarm();
  }

  function move(e) {
    if (!armed) return;
    var el = document.elementFromPoint(e.clientX, e.clientY);
    if (!el || el === ring || el === hover || el === document.documentElement) return;
    hover = el;
    placeRing(el);
  }

  function arm(nextNonce) {
    if (armed) disarm();
    nonce = nextNonce;
    armed = true;
    hover = null;
    document.addEventListener('mousemove', move, true);
    document.addEventListener('click', pickElement, true);
  }

  function disarm() {
    armed = false;
    nonce = '';
    hover = null;
    hideRing();
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('click', pickElement, true);
  }

  window.addEventListener('message', function (e) {
    if (e.source !== window.parent) return;
    var d = e.data;
    if (!d || typeof d !== 'object') return;
    if (d.type === 'disco-element-mention:arm' && typeof d.nonce === 'string') {
      arm(d.nonce);
      return;
    }
    if (d.type === 'disco-element-mention:disarm') {
      disarm();
    }
  });
})();
