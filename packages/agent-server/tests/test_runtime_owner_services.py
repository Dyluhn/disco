"""Focused owner-level characterization for the P8/P9 runtime owner services.

``SpaceService`` and ``ConnectionTracker`` are the new owners extracted from
``ConversationRuntime``.  This test characterizes each owner in isolation
through its **narrow named typed collaborators** — not through the runtime —
so the contract (no ``ConversationRuntime`` / ``Any`` / generic context /
cross-domain dictionaries) is proven before the primary session performs the
composition wiring.

Behavior characterized:

- ``SpaceService``: ``space_store`` / ``space_vector_store`` /
  ``space_corpus_service`` / ``set_space_ids`` / ``get_space_ids`` preserve
  the exact semantics previously on ``ConversationRuntime``.
- ``ConnectionTracker``: ``on_connect`` / ``on_disconnect`` /
  ``_suspend_after_grace`` preserve the auto-suspend timing/cancellation;
  session-view cache, wake-lock, last-sessions, and per-conversation cleanup
  behave identically to the raw-dict access patterns they replace.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from disco.agent_server.connection_tracker import ConnectionTracker
from disco.agent_server.space_service import SpaceService
from disco.retrieval import DiskVectorStore
from disco.retrieval.ranking import Embedder
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

# ---------------------------------------------------------------------------
# SpaceService collaborators
# ---------------------------------------------------------------------------


class _StubProjectRoot:
    """Resolve a fixed project-store root path."""

    def __init__(self, root: str | None) -> None:
        self._root = root

    def root(self) -> str | None:
        return self._root


class _StubEmbedder:
    """Resolve a fixed embedder object."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def embedder(self) -> Embedder:
        return self._embedder


class _FakeEmbedder:
    """Minimal Embedder protocol implementation for corpus-service construction."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


# ---------------------------------------------------------------------------
# SpaceService tests
# ---------------------------------------------------------------------------


def test_space_store_returns_json_space_store_under_root(tmp_path: Path) -> None:
    root = str(tmp_path / "projects")
    svc = SpaceService(_StubProjectRoot(root), _StubEmbedder(_FakeEmbedder()))
    store = svc.space_store()
    assert store.spaces_dir == tmp_path / "projects" / "spaces"


def test_space_store_returns_empty_store_when_root_unset() -> None:
    svc = SpaceService(_StubProjectRoot(None), _StubEmbedder(_FakeEmbedder()))
    store = svc.space_store()
    assert store.spaces_dir == Path("") / "spaces"


def test_space_vector_store_caches_and_rebuilds_on_root_change(tmp_path: Path) -> None:
    root_a = str(tmp_path / "projects_a")
    root_b = str(tmp_path / "projects_b")
    svc = SpaceService(_StubProjectRoot(root_a), _StubEmbedder(_FakeEmbedder()))

    store_a = svc.space_vector_store()
    assert isinstance(store_a, DiskVectorStore)
    assert svc._space_vector_store is store_a
    assert svc._space_vector_store_root == str(
        (tmp_path / "projects_a" / "spaces" / "vectors").resolve()
    ) or svc._space_vector_store_root == str(tmp_path / "projects_a" / "spaces" / "vectors")

    # Same root → same instance (cached).
    assert svc.space_vector_store() is store_a

    # Root changes → rebuilt.
    svc._project_root = _StubProjectRoot(root_b)
    store_b = svc.space_vector_store()
    assert store_b is not store_a
    assert svc._space_vector_store is store_b


def test_space_corpus_service_constructs_with_live_embedder(tmp_path: Path) -> None:
    from disco.retrieval import DefaultCorpusService

    root = str(tmp_path / "projects")
    embedder = _FakeEmbedder()
    svc = SpaceService(_StubProjectRoot(root), _StubEmbedder(embedder))
    corpus = svc.space_corpus_service()
    assert isinstance(corpus, DefaultCorpusService)


def test_set_space_ids_strips_blanks_and_pins(tmp_path: Path) -> None:
    svc = SpaceService(_StubProjectRoot(str(tmp_path)), _StubEmbedder(_FakeEmbedder()))
    svc.set_space_ids("conv_a", ["  space_1  ", "", "space_2"])
    assert svc.get_space_ids("conv_a") == frozenset({"space_1", "space_2"})


def test_set_space_ids_clears_on_empty(tmp_path: Path) -> None:
    svc = SpaceService(_StubProjectRoot(str(tmp_path)), _StubEmbedder(_FakeEmbedder()))
    svc.set_space_ids("conv_a", ["space_1"])
    assert "conv_a" in svc._space_ids
    svc.set_space_ids("conv_a", ["  ", ""])
    assert "conv_a" not in svc._space_ids
    assert svc.get_space_ids("conv_a") == frozenset()


def test_get_space_ids_none_conversation_returns_empty(tmp_path: Path) -> None:
    svc = SpaceService(_StubProjectRoot(str(tmp_path)), _StubEmbedder(_FakeEmbedder()))
    assert svc.get_space_ids(None) == frozenset()
    assert svc.get_space_ids("never_set") == frozenset()


# ---------------------------------------------------------------------------
# ConnectionTracker collaborators
# ---------------------------------------------------------------------------


class _StubSuspend:
    """Record suspend calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def suspend(self, conversation_id: str) -> None:
        self.calls.append(conversation_id)


