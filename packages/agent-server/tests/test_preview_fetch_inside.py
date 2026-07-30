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

    def __init__(self, session: _FakeSession | None, *, target_port: int = 8000) -> None:
        self._session = session
        self._preview = self
        self._target_port = target_port
        self.wake_calls: list[tuple[str, int]] = []

    def preview_target_port(self, conversation_id: str) -> int:
        return self._target_port

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
    if resp.headers.get("content-type", "").startswith("text/html"):
        assert resp.content.startswith(body.split(b"</body>")[0])
        assert b"disco-element-mention-picker:v1" in resp.content
    else:
        assert resp.content == body
    assert resp.headers["content-type"].startswith("text/html")
    # liveness probe hit the live session on the preview port
    assert session.calls and session.calls[0][0] == 8000


def test_preview_app_follows_managed_nondefault_port_for_wake_and_fetch() -> None:
    """H333: canonical origin stays isolated while its upstream follows PreviewManager."""
    body = b"<html><body>managed 5173</body></html>"
    session = _FakeSession((200, body, "text/html"))
    rt = _FakeRuntime(session, target_port=5173)

    resp = _client(rt).get("/conversations/conv_abc12345/preview-app/")

    assert resp.status_code == 200
    assert rt.wake_calls == [("abc12345", 5173)]
    assert session.calls == [(5173, "")]


def test_port_app_serves_live_body_via_fetch_inside() -> None:
    body = b"api up"
    session = _FakeSession((200, body, "application/json"))
    rt = _FakeRuntime(session)
    resp = _client(rt).get("/conversations/conv_porttest1/port/3000/health")
    assert resp.status_code == 200
    if resp.headers.get("content-type", "").startswith("text/html"):
        assert resp.content.startswith(body.split(b"</body>")[0])
        assert b"disco-element-mention-picker:v1" in resp.content
    else:
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


def _host_proxy_client(*, upstream: str | None, session: _FakeSession | None) -> TestClient:
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
    """The in-app iframe host (p2-cid8-8000.localhost) renders on a sealed box: no host
    upstream, but a live session serving → 200 from fetch_inside, NOT a 503."""
    body = b"<h1>iframe live</h1>"
    session = _FakeSession((200, body, "text/html"))
    client = _host_proxy_client(upstream=None, session=session)
    resp = client.get("/", headers={"host": "p2-abc12345-8000.localhost"})
    assert resp.status_code == 200
    if resp.headers.get("content-type", "").startswith("text/html"):
        assert resp.content.startswith(body.split(b"</body>")[0])
        assert b"disco-element-mention-picker:v1" in resp.content
    else:
        assert resp.content == body
    assert session.calls and session.calls[0][0] == 8000


def test_hostname_proxy_503_when_no_upstream_and_nothing_listening() -> None:
    """No upstream and the in-box server isn't up → honest 503 (not a false 200)."""
    session = _FakeSession(None)
    client = _host_proxy_client(upstream=None, session=session)
    resp = client.get("/", headers={"host": "p2-abc12345-8000.localhost"})
    assert resp.status_code == 503
    assert b"preview not available" in resp.content


def test_hostname_proxy_503_when_no_upstream_and_no_session() -> None:
    client = _host_proxy_client(upstream=None, session=None)
    resp = client.get("/", headers={"host": "p2-abc12345-8000.localhost"})
    assert resp.status_code == 503


# ---- (g) noVNC gate-bypass regression: NOVNC_PORT must NOT use fetch_inside ----
#
# codex P1: NOVNC_PORT (6080) is in USER_PORTS, so the path proxy + hostname proxy
# reach the fetch_inside fallback. When live browser is DISABLED, wake_for_preview /
# the upstream resolver returns None for 6080 *as the gate* (port_upstream refuses
# NOVNC_PORT). The fetch_inside fallback firing on that None would re-expose the
# stale in-box noVNC HTTP surface, bypassing the gate. These prove it does NOT — the
# disabled noVNC surface stays a 503 — while a normal dev port (8000) still falls back.


def test_port_app_does_not_fetch_inside_novnc_when_gated() -> None:
    """GET /port/6080/ on a sealed box with a live session that WOULD answer must
    still 503: the noVNC surface is never reachable via the exec-curl fallback, so
    a disabled live-browser surface cannot be revived through the path proxy."""
    from disco.tools.sandbox._container import NOVNC_PORT

    # session.fetch_inside WOULD return a live noVNC page if it were ever called.
    session = _FakeSession((200, b"<html>noVNC</html>", "text/html"))
    rt = _FakeRuntime(session)
    resp = _client(rt).get(f"/conversations/conv_novnc001/port/{NOVNC_PORT}/vnc.html")
    assert resp.status_code == 503
    assert b"preview not available" in resp.content
    # The gate held: fetch_inside was NEVER invoked for the noVNC port.
    assert all(call[0] != NOVNC_PORT for call in session.calls)


def test_port_app_still_fetches_inside_normal_port() -> None:
    """Control: a genuine dev port (8000) still gets the fetch_inside fallback — the
    fix closes ONLY the noVNC bypass, not the whole point of Fix 2."""
    body = b"dev server up"
    session = _FakeSession((200, body, "text/html"))
    rt = _FakeRuntime(session)
    resp = _client(rt).get("/conversations/conv_devport0/port/8000/")
    assert resp.status_code == 200
    if resp.headers.get("content-type", "").startswith("text/html"):
        assert resp.content.startswith(body.split(b"</body>")[0])
        assert b"disco-element-mention-picker:v1" in resp.content
    else:
        assert resp.content == body
    assert session.calls and session.calls[0][0] == 8000


def test_hostname_proxy_does_not_fetch_inside_novnc_when_gated() -> None:
    """The canonical hostname proxy (p2-cid8-6080.localhost) must also refuse the
    fetch_inside fallback for the noVNC surface → honest 503, gate preserved."""
    from disco.tools.sandbox._container import NOVNC_PORT

    session = _FakeSession((200, b"<html>noVNC</html>", "text/html"))
    client = _host_proxy_client(upstream=None, session=session)
    resp = client.get("/vnc.html", headers={"host": f"p2-abc12345-{NOVNC_PORT}.localhost"})
    assert resp.status_code == 503
    assert b"preview not available" in resp.content
    assert all(call[0] != NOVNC_PORT for call in session.calls)


def test_hostname_proxy_still_fetches_inside_normal_port() -> None:
    """Control for the hostname proxy: a normal dev port still falls back to
    fetch_inside (Fix 2 reachability preserved)."""
    body = b"<h1>iframe live</h1>"
    session = _FakeSession((200, body, "text/html"))
    client = _host_proxy_client(upstream=None, session=session)
    resp = client.get("/", headers={"host": "p2-abc12345-8000.localhost"})
    assert resp.status_code == 200
    if resp.headers.get("content-type", "").startswith("text/html"):
        assert resp.content.startswith(body.split(b"</body>")[0])
        assert b"disco-element-mention-picker:v1" in resp.content
    else:
        assert resp.content == body
    assert session.calls and session.calls[0][0] == 8000
