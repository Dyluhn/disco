"""WALK-10: preview-app and port-app routes must wake suspended sandboxes.

Root cause (confirmed): preview_app called runtime.preview_upstream which only
looks up a live in-memory executor → 503 "preview not available" after the run
ends and the sandbox auto-suspends.  The embedded Preview tab works because
PreviewPane's iframe uses the HostPreviewProxyMiddleware which calls
wake_for_preview.

Fix: preview_app and port_app now extract cid8 from the conversation_id and call
await runtime.wake_for_preview(cid8, port), which rematerialises a suspended
sandbox before proxying — same path as the hostname proxy.

Tests prove:
  - wake_for_preview is called (not preview_upstream) after the fix;
  - the cid8 and port arguments are correct;
  - 503 when wake returns None (sandbox truly unavailable);
  - 502 when upstream is alive but unreachable (dead upstream);
  - port_app respects the USER_PORTS allowlist before waking;
  - runtime=None → 503 without error (no AttributeError on None).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from disco.agent_server.routes.preview import make_preview_router
from disco.core import SqliteEventStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fake runtime — tracks wake_for_preview calls; preview_upstream NOT present
# (its absence would raise AttributeError if the old code ran, proving the fix)
# ---------------------------------------------------------------------------


class FakeRuntime:
    def __init__(self, wake_result: str | None = None) -> None:
        self.wake_calls: list[tuple[str, int]] = []
        self._wake_result = wake_result

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        self.wake_calls.append((cid8, port))
        return self._wake_result

    async def preview(self, conversation_id: str) -> dict:
        return {"available": False, "reason": "stub"}

    async def ensure_preview(self, conversation_id: str) -> bool:
        return False


def _make_client(runtime: FakeRuntime) -> tuple[TestClient, FakeRuntime]:
    store = SqliteEventStore(":memory:")
    app = FastAPI()
    # FakeRuntime satisfies the duck-type contract the routes require;
    # the type annotation is ConversationRuntime but only the methods above
    # are called within preview_app / port_app.
    app.include_router(make_preview_router(store, runtime))  # type: ignore[arg-type]
    return TestClient(app), runtime


# ---------------------------------------------------------------------------
# Minimal local HTTP server for proxy tests
# ---------------------------------------------------------------------------


class _FixedResponseHandler(BaseHTTPRequestHandler):
    body = b"hello from upstream"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, fmt: str, *args: object) -> None:  # silence HTTP log
        pass


def _start_upstream() -> tuple[str, HTTPServer]:
    server = HTTPServer(("127.0.0.1", 0), _FixedResponseHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", server


# ---------------------------------------------------------------------------
# preview-app route tests (WALK-10)
# ---------------------------------------------------------------------------


def test_preview_app_calls_wake_for_preview_with_correct_cid8() -> None:
    """FAILS before fix: old code calls preview_upstream (not present on FakeRuntime
    → AttributeError).  Passes after fix: wake_for_preview is called with cid8."""
    rt = FakeRuntime(wake_result=None)
    client, _ = _make_client(rt)
    resp = client.get("/conversations/conv_abc12345deadbeef/preview-app/")
    assert resp.status_code == 503
    assert len(rt.wake_calls) == 1
    cid8, port = rt.wake_calls[0]
    assert cid8 == "abc12345"  # first 8 chars after stripping "conv_"
    assert port == 8000  # PREVIEW_PORT


def test_preview_app_503_when_wake_returns_none() -> None:
    """Sandbox not wakeable → 503 with the same 'preview not available' message."""
    rt = FakeRuntime(wake_result=None)
    client, _ = _make_client(rt)
    resp = client.get("/conversations/conv_dead0000/preview-app/")
    assert resp.status_code == 503
    assert b"preview not available" in resp.content


def test_preview_app_proxies_when_upstream_is_live() -> None:
    """When wake_for_preview returns a live upstream, the response is proxied."""
    upstream_url, server = _start_upstream()
    try:
        rt = FakeRuntime(wake_result=upstream_url)
        client, _ = _make_client(rt)
        resp = client.get("/conversations/conv_alive001/preview-app/")
        assert resp.status_code == 200
        assert b"hello from upstream" in resp.content
        assert rt.wake_calls[0][0] == "alive001"
    finally:
        server.shutdown()


def test_preview_app_proxies_subpath() -> None:
    """Sub-path is forwarded to the upstream."""
    upstream_url, server = _start_upstream()
    try:
        rt = FakeRuntime(wake_result=upstream_url)
        client, _ = _make_client(rt)
        resp = client.get("/conversations/conv_sub00001/preview-app/index.html")
        assert resp.status_code == 200
        assert rt.wake_calls[0][0] == "sub00001"
    finally:
        server.shutdown()


def test_preview_app_502_when_upstream_unreachable() -> None:
    """Upstream URL known but server is down → 502 (not 503)."""
    rt = FakeRuntime(wake_result="http://127.0.0.1:59998")  # nothing bound there
    client, _ = _make_client(rt)
    resp = client.get("/conversations/conv_noserver1/preview-app/")
    assert resp.status_code == 502
    assert b"preview upstream unreachable" in resp.content


def test_preview_app_503_when_runtime_is_none() -> None:
    """runtime=None → 503 immediately (no AttributeError)."""
    store = SqliteEventStore(":memory:")
    app = FastAPI()
    app.include_router(make_preview_router(store, None))
    client = TestClient(app)
    resp = client.get("/conversations/conv_noruntime/preview-app/")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# port-app route tests (WALK-10)
# ---------------------------------------------------------------------------


def test_port_app_unknown_port_404_before_wake() -> None:
    """A non-USER port must 404 without ever calling wake_for_preview."""
    rt = FakeRuntime(wake_result=None)
    client, _ = _make_client(rt)
    resp = client.get("/conversations/conv_abc12345/port/9999/")
    assert resp.status_code == 404
    assert rt.wake_calls == []  # wake must NOT have been called


def test_port_app_user_port_calls_wake_for_preview() -> None:
    """A curated USER port (3000) must call wake_for_preview with that port."""
    rt = FakeRuntime(wake_result=None)
    client, _ = _make_client(rt)
    resp = client.get("/conversations/conv_porttest1/port/3000/")
    assert resp.status_code == 503
    assert len(rt.wake_calls) == 1
    cid8, port = rt.wake_calls[0]
    assert cid8 == "porttest"
    assert port == 3000


def test_port_app_proxies_when_upstream_is_live() -> None:
    """Curated port with a live upstream is proxied correctly."""
    upstream_url, server = _start_upstream()
    try:
        rt = FakeRuntime(wake_result=upstream_url)
        client, _ = _make_client(rt)
        # "conv_portlive1" → strip "conv_" → "portlive1" → [:8] = "portlive"
        resp = client.get("/conversations/conv_portlive1/port/5173/")
        assert resp.status_code == 200
        assert b"hello from upstream" in resp.content
        assert rt.wake_calls[0] == ("portlive", 5173)
    finally:
        server.shutdown()
