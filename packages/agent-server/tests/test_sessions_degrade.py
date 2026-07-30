"""DC-04b — sessions_snapshot retry + stale-degrade tests (DEFECT-1).

Fixture pattern mirrors test_sessions_routes.py: real ConversationRuntime with
a mocked live-session directory whose sessions.list is an AsyncMock, plus a TestClient
created via create_app(store, runtime=...) for route-layer assertions.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from disco.agent_server import create_app
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.tools.sandbox.shell_sessions import SessionInfo
from fastapi.testclient import TestClient

# ---- helpers -----------------------------------------------------------------


def _make_runtime_with_mock_session(list_mock: AsyncMock) -> ConversationRuntime:
    """Return a ConversationRuntime whose live_session() yields a fake sandbox
    whose sessions.list is the supplied AsyncMock."""
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)

    fake_manager = MagicMock()
    fake_manager.list = list_mock

    fake_session = MagicMock()
    fake_session.sessions = fake_manager

    runtime._live_sessions.live_session = MagicMock(return_value=fake_session)
    return runtime


def _make_runtime_no_sandbox() -> ConversationRuntime:
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    runtime._live_sessions.live_session = MagicMock(return_value=None)
    return runtime


def _fresh_session(name: str = "dev") -> SessionInfo:
    return SessionInfo(name=name, busy=False, last_lines="ok")


# ---- sessions_snapshot unit tests --------------------------------------------


def test_transient_failure_succeeds_on_second_attempt() -> None:
    """list raises once then succeeds → (fresh, False), exactly 2 calls, cache updated."""
    fresh = [_fresh_session("dev")]
    list_mock = AsyncMock(side_effect=[ConnectionResetError("blip"), fresh])
    runtime = _make_runtime_with_mock_session(list_mock)
    cid = "conv_transient"

    async def run():
        result, stale = await runtime._sessions.sessions_snapshot(cid)
        return result, stale

    result, stale = asyncio.run(run())

    assert stale is False
    assert len(result) == 1
    assert result[0].name == "dev"
    assert list_mock.call_count == 2
    assert runtime._connections.last_sessions_get(cid) == result


def test_dead_pipe_with_history_returns_stale() -> None:
    """Seed one successful call, then list raises persistently → (last_known, True),
    exactly 3 calls on the failing snapshot."""
    fresh = [_fresh_session("dev")]
    list_mock = AsyncMock(return_value=fresh)
    runtime = _make_runtime_with_mock_session(list_mock)
    cid = "conv_dead_with_history"

    async def run():
        # Seed the cache with a successful call.
        first, stale0 = await runtime._sessions.sessions_snapshot(cid)
        assert stale0 is False
        assert first == fresh
        assert list_mock.call_count == 1

        # Now make list always raise.
        list_mock.side_effect = ConnectionResetError("ssh pipe dropped")
        list_mock.reset_mock()

        # The degraded snapshot should return the seeded list marked stale.
        degraded, stale1 = await runtime._sessions.sessions_snapshot(cid)
        return degraded, stale1, list_mock.call_count

    degraded, stale1, call_count = asyncio.run(run())

    assert stale1 is True
    assert len(degraded) == 1
    assert degraded[0].name == "dev"
    # 3 total attempts (initial + 2 retries)
    assert call_count == 3


def test_dead_pipe_cold_no_history_returns_empty_stale() -> None:
    """Persistent raise, no prior success → ([], True)."""
    list_mock = AsyncMock(side_effect=ConnectionResetError("ssh pipe dropped"))
    runtime = _make_runtime_with_mock_session(list_mock)
    cid = "conv_dead_cold"

    async def run():
        return await runtime._sessions.sessions_snapshot(cid)

    result, stale = asyncio.run(run())

    assert stale is True
    assert result == []
    assert list_mock.call_count == 3


def test_no_sandbox_returns_empty_not_stale() -> None:
    """live_session → None ⇒ ([], False) and the owner cache untouched."""
    runtime = _make_runtime_no_sandbox()
    cid = "conv_no_sandbox"

    async def run():
        return await runtime._sessions.sessions_snapshot(cid)

    result, stale = asyncio.run(run())

    assert result == []
    assert stale is False
    assert cid not in runtime._connections._state._last_sessions


def test_teardown_hygiene() -> None:
    """Populate the cache, run _teardown_sandbox, cache no longer holds the cid."""
    fresh = [_fresh_session("dev")]
    list_mock = AsyncMock(return_value=fresh)
    runtime = _make_runtime_with_mock_session(list_mock)
    cid = "conv_hygiene"

    async def run():
        # Populate the connection owner's last-session cache.
        await runtime._sessions.sessions_snapshot(cid)
        assert cid in runtime._connections._state._last_sessions
        # Teardown should evict it.
        await runtime._teardown_sandbox(cid)
        assert cid not in runtime._connections._state._last_sessions

    asyncio.run(run())


# ---- route layer tests -------------------------------------------------------


def _create_conversation(client: TestClient) -> str:
    resp = client.post("/conversations", json={"owner_id": "test"})
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


def test_route_stale_true_and_200_on_degraded_path() -> None:
    """/sessions returns 200 with stale=true and last-known names when degraded."""
    fresh = [_fresh_session("dev")]
    list_mock = AsyncMock(return_value=fresh)
    runtime = _make_runtime_with_mock_session(list_mock)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=runtime))
    cid = _create_conversation(client)

    # Seed the cache.
    r = client.get(f"/conversations/{cid}/sessions")
    assert r.status_code == 200
    assert r.json()["stale"] is False
    assert r.json()["sessions"][0]["name"] == "dev"

    # Now make list always fail.
    list_mock.side_effect = ConnectionResetError("ssh pipe dropped")

    # Must be 200, never 500.
    r2 = client.get(f"/conversations/{cid}/sessions")
    assert r2.status_code == 200, f"Expected 200, got {r2.status_code}"
    body2 = r2.json()
    assert body2["stale"] is True
    assert body2["sessions"][0]["name"] == "dev"


def test_route_never_500_on_degraded_path() -> None:
    """Explicitly assert status_code == 200 even with a persistently failing list."""
    list_mock = AsyncMock(side_effect=OSError("connection reset"))
    runtime = _make_runtime_with_mock_session(list_mock)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=runtime))
    cid = _create_conversation(client)

    r = client.get(f"/conversations/{cid}/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["sessions"] == []


# ---- DEFECT-1 replay ---------------------------------------------------------


def test_defect1_ssh_pipe_drop_two_consecutive_polls() -> None:
    """Replay DEFECT-1: sessions.list raises ConnectionResetError mid-poll.
    Two consecutive /sessions polls must both be 200; second one is stale."""
    fresh = [_fresh_session("dev")]
    list_mock = AsyncMock(return_value=fresh)
    runtime = _make_runtime_with_mock_session(list_mock)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=runtime))
    cid = _create_conversation(client)

    # First poll — healthy.
    r1 = client.get(f"/conversations/{cid}/sessions")
    assert r1.status_code == 200
    assert r1.json()["stale"] is False

    # Drop the pipe.
    list_mock.side_effect = ConnectionResetError("ssh pipe dropped")

    # Second poll — must still be 200, now stale.
    r2 = client.get(f"/conversations/{cid}/sessions")
    assert r2.status_code == 200, f"DEFECT-1 regression: got {r2.status_code}"
    assert r2.json()["stale"] is True
    assert r2.json()["sessions"][0]["name"] == "dev"
