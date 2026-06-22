"""Fix 2 (B-E + codex P1) — preview reachability via the in-sandbox liveness proxy.

On sealed/filtered backends `wake_for_preview` resolves None (no host port is
published) even while a dev server is live inside the box. These tests prove:
  (a) GET /preview-app/ returns 200 + the live body via session.fetch_inside when
      no host upstream exists but a live session is serving (liveness-gated, NOT
      publish-gated);
  (b) honest 503 when nothing is listening (fetch_inside None + no snapshot);
  (e) the canonical HOSTNAME proxy (HostPreviewProxyMiddleware) falls back to
      fetch_inside on a no-upstream + live-session box (the codex P1 coverage).
"""

from __future__ import annotations

from disco.agent_server.host_proxy import HostPreviewProxyMiddleware
from disco.agent_server.routes.preview import make_preview_router
from disco.core import SqliteEventStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse


class _FakeSession:
    """Live SandboxSession stand-in: fetch_inside returns a configured frame."""

    def __init__(self, result: tuple[int, bytes, str] | None) -> None:
        self._result = result
        self.calls: list[tuple[int, str]] = []

    async def fetch_inside(
        self, port: int, path: str, *, timeout_s: int = 10
    ) -> tuple[int, bytes, str] | None:
        self.calls.append((port, path))
        return self._result


class _FakeRuntime:
    """No host upstream is ever published (sealed box); a live session may serve."""

    def __init__(self, session: _FakeSession | None) -> None:
        self._session = session
        self.wake_calls: list[tuple[str, int]] = []

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        self.wake_calls.append((cid8, port))
        return None  # sealed/filtered: no published host port

    def live_session(self, conversation_id: str):
        return self._session

    def project_store(self):
        return None  # no host snapshot → the only channel is fetch_inside

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        return f"conv_{cid8}"


def _client(runtime: _FakeRuntime) -> TestClient:
    store = SqliteEventStore(":memory:")
    app = FastAPI()
    app.include_router(make_preview_router(store, runtime))  # type: ignore[arg-type]
    return TestClient(app)


# ---- (a) sealed box mid-run: liveness-gated 200 -------------------------------


def test_preview_app_serves_live_body_via_fetch_inside() -> None:
    body = b"<html><body>live preview</body></html>"
    session = _FakeSession((200, body, "text/html"))
    rt = _FakeRuntime(session)
    resp = _client(rt).get("/conversations/conv_abc12345/preview-app/")
    assert resp.status_code == 200
    assert resp.content == body
    assert resp.headers["content-type"].startswith("text/html")
    # liveness probe hit the live session on the preview port
    assert session.calls and session.calls[0][0] == 8000


def test_port_app_serves_live_body_via_fetch_inside() -> None:
    body = b"api up"
    session = _FakeSession((200, body, "application/json"))
    rt = _FakeRuntime(session)
    resp = _client(rt).get("/conversations/conv_porttest1/port/3000/health")
    assert resp.status_code == 200
    assert resp.content == body
    assert session.calls[0] == (3000, "health")


# ---- (b) nothing listening → honest 503 ---------------------------------------


def test_preview_app_503_when_fetch_inside_none_and_no_snapshot() -> None:
    session = _FakeSession(None)  # nothing is listening inside the box
    rt = _FakeRuntime(session)
    resp = _client(rt).get("/conversations/conv_dead0000/preview-app/")
    assert resp.status_code == 503
    assert b"preview not available" in resp.content


def test_port_app_503_when_fetch_inside_none() -> None:
    session = _FakeSession(None)
    rt = _FakeRuntime(session)
    resp = _client(rt).get("/conversations/conv_dead0000/port/5173/")
    assert resp.status_code == 503


def test_preview_app_503_when_no_live_session() -> None:
    rt = _FakeRuntime(None)  # no executor/session at all
    resp = _client(rt).get("/conversations/conv_nosess00/preview-app/")
    assert resp.status_code == 503


# ---- (e) codex P1: canonical hostname proxy falls back to fetch_inside --------


def _host_proxy_client(
    *, upstream: str | None, session: _FakeSession | None
) -> TestClient:
    async def _upstream_resolver(cid8: str, port: int) -> str | None:
        return upstream

    def _session_resolver(cid8: str):
        return session

    inner = FastAPI()

    @inner.get("/{path:path}")
    async def _fallthrough(path: str) -> PlainTextResponse:  # pragma: no cover - not hit
        return PlainTextResponse("inner app should not be reached for a preview host")

    app = HostPreviewProxyMiddleware(
        inner,
        upstream_resolver=_upstream_resolver,
        session_resolver=_session_resolver,
    )
    return TestClient(app)


def test_hostname_proxy_falls_back_to_fetch_inside_when_no_upstream() -> None:
    """The in-app iframe host (cid8-8000.localhost) renders on a sealed box: no host
    upstream, but a live session serving → 200 from fetch_inside, NOT a 503."""
    body = b"<h1>iframe live</h1>"
    session = _FakeSession((200, body, "text/html"))
    client = _host_proxy_client(upstream=None, session=session)
    resp = client.get("/", headers={"host": "abc12345-8000.localhost"})
    assert resp.status_code == 200
    assert resp.content == body
    assert session.calls and session.calls[0][0] == 8000


def test_hostname_proxy_503_when_no_upstream_and_nothing_listening() -> None:
    """No upstream and the in-box server isn't up → honest 503 (not a false 200)."""
    session = _FakeSession(None)
    client = _host_proxy_client(upstream=None, session=session)
    resp = client.get("/", headers={"host": "abc12345-8000.localhost"})
    assert resp.status_code == 503
    assert b"preview not available" in resp.content


def test_hostname_proxy_503_when_no_upstream_and_no_session() -> None:
    client = _host_proxy_client(upstream=None, session=None)
    resp = client.get("/", headers={"host": "abc12345-8000.localhost"})
    assert resp.status_code == 503