# ---------------------------------------------------------------------------
# ConnectionTracker: on_connect / on_disconnect / suspend scheduling
# ---------------------------------------------------------------------------


def test_on_connect_increments_and_cancels_pending_suspend() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    assert tracker._state._connections["conv_a"] == 1
    tracker.on_connect("conv_a")
    assert tracker._state._connections["conv_a"] == 2


async def test_on_disconnect_decrements_and_keeps_count_when_remaining() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a")
    assert tracker._state._connections["conv_a"] == 1
    assert "conv_a" not in tracker._suspend_tasks


async def test_on_disconnect_last_connection_schedules_suspend() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a", grace_s=0.01)
    assert "conv_a" not in tracker._state._connections
    assert "conv_a" in tracker._suspend_tasks


async def test_on_disconnect_reconnect_cancels_suspend() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a", grace_s=10.0)
    assert "conv_a" in tracker._suspend_tasks
    # Reconnect before grace elapses.
    tracker.on_connect("conv_a")
    assert "conv_a" not in tracker._suspend_tasks
    assert tracker._state._connections["conv_a"] == 1


async def test_suspend_after_grace_fires_when_no_connections() -> None:
    suspend = _StubSuspend()
    tracker = ConnectionTracker(suspend)
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a", grace_s=0.01)
    # Let the grace elapse.
    await asyncio.sleep(0.05)
    assert suspend.calls == ["conv_a"]
    assert "conv_a" not in tracker._suspend_tasks


async def test_suspend_after_grace_skipped_when_reconnected() -> None:
    suspend = _StubSuspend()
    tracker = ConnectionTracker(suspend)
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a", grace_s=0.05)
    # Reconnect before grace elapses.
    tracker.on_connect("conv_a")
    await asyncio.sleep(0.1)
    assert suspend.calls == []
    # The suspend task was cancelled and removed.
    assert "conv_a" not in tracker._suspend_tasks


# ---------------------------------------------------------------------------
# ConnectionTracker: has_connections / cancel_suspension
# ---------------------------------------------------------------------------


async def test_has_connections_true_only_when_count_positive() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    assert tracker.has_connections("conv_a") is False
    tracker.on_connect("conv_a")
    assert tracker.has_connections("conv_a") is True
    tracker.on_disconnect("conv_a", grace_s=999.0)
    assert tracker.has_connections("conv_a") is False


async def test_cancel_suspension_cancels_pending_task() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a", grace_s=999.0)
    assert "conv_a" in tracker._suspend_tasks
    tracker.cancel_suspension("conv_a")
    assert "conv_a" not in tracker._suspend_tasks


