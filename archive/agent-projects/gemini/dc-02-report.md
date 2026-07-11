# DC-02 Report

## What Changed

### 1. `runtime.py` - FINISHED no longer reaps immediately
- Removed the immediate `_teardown_sandbox` call within the `_ENDED` block of `_run_with_persistence`.
- FINISHED conversations now stay active until they are suspended by the idle sweep.

### 2. `sandbox.idle_ttl_s` Configuration
- Added `idle_ttl_s: int = 1800` to `SandboxSettings` in `packages/core/src/disco/core/llm/config.py`.
- Updated `sweep_idle_once()` to respect `PMX_IDLE_SUSPEND_S` as an override, falling back to `sandbox.idle_ttl_s`.

### 3. Wake-on-preview-hit
- Added `wake_for_preview` to `runtime.py`, guarding `ensure_preview` with a per-cid `asyncio.Lock`.
- It checks if a live executor exists via `resolve_cid_prefix(cid8)`, else it resolves the full CID from the store and calls `ensure_preview`.
- Updated `_preview_upstream_resolver` in `app.py` to be `async` and await `runtime.wake_for_preview(cid8, port)`.
- Updated `HostPreviewProxyMiddleware` in `host_proxy.py` to correctly await the resolver if it returns an awaitable.

### 4. Tests
- Created `packages/agent-server/tests/test_idle_suspend.py` to assert the new behaviors including the lack of immediate reap, the sweep suspending past TTL, precedence logic, and the `wake_for_preview` functionality properly guarded by the lock.
- Added `test_async_resolver_awaited` in `packages/agent-server/tests/test_host_proxy.py` to prove the middleware awaits an async upstream resolver.
- Evaluated existing tests in `test_lifecycle.py` and `test_override_persistence.py`. `test_override_persistence.py` still correctly asserts the auto-suspend functionality without needing manual flip changes since it already called `_suspend()` directly, matching the exact requirements. The flip impact was solely tested by the missing call in the `_run_with_persistence` function now proven by the `test_idle_suspend.py` test suite.

## Verbatim Test Output
```text
........................................................................ [ 42%]
........................................................................ [ 84%]
..........................                                               [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/uvicorn/protocols/websockets/websockets_impl.py:17: DeprecationWarning: websockets.server.WebSocketServerProtocol is deprecated
    from websockets.server import WebSocketServerProtocol

packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
packages/agent-server/tests/test_host_proxy.py::test_websocket_proxy
  /var/home/dylan/projects/disco build/.venv/lib/python3.13/site-packages/websockets/legacy/server.py:1178: DeprecationWarning: remove second argument of ws_handler
    warnings.warn("remove second argument of ws_handler", DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
```

## Deviations
None. The implementation follows the design precisely. Existing tests for lifecycle auto-suspend were calling internal methods (e.g., `_suspend`, `_teardown_sandbox`) rather than testing the end-to-end `_run` teardown, so they continued passing naturally, while the new `test_idle_suspend.py` covers the exact new behaviors.