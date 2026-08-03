"""BP-13: idle-TTL sweep unit tests.

Tests the idleness truth table (status × connections × age), verifies the sweep
never selects RUNNING conversations, and confirms TTL/interval env overrides work.
All driven through sweep_idle_once() — no sleeping required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import SnapshotResult, StorageStatus

# ---- helpers -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _close_event_stores(monkeypatch):
    """Lifecycle runtimes form reference cycles; close their stores deterministically."""
    owned: list[SqliteEventStore] = []
    original_init = SqliteEventStore.__init__

    def tracked_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        original_init(self, *args, **kwargs)
        owned.append(self)

    monkeypatch.setattr(SqliteEventStore, "__init__", tracked_init)
    yield
    for event_store in owned:
        event_store.close()


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    router = MagicMock()
    svc = ProcessSandboxService()
    return ConversationRuntime(store, router=router, sandbox_service=svc)


def _runtime_with_storage(store: SqliteEventStore, projects_root: str) -> ConversationRuntime:
    """Runtime with projects_root configured so _suspend can snapshot + teardown."""
    rt = _runtime(store)
    # Patch the config store's load() to return a config with projects_root set.
    fake_cfg = MagicMock()
    fake_cfg.projects.projects_root = projects_root
    rt._config_store = MagicMock()
    rt._config_store.load.return_value = fake_cfg
    return rt


async def _make_conversation(store: SqliteEventStore, status: ConversationStatus) -> str:
    """Create a conversation with a user message and the given status."""
    cid = f"conv_test_{status.value.lower()}_{id(status)}"
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="hello"),
        ),
    )
    if status != ConversationStatus.IDLE:
        await store.append(cid, StatusEvent(status=status))
    return cid


class _VersionCutStore:
    def __init__(self, root: Path, *, fail_cut: bool = False) -> None:
        self._root = root
        self.fail_cut = fail_cut
        self.manifest_writes = 0
        self.cut_triggers: list[str] = []
        self.next_version = None
        self.versions = []

    def status(self) -> StorageStatus:
        return StorageStatus.OK

    def path_for(self, conversation_id: str) -> Path:
        return self._root / conversation_id / "workspace"

    def write_manifest(self, conversation_id: str, **kwargs) -> Path:
        self.manifest_writes += 1
        path = self._root / conversation_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return path

    def cut_version(self, conversation_id: str, *, trigger: str):
        self.cut_triggers.append(trigger)
        if self.fail_cut:
            raise RuntimeError("version store unavailable")
        return self.next_version

    def list_versions(self, conversation_id: str):
        return self.versions


# ---- snapshot version cuts ---------------------------------------------------


async def test_maybe_snapshot_cuts_version_after_manifest(monkeypatch, tmp_path):
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-snapshot-version"
    project_store = _VersionCutStore(tmp_path)
    rt._projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=object()))

    async def _snapshot(session, dest):
        assert dest == project_store.path_for(cid)
        return SnapshotResult(file_count=1, total_bytes=4, paths=["a.txt"])

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", _snapshot)

    await rt._maybe_snapshot(cid, trigger="finish")

    assert project_store.manifest_writes == 1
    assert project_store.cut_triggers == ["finish"]


async def test_maybe_snapshot_emits_commit_after_version_cut(monkeypatch, tmp_path):
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-snapshot-commit"
    project_store = _VersionCutStore(tmp_path)
    project_store.next_version = SimpleNamespace(seq=2, tree_digest="tree-2")
    rt._projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=object()))

    async def _snapshot(session, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text("<h1>committed</h1>")
        return SnapshotResult(file_count=1, total_bytes=4, paths=["a.txt"])

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", _snapshot)

    await rt._maybe_snapshot(cid, trigger="finish")

    events = await store.get_events(cid)
    assert len(events) == 2
    assert isinstance(events[0], DeliverableEvent)
    assert events[0].path == "."
    assert isinstance(events[1], WorkspaceVersionEvent)
    assert events[1].version_seq == 2
    assert events[1].tree_digest == "tree-2"
    assert events[1].trigger == "finish"


async def test_maybe_snapshot_reemits_commit_for_unchanged_latest_version(monkeypatch, tmp_path):
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-snapshot-unchanged-commit"
    project_store = _VersionCutStore(tmp_path)
    project_store.versions = [SimpleNamespace(seq=4, tree_digest="same-tree")]
    rt._projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=object()))

    async def _snapshot(session, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text("<h1>unchanged</h1>")
        return SnapshotResult(file_count=1, total_bytes=18, paths=["index.html"])

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", _snapshot)

    await rt._maybe_snapshot(cid, trigger="finish")

    commits = [
        event for event in await store.get_events(cid) if isinstance(event, WorkspaceVersionEvent)
    ]
    assert len(commits) == 1
    assert commits[0].version_seq == 4
    assert commits[0].tree_digest == "same-tree"
    assert commits[0].trigger == "finish"


async def test_maybe_snapshot_survives_cut_version_failure(monkeypatch, tmp_path, caplog):
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-snapshot-cut-fails"
    project_store = _VersionCutStore(tmp_path, fail_cut=True)
    rt._projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
    rt._persistence_notifier = MagicMock()
    rt._persistence_notifier.emit = AsyncMock()
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=object()))

    async def _snapshot(session, dest):
        return SnapshotResult(file_count=1, total_bytes=4, paths=["a.txt"])

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", _snapshot)
    caplog.set_level("WARNING")

    await rt._maybe_snapshot(cid)

    assert project_store.manifest_writes == 1
    assert project_store.cut_triggers == ["turn"]
    rt._persistence_notifier.emit.assert_not_awaited()
    assert "version cut failed" in caplog.text


# ---- idleness truth table ----------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.FINISHED,
        ConversationStatus.PAUSED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.IDLE,
    ],
)
async def test_idle_statuses_are_eligible(status, tmp_path):
    """Non-RUNNING statuses with no connections and old events are swept."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, status)

    # Inject a fake executor so _suspend thinks there's a live sandbox to free.
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    # Force the event to appear old by patching the TTL to 0.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.sweep_idle_once()

    assert count == 1
    assert not rt._run_resources.has_executor(cid)  # executor was removed (suspended)