# ---------------------------------------------------------------------------
# ConnectionTracker: session-view coalescing cache
# ---------------------------------------------------------------------------


def test_session_view_lock_returns_same_lock_for_same_key() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    lock1 = tracker.session_view_lock("conv_a", "dev")
    lock2 = tracker.session_view_lock("conv_a", "dev")
    assert lock1 is lock2


def test_session_view_cache_get_set_round_trip() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    assert tracker.session_view_cache_get("conv_a", "dev") is None
    now = time.monotonic()
    view = SessionView(running=True, output="hello")
    tracker.session_view_cache_set("conv_a", "dev", now, view)
    cached = tracker.session_view_cache_get("conv_a", "dev")
    assert cached is not None
    assert cached[0] == now
    assert cached[1] is view


# ---------------------------------------------------------------------------
# ConnectionTracker: wake-lock
# ---------------------------------------------------------------------------


def test_wake_lock_for_returns_same_lock_for_same_cid() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    lock1 = tracker.wake_lock_for("conv_a")
    lock2 = tracker.wake_lock_for("conv_a")
    assert lock1 is lock2


# ---------------------------------------------------------------------------
# ConnectionTracker: last-sessions degrade
# ---------------------------------------------------------------------------


def test_last_sessions_get_returns_empty_when_unset() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    assert tracker.last_sessions_get("conv_a") == []


def test_set_last_sessions_round_trip() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    sessions = [SessionInfo(name="dev", busy=True, last_lines="ok")]
    tracker.set_last_sessions("conv_a", sessions)
    assert tracker.last_sessions_get("conv_a") is sessions


# ---------------------------------------------------------------------------
# ConnectionTracker: per-conversation cleanup
# ---------------------------------------------------------------------------


def test_clear_conversation_drops_all_caches_and_locks() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    # Populate all caches.
    tracker.on_connect("conv_a")
    tracker.session_view_cache_set("conv_a", "dev", 0.0, SessionView(running=False, output=""))
    tracker.session_view_lock("conv_a", "dev")
    tracker.wake_lock_for("conv_a")
    tracker.set_last_sessions("conv_a", [SessionInfo(name="dev", busy=False, last_lines="")])

    tracker.clear_conversation("conv_a")

    assert tracker.session_view_cache_get("conv_a", "dev") is None
    assert ("conv_a", "dev") not in tracker._state._session_view_locks
    assert "conv_a" not in tracker._state._wake_locks
    assert tracker.last_sessions_get("conv_a") == []
    assert tracker.has_connections("conv_a") is False
    assert "conv_a" not in tracker._suspend_tasks


def test_clear_session_state_preserves_connection_ownership() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    tracker.session_view_cache_set(
        "conv_a",
        "dev",
        0.0,
        SessionView(running=True, output="out"),
    )
    tracker.set_last_sessions("conv_a", [])

    tracker.clear_session_state("conv_a")

    assert tracker.has_connections("conv_a")
    assert tracker.session_view_cache_get("conv_a", "dev") is None
    assert tracker.last_sessions_get("conv_a") == []


async def test_clear_conversation_cancels_pending_suspend() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.on_connect("conv_a")
    tracker.on_disconnect("conv_a", grace_s=999.0)
    assert "conv_a" in tracker._suspend_tasks
    tracker.clear_conversation("conv_a")
    assert "conv_a" not in tracker._suspend_tasks


def test_clear_conversation_does_not_touch_other_conversations() -> None:
    tracker = ConnectionTracker(_StubSuspend())
    tracker.set_last_sessions("conv_a", [SessionInfo(name="dev", busy=False, last_lines="")])
    tracker.set_last_sessions("conv_b", [SessionInfo(name="dev", busy=True, last_lines="x")])

    tracker.clear_conversation("conv_a")

    assert tracker.last_sessions_get("conv_a") == []
    assert len(tracker.last_sessions_get("conv_b")) == 1
