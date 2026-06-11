# DC-01 — Origin-true preview proxy (hostname-per-conversation)

**Read `README.md` first. De-complexity Wave 0 order 1 (docs/decomplexity-wave-plan.md).**

## Why

The path-prefix proxies (`app.py` `preview_app` + `port_app`, lines ~404–445) forward
plain GETs under `/conversations/{cid}/preview-app/…` — every absolute asset path
(`/src/main.jsx`, `/@vite/client`), every `fetch('/api/…')`, and Vite's HMR websocket
escapes the prefix and 404s. Result: **DEFECT-3** — a blank preview for every real
dev-server app (test-record/marathon/DEFECTS.md). The fix is the Manus model scaled to
localhost: give each conversation+port its own ORIGIN via Host-header routing.

`http://{cid8}-{port}.localhost:8000/` reverse-proxies ALL methods + WebSocket to the
conversation's sandbox port, preserving origin-root semantics. No HTML rewriting, no
`<base>` injection, no fetch shim. `*.localhost` resolves to loopback in Firefox &
Chromium (RFC 6761) — **already verified live in Playwright Firefox** (both
`foo.localhost:8000` and `conv1234-8000.localhost:8000` returned 200 against a probe
server), so do not re-litigate resolution; just assert it in the live spec.

## The decided design (locked — do not redesign)

### New module: `packages/agent-server/src/perpleximanus/agent_server/host_proxy.py`

A **pure-ASGI middleware** class (NOT BaseHTTPMiddleware — it must see `websocket`
scopes and must stream):

```python
PREVIEW_HOST_RE = re.compile(r"^(?P<cid8>[0-9a-f]{8})-(?P<port>\d{2,5})\.localhost(?::\d+)?$")
# ONE constant; the RP share-link work swaps the suffix for a MagicDNS name later.
```

- `__init__(self, app, *, upstream_resolver)` — `upstream_resolver(cid8: str, port: int)
  -> str | None` is a callable injected from the app factory (closure over `runtime`),
  so the middleware never imports runtime and unit tests stub it trivially.