async def test_running_never_swept():
    """RUNNING conversations must never be suspended by the sweep."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _make_conversation(store, ConversationStatus.RUNNING)

    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.sweep_idle_once()

    assert count == 0
    assert rt._run_resources.has_executor(cid)  # not touched


async def test_connected_session_not_swept():
    """A conversation with at least one live WS connection is not swept even if old."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _make_conversation(store, ConversationStatus.FINISHED)

    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)
    rt.connections.on_connect(cid)

    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.sweep_idle_once()

    assert count == 0
    assert rt._run_resources.has_executor(cid)


async def test_fresh_event_not_swept():
    """A conversation whose last event is newer than TTL is not swept."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _make_conversation(store, ConversationStatus.FINISHED)

    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    # TTL very large — the event just happened, so it's not idle.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "99999"}):
        count = await rt.sweep_idle_once()

    assert count == 0
    assert rt._run_resources.has_executor(cid)


async def test_ttl_env_override(tmp_path):
    """PMX_IDLE_SUSPEND_S is read per-sweep so tests can compress time."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, ConversationStatus.FINISHED)

    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    # With a huge TTL, NOT swept.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "99999"}):
        count = await rt.sweep_idle_once()
    assert count == 0

    # With TTL=0, swept.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.sweep_idle_once()
    assert count == 1


async def test_no_executor_not_swept():
    """A conversation without a live executor is skipped (nothing to suspend)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    await _make_conversation(store, ConversationStatus.FINISHED)
    # No executor injected.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.sweep_idle_once()
    assert count == 0


# ---- sandbox_state -----------------------------------------------------------


async def test_sandbox_state_active_with_executor():
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-active"
    rt._run_resources.set_executor(cid, MagicMock())
    assert rt.sandbox_state(cid) == "active"


async def test_sandbox_state_no_context():
    """No executor and no snapshot record → None (research surface)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    result = rt.sandbox_state("conv-nobody")
    assert result is None


# ---- _rehydrated flag regression (the production bug) -----------------------


