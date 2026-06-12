# DC-01 Report

## Changes Made
- Added `websockets` dependency to `packages/agent-server/pyproject.toml`.
- Implemented `HostPreviewProxyMiddleware` in `packages/agent-server/src/disco/agent_server/host_proxy.py`. The middleware intercepts HTTP and WebSocket requests where the `Host` header matches `^(?P<cid8>[0-9a-f]{8})-(?P<port>\d{2,5})\.localhost(?::\d+)?$`. It validates the port against `USER_PORTS`, resolves the `cid8`, and proxies the requests to the real upstream using `httpx.AsyncClient` for HTTP and `websockets` for WebSockets.
- Added `resolve_cid_prefix(cid8)` to `ConversationRuntime` in `packages/agent-server/src/disco/agent_server/runtime.py` to match the prefix against `_executors.keys()`.
- Wired the middleware into `app.py` after `CORSMiddleware`.
- Marked existing path-based preview proxy endpoints (`/conversations/{id}/preview-app/` and `/conversations/{id}/port/{port}/`) as deprecated.
- Implemented `previewHostUrl` in `frontend/src/api/client.ts` to construct the origin-true preview URLs.
- Updated `ExecutionCanvas.tsx` to use `previewHostUrl(cid, previewPort)` for the proxy URL.
- Added extensive HTTP and WebSocket tests in `packages/agent-server/tests/test_host_proxy.py`. Tested directly using HTTP and real Uvicorn/Websocket integrations.
- Extended frontend unit tests in `frontend/src/components/build/ExecutionCanvas.preview.test.tsx`.
- Wrote the live spec `frontend/e2e-live/dc-01-host-preview.spec.ts` matching the `bp-16` scenario.

## Test Evidence

### Frontend Unit Tests
```
 RUN  v3.2.4 /var/home/dylan/projects/disco build/frontend

 ✓ src/components/build/ExecutionCanvas.preview.test.tsx (6 tests) 85ms

 Test Files  1 passed (1)
      Tests  6 passed (6)
   Start at  19:04:14
   Duration  645ms (transform 84ms, setup 26ms, collect 144ms, tests 85ms, environment 165ms, prepare 46ms)
```

### Backend Integration Tests
```
uv run pytest packages/agent-server/tests/test_host_proxy.py -x -q
.........                                                                [100%]
=============================== warnings summary ===============================
packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/uvicorn/protocols/websockets/websockets_impl.py:17: DeprecationWarning: websockets.server.WebSocketServerProtocol is deprecated
    from websockets.server import WebSocketServerProtocol

packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/websockets/legacy/server.py:1178: DeprecationWarning: remove second argument of ws_handler
    warnings.warn("remove second argument of ws_handler", DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
9 passed, 3 warnings in 0.81s
```

## Deviations
- None. `websockets` was successfully utilized as a client dependency. `httpx.ASGITransport` was used for HTTP proxying, while a real `uvicorn` instance with `websockets` was set up to test the websocket pipeline, which uncovered standard proxy forwarding nuances cleanly.
- `previewHostUrl` was enhanced with a default `base` parameter in `frontend/src/api/client.ts` to allow it to be tested in the frontend unit tests without directly coupling to `import.meta.env` context since tests do not run with full env vars.

## Skipped Items
- The live E2E spec (`frontend/e2e-live/dc-01-host-preview.spec.ts`) was written but not executed, as per the order to leave this to the orchestrator.