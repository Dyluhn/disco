"""BP-15 — workspace-file route: allowlist + 404 shapes.

The truth table runs against a STUB runtime whose session serves ANY path —
so a rejected path 404ing proves the allowlist fired BEFORE the read, and an
allowed path must come back 200 with PNG bytes.  (An earlier draft ran with
no runtime at all, where every path 404s and the table proved nothing.)
The real-sandbox round-trip is in
test-record/bp-15/integration-workspace-route.log.
"""

from __future__ import annotations

import pytest
from disco.agent_server.routes.conversations import make_conversations_router
from disco.core import SqliteEventStore
from fastapi.testclient import TestClient

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MISSING = ".pmx/screenshots/9999-missing.png"


class _StubSession:
    """Serves PNG bytes for EVERY path except the designated missing one —
    if a path 404s against this session, the route rejected it itself."""

    async def read_file(self, path: str) -> bytes:
        if path == MISSING:
            raise FileNotFoundError(path)
        return PNG_MAGIC + b"stub"


class _StubLiveSessions:
    """The LiveSessionDirectory named owner (13-B2): the stub reaches the live
    session through `runtime.live_sessions`, not through a runtime delegate."""

    def live_session(self, cid: str) -> _StubSession:
        return _StubSession()


class _StubRuntime:
    """Just enough surface for create_conversation + the workspace route."""

    def __init__(self) -> None:
        self.live_sessions = _StubLiveSessions()

    def set_surface(self, cid: str, surface: object) -> None: ...

    def set_model_override(self, cid: str, model: object) -> None: ...

    def set_depth(self, cid: str, tier: object) -> None: ...

    def get_last_selected_model(self) -> str | None:
        return None  # no last pick in the stub

    def sandbox_backend_name(self) -> str | None:
        return "gvisor"


@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    return _route_client(store, _StubRuntime())


@pytest.fixture
def bare_client() -> TestClient:
    """No runtime at all — the 'agent-server without a sandbox' shape."""
    store = SqliteEventStore(":memory:")
    return _route_client(store, None)


def _route_client(store: SqliteEventStore, runtime: object | None) -> TestClient:
    from disco.agent_server.auth import AgentAuthMiddleware
    from fastapi import FastAPI

    if runtime is not None:
        runtime.project_store = _project_store  # type: ignore[attr-defined]
    app = FastAPI()
    app.add_middleware(AgentAuthMiddleware, store=store)
    app.include_router(make_conversations_router(store, runtime))  # type: ignore[arg-type]
    return TestClient(app)


class _NoProjectStore:
    def path_for(self, _conversation_id: str) -> None:
        return None


def _project_store() -> _NoProjectStore:
    return _NoProjectStore()


def _create(client: TestClient) -> str:
    resp = client.post("/conversations", json={"owner_id": "local"})
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


# ── Allowlist truth table (stub session serves everything) ───────────────────


@pytest.mark.parametrize(
    "path",
    [
        ".pmx/screenshots/0001-navigate.png",
        ".pmx/screenshots/0042-click.png",
        ".pmx/plots/0001.png",
        ".pmx/plots/0099.png",
    ],
)
def test_allowed_paths_serve_png(client: TestClient, path: str) -> None:
    cid = _create(client)
    resp = client.get(f"/conversations/{cid}/workspace/{path}")
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path",
    [
        # outside the allowlist — user code must never be served here
        "index.html",
        "workspace/index.html",
        "src/app.py",
        ".pmx/secrets",
        ".pmx/screenshots",  # the bare dir (no trailing segment) isn't a file grant
        # path-traversal attempts (various forms)
        "../etc/passwd",
        ".pmx/screenshots/../../../etc/passwd",
        ".pmx/plots/../../secret",
        ".pmx/screenshots/../plots/0001.png",  # normalises to .pmx/plots — see below
        "%2e%2e/etc/passwd",
        # absolute paths
        "/etc/passwd",
        "/tmp/evil.png",
    ],
)
def test_rejected_paths_404_even_when_file_exists(client: TestClient, path: str) -> None:
    """The stub session would happily serve these — a 404 means the route's
    own allowlist rejected the path BEFORE any read. Never 403."""
    cid = _create(client)
    resp = client.get(f"/conversations/{cid}/workspace/{path}")
    if path == ".pmx/screenshots/../plots/0001.png":
        # normpath folds this INSIDE the allowlist (.pmx/plots/0001.png) —
        # same-allowlist hops are harmless by construction; just pin the
        # behavior so a change here is a conscious one.
        assert resp.status_code == 200
        return
    assert resp.status_code == 404


def test_missing_file_404(client: TestClient) -> None:
    """Allowed prefix but absent file → 404 (read_file raised)."""
    cid = _create(client)
    assert client.get(f"/conversations/{cid}/workspace/{MISSING}").status_code == 404


def test_create_returns_sandbox_backend(client: TestClient) -> None:
    body = client.post("/conversations", json={}).json()
    assert body["sandbox_backend"] == "gvisor"


# ── No-runtime shapes ─────────────────────────────────────────────────────────


def test_no_runtime_allowed_path_404_not_422(bare_client: TestClient) -> None:
    """Route matched (not 422), then 404 at the no-runtime check."""
    cid = _create(bare_client)
    r = bare_client.get(f"/conversations/{cid}/workspace/.pmx/screenshots/0001-nav.png")
    assert r.status_code == 404


def test_no_runtime_create_backend_none(bare_client: TestClient) -> None:
    body = bare_client.post("/conversations", json={}).json()
    assert "sandbox_backend" in body
    assert body["sandbox_backend"] is None


def test_no_runtime_state_backend_absent(bare_client: TestClient) -> None:
    cid = _create(bare_client)
    state = bare_client.get(f"/conversations/{cid}/state").json()
    assert state.get("sandbox_backend") is None


def test_unknown_conversation_404(bare_client: TestClient) -> None:
    r = bare_client.get("/conversations/conv_unknown/workspace/.pmx/screenshots/0001.png")
    assert r.status_code == 404