- `__call__`: scope type not in {"http", "websocket"} → passthrough. Extract `host`
  header; no match → passthrough (the ENTIRE existing app is untouched for normal
  hosts). On match:
  - `port not in USER_PORTS` → 404 `unknown port` (import USER_PORTS from
    `perpleximanus.tools.sandbox._container` like app.py does). 8899/8901 are not in
    USER_PORTS → stay non-routable, by construction.
  - `upstream_resolver(cid8, port)` returns None → 503 `preview not available`
    (mirrors the path-prefix proxies' semantics).
  - **HTTP**: stream-proxy with httpx. Build the upstream URL from the resolved
    upstream + the request's raw path + query string. Forward the method, body
    (stream the receive channel), and headers minus hop-by-hop
    (connection/keep-alive/transfer-encoding/upgrade/proxy-*) with `Host` rewritten
    to the upstream's host. Send with `client.send(req, stream=True)` and relay
    status + filtered response headers + body chunks (`aiter_raw`). Use ONE
    module-level `httpx.AsyncClient(timeout=httpx.Timeout(15, read=60))` created
    lazily — not per-request. `follow_redirects=False` (origin-true: let the browser
    see redirects). Upstream connect error → 502 `preview upstream unreachable`.
  - **WebSocket**: accept the client (forward the `sec-websocket-protocol` offer —
    Vite HMR negotiates the `vite-hmr` subprotocol and dies without it), connect
    upstream `ws://{upstream_host}:{port}{path}?{query}` via `websockets.connect`
    (add `websockets` to packages/agent-server/pyproject.toml — durable dep, not
    transitive-via-uvicorn), then two pump tasks (client→upstream, upstream→client,
    text AND bytes frames); first close/exception cancels the other and propagates a
    close code.

### `runtime.py` — cid8 resolution

```python
def resolve_cid_prefix(self, cid8: str) -> str | None:
    """Full conversation id whose uuid part starts with cid8 — live executors only
    (a preview without a live sandbox is a 503 anyway). Ambiguous (>1) → None."""
```

Conversation ids look like `conv_2d96fd06…` — match against `cid.removeprefix("conv_")`.
The app factory wires `upstream_resolver = lambda cid8, port: (cid := runtime.resolve_cid_prefix(cid8)) and runtime.port_upstream(cid, port)`
(guard `runtime is None` → resolver returns None).

### `app.py`

- `app.add_middleware(HostPreviewProxyMiddleware, upstream_resolver=…)` in the factory.
  (Starlette passes http AND websocket scopes through user middleware — confirmed fine.)
- The path-prefix routes (`preview_app`, `port_app`) STAY untouched, marked
  `# DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages`.

### Frontend

- `frontend/src/api/client.ts` — new helper:
  ```ts
  /** Origin-true preview URL (DC-01): http://{cid8}-{port}.localhost:8000/.
   * cid8 = first 8 chars of the uuid part. 127.0.0.1/localhost bases map to the
   * .localhost zone; any other base (future MagicDNS) gets the same {cid8}-{port}.
   * prefix on its hostname. Null when AGENT_BASE is unconfigured. */
  export function previewHostUrl(cid: string, port: number): string | null
  ```
  Implementation: `new URL(AGENT_BASE)`; hostname `127.0.0.1`|`localhost` →
  `${cid8}-${port}.localhost`, else `${cid8}-${port}.${hostname}`; keep protocol+port.
- `frontend/src/components/build/ExecutionCanvas.tsx` — `proxySrc` becomes
  `previewHostUrl(cid, previewPort) + `?r=${reloadKey}``  for BOTH the 8000 and
  extra-port cases (the `/port/{port}/` branch dies from the UI). Port pills, the
  Open link, Refresh/restart, and the rendered/live mode split all stay as-is —
  this order changes ONLY where the iframe points. Keep the iframe `sandbox` attr.

## What NOT to do

- No HTML/asset rewriting of any kind — if you find yourself parsing HTML, stop.
- Do not touch `_container.py`, the sandbox backends, or USER_PORTS membership.
- Do not remove the path-prefix routes or their tests.
- No new config knobs; the host pattern is one module constant.
- Do not auto-start or restart vite on :5173. Frontend tests: `npx vitest run <file>`
  from frontend/. Python tests: `uv run pytest packages/agent-server/tests/<file> -x -q`
  (agent-server package only — never mix core/tools test runs).

## Acceptance ladder (run + log everything; orchestrator runs the live rung)

1. **Unit/integration — `packages/agent-server/tests/test_host_proxy.py`** (NEW).
   Spin a REAL local upstream in the test (asyncio `aiohttp`-free: use
   `http.server`/`socketserver` in a thread, or an `asyncio.start_server` echo) on an
   ephemeral port; stub `upstream_resolver` to return it for cid8 `aaaaaaaa` port 8000.
   Use `httpx.ASGITransport(app=…)`-style in-process calls where possible; the WS
   bridge test may run uvicorn on an ephemeral loopback port. MUST cover:
   - GET passthrough: status, content-type, body bytes round-trip; query string reaches
     the upstream verbatim; path `/src/main.jsx` (absolute asset path) round-trips.
   - POST with a body → method + body reach the upstream (origin-true `fetch('/api/…')`).
   - Redirect from upstream (301 + Location) relayed UNTOUCHED to the client.
   - Unknown port (e.g. 9999) → 404; internal 8899 → 404 (allowlist truth table — make
     the forbidden outcome REACHABLE: the stub resolver must serve 8899 so the 404 is
     provably the middleware's own, not a dead upstream's).
   - Unknown cid8 / resolver None → 503; upstream down (resolved but connection
     refused) → 502.
   - Host header NOT matching the pattern → existing routes still work (hit a real
     app route through the middleware and assert it answers).
   - WebSocket: echo upstream; client sends text+binary through the bridge and gets
     them back; subprotocol offer forwarded (assert upstream saw `vite-hmr`).
   Log → `test-record/dc-01/integration-host-proxy.log`.
2. **Frontend units** — `frontend/src/components/build/ExecutionCanvas.preview.test.tsx`
   (extend) + a `previewHostUrl` unit block (cid8 extraction, 127.0.0.1→.localhost
   mapping, unconfigured→null). Run the touched files; log →
   `test-record/dc-01/frontend-units.log`.
3. **Live spec — `frontend/e2e-live/dc-01-host-preview.spec.ts`** (NEW; written by you,
   RUN by the orchestrator): drive a real conversation that builds the bp-16 frozen
   scenario shape (Vite+React app on :8000 reading `/api/readings` from :3000); assert
   the preview iframe's document contains REAL table cells with the CSV values — the
   exact `preview_cells` check that fails today. Pattern-match bp-15-screenshots.spec.ts
   for plumbing (getJson retry 5×30s, accept IDLE|PAUSED post-stop, tall viewport
   1280×2200 + element screenshots). Screenshots → test-record/screenshots/dc-01/.
4. **Report** — `agent-projects/gemini/dc-01-report.md`: what changed, test evidence
   with VERBATIM output snippets, deviations declared honestly, anything skipped
   flagged in its own section. Do NOT commit anything — the orchestrator commits.

## Manifest (orders.yaml `dc-01` — touch nothing outside it)

- packages/agent-server/src/perpleximanus/agent_server/host_proxy.py
- packages/agent-server/src/perpleximanus/agent_server/app.py
- packages/agent-server/src/perpleximanus/agent_server/runtime.py
- packages/agent-server/pyproject.toml
- packages/agent-server/tests/test_host_proxy.py
- frontend/src/api/client.ts
- frontend/src/components/build/ExecutionCanvas.tsx
- frontend/src/components/build/ExecutionCanvas.preview.test.tsx
- frontend/e2e-live/dc-01-host-preview.spec.ts
- test-record/dc-01/integration-host-proxy.log
- test-record/dc-01/frontend-units.log
- agent-projects/gemini/dc-01-report.md