async def test_rehydrated_flag_cleared_after_teardown():
    """_teardown_sandbox must clear the _rehydrated flag so a second rehydrate
    works after the sandbox is recreated (the conv_f3bdc842 production bug class).
    Without this fix, continuation runs started with an empty workspace."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-rehydrate-regression"

    # Simulate prior rehydration.
    rt._lifecycle._rehydration._rehydrated.add(cid)

    # Inject a fake executor so teardown has something to remove.
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    await rt._teardown_sandbox(cid)

    # The flag MUST be cleared so _maybe_rehydrate runs again after a recreate.
    assert cid not in rt._lifecycle._rehydration._rehydrated


async def test_rehydrate_after_recreate_clears_flag_and_rehydrates():
    """Mid-run recreate hook (bp-13 §2, orchestrator fix): _rehydrate_after_recreate
    must clear the idempotency flag THEN re-run _maybe_rehydrate — the mid-run drop
    path never goes through _teardown_sandbox, so without this hook the fresh
    instance stayed empty (conv_f3bdc842: 'all files were lost')."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-midrun-recreate"
    rt._lifecycle._rehydration._rehydrated.add(
        cid
    )  # the run already rehydrated once before the drop

    rt._lifecycle._rehydration._maybe_rehydrate = AsyncMock()
    await rt._lifecycle._rehydrate_after_recreate(cid)

    assert (
        cid not in rt._lifecycle._rehydration._rehydrated
    )  # flag cleared BEFORE the rehydrate call
    rt._lifecycle._rehydration._maybe_rehydrate.assert_awaited_once_with(cid)


async def test_build_session_wires_recreate_hook():
    """The sessions the runtime creates must carry the on_recreate hook — without
    the wiring, the session.py hook is dead code and mid-run drops lose the
    workspace exactly as before."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    session = rt.sessions.upload_session("conv-hook-wired")
    assert session._on_recreate is not None


# ---- orphan container sweep: terminal-status coverage -------------------------


async def test_orphan_sweep_destroys_idle_and_unknown(tmp_path):
    """IDLE is the PRIMARY live stop state (bp-12) — its containers are just as
    orphaned after a restart as FINISHED ones (handles are in-memory; resume
    builds a fresh instance). Unknown conversation_ids (store has no row) are
    garbage too. Both must be destroyed by the startup sweep."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    idle_cid = await _make_conversation(store, ConversationStatus.IDLE)

    svc = MagicMock()
    svc.list_live_instances = AsyncMock(return_value=[idle_cid, "conv-ghost-no-row"])
    svc.destroy_by_conversation = AsyncMock()
    rt._sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    await rt.reconcile_orphaned_runs()

    destroyed = {c.args[0] for c in svc.destroy_by_conversation.await_args_list}
    assert idle_cid in destroyed, "IDLE conversation's container must be swept"
    assert "conv-ghost-no-row" in destroyed, "unknown conversation's container must be swept"


