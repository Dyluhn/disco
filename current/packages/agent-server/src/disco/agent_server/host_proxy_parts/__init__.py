"""Cohesive private implementation parts extracted from ``host_proxy``.

``host_proxy.py`` remains the sole public compatibility/export facade
(``HostPreviewProxyMiddleware``, ``make_preview_session_resolver``, and the
private names its own test suite imports directly) and re-imports what it
needs from these modules. Nothing here is part of the public API; external
callers must never import from ``host_proxy_parts`` directly.

Every function below that needs the live ``HostPreviewProxyMiddleware``
instance takes it as an explicit first parameter (named ``self`` or ``proxy``)
rather than being a method — the same convention ``host_proxy.py`` already
used, pre-split, for ``_forward_http_response``/``_proxy_http``/
``_proxy_websocket``/``_complete_storage_handoff``. Where a name is resolved
through the parent module at call time (``from disco.agent_server import
host_proxy`` then ``host_proxy.NAME``) rather than imported directly, that is
deliberate: the test suite monkeypatches that name on the ``host_proxy``
module object, and a direct import would bind the pre-patch function object.

Modules:
- :mod:`_headers`          — cookie/response-header hygiene helpers.
- :mod:`_redirects`        — canonical-upstream Location rewriting.
- :mod:`_request_checks`   — WS origin / service-worker / capability lookup.
- :mod:`_responses`        — no-store wrapping + the p3s static-route denial.
- :mod:`_route_resolution` — host/port -> (cid8, port, family) + the
  port/service-worker/static-route/ws-origin admission gate.
- :mod:`_capability`       — capability verification + authority checks +
  the canonical internal-rewrite dispatch + immutable-preview read-only gate.
- :mod:`_bootstrap`        — the preview-bootstrap POST flow (redemption +
  storage handoff).
- :mod:`_forwarding`       — HTTP request/response proxying to the upstream
  dev server, including the in-sandbox liveness fallback.
- :mod:`_websocket`        — WebSocket proxying to the upstream dev server.
"""

from __future__ import annotations
