"""Standalone HTML for the read-only public share viewer.

Extracted verbatim from app.py (god-file decomposition, pure move). A single
large template string served by the share-viewer route; lives here so app.py
isn't carrying ~185 lines of embedded HTML.
"""

_SHARE_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Shared run — disco</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 14px/1.45 -apple-system, system-ui, sans-serif;
           max-width: 760px; margin: 2rem auto; padding: 0 1rem;
           color: #222; background: #fafafa; }
    @media (prefers-color-scheme: dark) {
      body { color: #eaeaea; background: #181818; }
    }
    h1 { font-size: 1.1rem; margin: 0 0 0.25rem; font-weight: 600; }
    .meta { color: #777; font-size: 0.85rem; margin-bottom: 1.5rem; }
    .item { border: 1px solid #ddd; border-radius: 6px;
            padding: 0.6rem 0.8rem; margin: 0.4rem 0; background: #fff; }
    @media (prefers-color-scheme: dark) {
      .item { background: #222; border-color: #333; }
    }
    .item.kind-action .label { font-weight: 600; }
    .item.kind-user { background: #f4f7ff; }
    .item.kind-system_warning { background: #fff7e6; }
    .item.kind-agent_message { font-style: italic; }
    .item .thought { white-space: pre-wrap; margin-top: 0.4rem;
                     color: #555; font-size: 0.9rem; }
    .item .detail { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                    font-size: 0.85rem; color: #555; margin-top: 0.25rem;
                    white-space: pre-wrap; word-break: break-word; }
    .err { color: #b00; font-size: 0.85rem; }
    .badge { display: inline-block; padding: 1px 6px; border-radius: 4px;
             font-size: 0.7rem; background: #eee; margin-right: 0.4rem; }
    @media (prefers-color-scheme: dark) {
      .badge { background: #333; }
    }
    .scrubber { position: sticky; top: 0; background: inherit;
                padding: 0.5rem 0; border-bottom: 1px solid #ddd;
                margin-bottom: 1rem; }
    .scrubber input[type=range] { width: 100%; }
    .scrubber .count { font-size: 0.8rem; color: #777; }
  </style>
</head>
<body>
  <div class="scrubber">
    <strong>Replay</strong>
    <span class="count" id="count"></span>
    <input type="range" id="scrub" min="0" max="0" value="0" step="1" />
  </div>
  <h1 id="title">Shared run</h1>
  <div class="meta" id="meta"></div>
  <div id="feed"></div>
  <script>
    // Tiny, dependency-free share viewer. Mirrors the live Build view's
    // "chat + actions" feed shape (user messages, agent prose, tool
    // calls, environment warnings). State and scrubber operate on the
    // BUNDLE — never on a live socket.
    (async () => {
      const params = new URLSearchParams(location.search);
      // The token is the FIRST path segment of the request URL. We could
      // read it server-side and pass it via a script-injected variable,
      // but the page is fully static and the token is the same one the
      // browser hit, so URL parsing is enough.
      const tokenMatch = location.pathname.match(/^\\/share\\/([A-Za-z0-9_-]+)/);
      if (!tokenMatch) {
        document.getElementById('feed').innerHTML =
          '<div class="err">Missing share token in URL.</div>';
        return;
      }
      const token = tokenMatch[1];
      let bundle;
      try {
        const r = await fetch('/api/share/' + encodeURIComponent(token) + '/bundle');
        if (!r.ok) {
          document.getElementById('feed').innerHTML =
            '<div class="err">Share link not found or has been revoked.</div>';
          return;
        }
        bundle = await r.json();
      } catch (e) {
        document.getElementById('feed').innerHTML =
          '<div class="err">Could not load bundle: ' + (e && e.message || e) + '</div>';
        return;
      }

      // Build the scrubber + feed. Pure DOM, no framework.
      const events = (bundle.events || []).slice().sort(
        (a, b) => (a.seq || 0) - (b.seq || 0)
      );
      const scrub = document.getElementById('scrub');
      const count = document.getElementById('count');
      const feed = document.getElementById('feed');
      const titleEl = document.getElementById('title');
      const metaEl = document.getElementById('meta');

      titleEl.textContent = bundle.title || ('Shared run ' + (bundle.conversation_id || ''));
      const metaBits = [
        'surface: ' + (bundle.surface || 'research'),
        'exported: ' + (bundle.exported_at || ''),
        'last_seq: ' + (bundle.last_seq || 0),
        'state: ' + ((bundle.state && bundle.state.execution_status) || '?'),
      ];
      if (bundle.share) {
        metaBits.push('issued: ' + (bundle.share.created_at || ''));
      }
      metaEl.textContent = metaBits.join('  •  ');

      scrub.max = events.length;
      count.textContent = ' step 0 / ' + events.length;

      function escape(s) {
        return String(s || '').replace(/[&<>"']/g, c => (
          { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
      }

      function renderFeed(upto) {
        const slice = events.slice(0, upto);
        if (slice.length === 0) {
          feed.innerHTML = '<div class="meta">No events yet.</div>';
          return;
        }
        const html = slice.map(ev => {
          const k = ev.kind;
          if (k === 'message') {
            const src = ev.source || 'agent';
            const content = (ev.message && ev.message.content) || '';
            if (src === 'user') {
              return '<div class="item kind-user"><span class="badge">user</span>'
                + escape(content) + '</div>';
            } else if (src === 'environment') {
              if (content.startsWith('\u26a0')) {
                return '<div class="item kind-system_warning">'
                  + escape(content) + '</div>';
              }
              return '';
            } else {
              return '<div class="item kind-agent_message">'
                + escape(content) + '</div>';
            }
          } else if (k === 'action') {
            const tc = ev.tool_call || {};
            const args = JSON.stringify(tc.arguments || {});
            return '<div class="item kind-action">'
              + '<span class="badge">action</span>'
              + '<span class="label">' + escape(tc.tool_name || '?') + '</span>'
              + '<div class="detail">' + escape(args) + '</div>'
              + (ev.thought
                  ? '<div class="thought">' + escape(ev.thought) + '</div>'
                  : '')
              + '</div>';
          } else if (k === 'observation') {
            const tr = ev.tool_result || {};
            return '<div class="item"><span class="badge">observation</span>'
              + escape(tr.tool_name || '?') + ': '
              + escape((tr.content || '').slice(0, 200))
              + (tr.content && tr.content.length > 200 ? ' \u2026' : '')
              + '</div>';
          } else if (k === 'agent_error') {
            return '<div class="item"><span class="badge">error</span>'
              + escape(ev.error || '') + '</div>';
          } else if (k === 'plan') {
            return '<div class="item"><span class="badge">plan</span>'
              + escape(ev.summary || '') + '</div>';
          } else if (k === 'status') {
            return '<div class="item"><span class="badge">status</span>'
              + escape(ev.status || '') + '</div>';
          }
          return '';
        }).join('');
        feed.innerHTML = html;
      }

      scrub.addEventListener('input', () => {
        const n = parseInt(scrub.value, 10) || 0;
        count.textContent = ' step ' + n + ' / ' + events.length;
        renderFeed(n);
      });
      renderFeed(events.length);  // start at the end (the "show full" default)
    })();
  </script>
</body>
</html>"""