async def test_orphan_sweep_keeps_running(tmp_path):
    """A genuinely RUNNING conversation (reconciled or otherwise) keeps its
    container — the sweep must never kill live work."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    # AWAITING_PLAN_APPROVAL: non-terminal, non-RUNNING — must be kept too
    # (the user is mid-decision; the sandbox may hold uploaded files).
    cid = await _make_conversation(store, ConversationStatus.AWAITING_PLAN_APPROVAL)

    svc = MagicMock()
    svc.list_live_instances = AsyncMock(return_value=[cid])
    svc.destroy_by_conversation = AsyncMock()
    rt._sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    await rt.reconcile_orphaned_runs()

    destroyed = {c.args[0] for c in svc.destroy_by_conversation.await_args_list}
    assert cid not in destroyed


async def test_orphan_sweep_destroys_concurrently(tmp_path):
    """Fix 5: the slow destroy_by_conversation calls must run CONCURRENTLY (bounded
    fan-out), not serially — a serial sweep blocked startup ~25s for 6 leaked
    containers. With N orphans each sleeping `delay`, total wall time must be ~one
    delay, not N×delay."""
    import asyncio
    import time

    store = SqliteEventStore(":memory:")
    rt = _runtime(store)

    n = 6
    delay = 0.2
    cids = []
    for _ in range(n):
        cids.append(await _make_conversation(store, ConversationStatus.FINISHED))

    destroyed: list[str] = []

    async def _slow_destroy(cid):
        await asyncio.sleep(delay)
        destroyed.append(cid)

    svc = MagicMock()
    svc.list_live_instances = AsyncMock(return_value=list(cids))
    svc.destroy_by_conversation = AsyncMock(side_effect=_slow_destroy)
    rt._sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    start = time.perf_counter()
    await rt.reconcile_orphaned_runs()
    elapsed = time.perf_counter() - start

    assert set(destroyed) == set(cids), "every orphan must be destroyed"
    # Serial would be n*delay (1.2s); concurrent (fan-out 6) ~= one delay. Generous
    # bound rules out serialization without being flaky.
    assert elapsed < delay * (n / 2), f"destroys ran serially: {elapsed:.2f}s for {n} x {delay}s"


async def test_orphan_sweep_one_failure_does_not_abort_others(tmp_path):
    """Fix 5 preserves the 'one bad cid doesn't abort the sweep' property under the
    concurrent destroy phase: a cid whose destroy raises must not stop the others
    from being destroyed."""
    import asyncio

    store = SqliteEventStore(":memory:")
    rt = _runtime(store)

    cids = [await _make_conversation(store, ConversationStatus.FINISHED) for _ in range(4)]
    bad = cids[1]
    destroyed: list[str] = []

    async def _maybe_fail(cid):
        await asyncio.sleep(0)
        if cid == bad:
            raise RuntimeError("backend refused to destroy this container")
        destroyed.append(cid)

    svc = MagicMock()
    svc.list_live_instances = AsyncMock(return_value=list(cids))
    svc.destroy_by_conversation = AsyncMock(side_effect=_maybe_fail)
    rt._sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    await rt.reconcile_orphaned_runs()

    # destroy_by_conversation was attempted on every cid (incl. the bad one)...
    attempted = {c.args[0] for c in svc.destroy_by_conversation.await_args_list}
    assert attempted == set(cids)
    # ...and the good ones all succeeded despite the bad cid raising.
    assert set(destroyed) == set(cids) - {bad}


# ---- HTTP state route overlay parity (live-spec regression) -------------------


async def test_http_state_route_overlays_sandbox_state():
    """GET /conversations/{cid}/state must carry the SAME extras.sandbox overlay
    as the WS state frame — agentLive fallback clients and the live UI specs poll
    HTTP. Caught live: the first bp-13 UI-spec run failed because only the WS
    path was overlaid (extras.sandbox came back undefined over HTTP)."""
    import httpx
    from disco.agent_server import create_app

    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _make_conversation(store, ConversationStatus.FINISHED)
    rt._run_resources.set_executor(cid, MagicMock())  # live executor → sandbox_state == "active"
    rt._mcp._start_mcp_pool = AsyncMock()
    rt._mcp._close_mcp_pool = AsyncMock()
    rt.reconcile_orphaned_runs = AsyncMock()
    rt.drivers.prewarm_model_probe = AsyncMock()
    rt.drivers.prewarm_vision_probe = AsyncMock()

    app = create_app(store, runtime=rt)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/conversations/{cid}/state")

    assert resp.status_code == 200
    assert resp.json()["extras"]["sandbox"] == "active"


# ---- LIFE-3: auto-suspend active-work guard ----------------------------------


async def test_live_run_task_blocks_suspend(tmp_path):
    """A live (not-done) run task means in-flight work — suspend must skip it even
    when the durable status is idle-ish (PAUSED) and no UI is connected."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, ConversationStatus.PAUSED)
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_never())
    rt._run_registry.register_task(cid, task)  # type: ignore[arg-type]
    try:
        with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
            count = await rt.sweep_idle_once()
        assert count == 0
        assert rt._run_resources.has_executor(cid)  # NOT suspended — a live run task is active work
    finally:
        task.cancel()


async def test_done_run_task_does_not_block_suspend(tmp_path):
    """A COMPLETED task is not in-flight — it must NOT block suspend."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, ConversationStatus.PAUSED)
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    async def _noop() -> None:
        return None

    t = asyncio.create_task(_noop())
    await t  # let it finish
    rt._run_registry.register_task(cid, t)  # type: ignore[arg-type]
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.sweep_idle_once()
    assert count == 1
    assert not rt._run_resources.has_executor(cid)  # a done task does not block suspend
