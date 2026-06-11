"""BP-14 — /sessions and /sessions/{name}/view routes.

Uses the same TestClient fixture pattern as test_wire.py (create_app with in-memory store).
The routes are gated behind a runtime, so we inject a fake runtime to test live paths.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from perpleximanus.agent_server import create_app
from perpleximanus.core import SqliteEventStore
from perpleximanus.tools.sandbox.shell_sessions import SessionInfo, SessionView

# ---- helpers -----------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    return TestClient(create_app(store))


@pytest.fixture
def client_with_runtime() -> TestClient:
    """TestClient with a fake runtime injected via create_app's runtime= kwarg."""
    store = SqliteEventStore(":memory:")
    runtime = _make_fake_runtime()
    return TestClient(create_app(store, runtime=runtime))


def _make_fake_runtime(sessions: list[SessionInfo] | None = None) -> MagicMock:
    """Build a minimal fake ConversationRuntime sufficient for the session routes."""
    rt = MagicMock()
    sessions = sessions or []

    async def _sessions_list(cid: str) -> list[SessionInfo]:
        return [s for s in sessions if not s.name.startswith("__")]

    async def _sessions_snapshot(cid: str) -> tuple[list[SessionInfo], bool]:
        filtered = [s for s in sessions if not s.name.startswith("__")]
        return (filtered, False)

    async def _session_view(cid: str, name: str, tail_chars: int) -> SessionView | None:
        for s in sessions:
            if s.name == name:
                return SessionView(running=s.busy, output=f"output of {name}")
        return None

    rt.sessions_list = AsyncMock(side_effect=_sessions_list)
    rt.sessions_snapshot = AsyncMock(side_effect=_sessions_snapshot)
    rt.session_view = AsyncMock(side_effect=_session_view)
    return rt


def _create_conversation(client: TestClient) -> str:
    resp = client.post("/conversations", json={"owner_id": "test"})
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


# ---- no-runtime guard --------------------------------------------------------

def test_list_sessions_no_runtime_returns_empty(client: TestClient) -> None:
    cid = _create_conversation(client)
    r = client.get(f"/conversations/{cid}/sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_get_session_view_no_runtime_404(client: TestClient) -> None:
    cid = _create_conversation(client)
    r = client.get(f"/conversations/{cid}/sessions/dev/view")
    assert r.status_code == 404


# ---- list shape --------------------------------------------------------------

def test_list_sessions_wire_shape() -> None:
    sessions = [
        SessionInfo(name="dev", busy=True, last_lines="line1\nline2\nVITE ready"),
        SessionInfo(name="preview", busy=False, last_lines="Serving HTTP on 0.0.0.0"),
    ]
    rt = _make_fake_runtime(sessions)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 2

    dev = next(s for s in body["sessions"] if s["name"] == "dev")
    assert dev["busy"] is True
    assert dev["last_line"] == "VITE ready"

    prev = next(s for s in body["sessions"] if s["name"] == "preview")
    assert prev["busy"] is False
    assert prev["last_line"] == "Serving HTTP on 0.0.0.0"


def test_list_sessions_empty_when_no_sandbox() -> None:
    rt = _make_fake_runtime([])
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions")
    assert r.status_code == 200
    assert r.json()["sessions"] == []
    assert r.json()["stale"] is False


# ---- __-prefix exclusion -----------------------------------------------------

def test_internal_sessions_excluded_from_list() -> None:
    sessions = [
        SessionInfo(name="__browser", busy=False, last_lines=""),
        SessionInfo(name="__kernel", busy=False, last_lines=""),
        SessionInfo(name="dev", busy=True, last_lines="serving"),
    ]
    rt = _make_fake_runtime(sessions)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions")
    names = [s["name"] for s in r.json()["sessions"]]
    assert names == ["dev"]


def test_internal_session_view_404() -> None:
    sessions = [SessionInfo(name="__browser", busy=False, last_lines="")]
    rt = _make_fake_runtime(sessions)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions/__browser/view")
    assert r.status_code == 404


# ---- view wire shape ---------------------------------------------------------

def test_view_wire_shape() -> None:
    sessions = [SessionInfo(name="dev", busy=True, last_lines="VITE ready")]
    rt = _make_fake_runtime(sessions)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions/dev/view")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "dev"
    assert body["busy"] is True
    assert "output of dev" in body["content"]


# ---- 404 on unknown name -----------------------------------------------------

def test_view_unknown_name_404() -> None:
    sessions = [SessionInfo(name="dev", busy=True, last_lines="running")]
    rt = _make_fake_runtime(sessions)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions/nonexistent/view")
    assert r.status_code == 404


# ---- tail_chars clamp --------------------------------------------------------

def test_tail_chars_clamped_to_max() -> None:
    sessions = [SessionInfo(name="dev", busy=False, last_lines="ok")]
    rt = _make_fake_runtime(sessions)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=rt))
    cid = _create_conversation(client)

    # tail_chars above the allowed max (100_000) should fail validation (422)
    r = client.get(f"/conversations/{cid}/sessions/dev/view?tail_chars=999999999")
    assert r.status_code == 422


# ---- coalescing: two concurrent /view calls → one exec_shell -----------------

def test_view_coalescing_single_exec() -> None:
    """Two concurrent session_view calls for the same (cid, name) coalesce into one
    capture-pane exec. We test the runtime layer directly (not via HTTP) to count
    the underlying view() invocations on a counting fake ShellSessionManager."""
    from perpleximanus.agent_server.runtime import ConversationRuntime
    from perpleximanus.core import SqliteEventStore
    from perpleximanus.tools.sandbox.shell_sessions import SessionView

    call_count = 0

    class FakeManager:
        async def view(self, name: str, tail_chars: int = 10_000) -> SessionView:
            nonlocal call_count
            call_count += 1
            # Simulate a tiny I/O delay so concurrent calls really overlap.
            await asyncio.sleep(0.01)
            return SessionView(running=True, output="hello")

        async def list(self):
            return [SessionInfo(name="dev", busy=True, last_lines="hello")]

    class FakeSession:
        sessions = FakeManager()

    class FakeSandboxService:
        name = "fake"

    fake_session = FakeSession()
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)

    cid = "conv_coalesce_test"
    # Inject a fake executor with the fake session
    fake_executor = MagicMock()
    fake_executor._sandbox = fake_session
    runtime._executors[cid] = fake_executor

    async def run():
        # Fire two concurrent view calls — they should coalesce.
        results = await asyncio.gather(
            runtime.session_view(cid, "dev", 10_000),
            runtime.session_view(cid, "dev", 10_000),
        )
        return results

    results = asyncio.run(run())
    assert all(r is not None for r in results)
    assert call_count == 1, f"Expected 1 view() call (coalesced), got {call_count}"
