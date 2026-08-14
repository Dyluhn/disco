"""BP-13: idle-TTL sweep unit tests.

Tests the idleness truth table (status × connections × age), verifies the sweep
never selects RUNNING conversations, and confirms TTL/interval env overrides work.
All driven through sweep_idle_once() — no sleeping required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import disco.agent_server.preview_capture_ownership as preview_capture_ownership_module
import pytest
from _static_preview_capability_support import _redeem
from disco.agent_server import ConversationRuntime
from disco.agent_server.auth import AgentAuthMiddleware, make_auth_router
from disco.agent_server.routes import preview_proxy
from disco.agent_server.routes.preview import make_preview_router
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
from disco.core.auth import PreviewCapability, intent_ttl_s
from disco.tools import ProcessSandboxService
from disco.tools.projects import SnapshotResult, StorageStatus
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

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
    rt.projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
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
    rt.projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
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
    rt.projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
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
    rt.projects.current_project_store = MagicMock(return_value=project_store)  # type: ignore[method-assign]
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
        count = await rt.lifecycle.sweep_idle_once()

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
        count = await rt.lifecycle.sweep_idle_once()

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
        count = await rt.lifecycle.sweep_idle_once()

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
        count = await rt.lifecycle.sweep_idle_once()

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
        count = await rt.lifecycle.sweep_idle_once()
    assert count == 0

    # With TTL=0, swept.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.lifecycle.sweep_idle_once()
    assert count == 1


async def test_no_executor_not_swept():
    """A conversation without a live executor is skipped (nothing to suspend)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    await _make_conversation(store, ConversationStatus.FINISHED)
    # No executor injected.
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.lifecycle.sweep_idle_once()
    assert count == 0


# ---- sandbox_state -----------------------------------------------------------


async def test_sandbox_state_active_with_executor():
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-active"
    rt._run_resources.set_executor(cid, MagicMock())
    assert rt.lifecycle.sandbox_state(cid) == "active"


async def test_sandbox_state_no_context():
    """No executor and no snapshot record → None (research surface)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    result = rt.lifecycle.sandbox_state("conv-nobody")
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
    rt.lifecycle._rehydration._rehydrated.add(cid)

    # Inject a fake executor so teardown has something to remove.
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    await rt._teardown_sandbox(cid)

    # The flag MUST be cleared so _maybe_rehydrate runs again after a recreate.
    assert cid not in rt.lifecycle._rehydration._rehydrated


async def test_explicit_teardown_overrides_pending_preview_intent():
    """Delete/kill is authoritative even while a preview handoff is pending."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-preview-explicit-teardown"
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)
    rt.preview.begin_capture(cid)

    await rt._teardown_sandbox(cid)

    fake_executor.kill.assert_awaited_once()
    assert not rt.preview._connections.preview_capture_ownership.active(cid)


async def test_finished_preview_handoff_holds_then_releases_suspend_ownership(
    monkeypatch, tmp_path
):
    """The real meta/capability/redemption seam owns the sandbox through handoff."""
    monkeypatch.setenv("DISCO_AUTH_SECRET", "preview-handoff-seam-secret")
    store = SqliteEventStore(tmp_path / "preview-handoff.sqlite3")
    cid = "conv_a1b2c3d4handoff"
    store.create_conversation(cid, owner_id="owner-a", surface="build")
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="preview"),
        ),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    rt = _runtime(store)
    now = [100.0]
    monkeypatch.setattr(preview_capture_ownership_module.time, "monotonic", lambda: now[0])

    async def slow_preview(_conversation_id: str) -> dict[str, bool]:
        now[0] += intent_ttl_s() + 1.0
        return {"available": True}

    rt.preview.preview = slow_preview
    rt.preview.preview_target_port = lambda _cid: 8000
    fake_store = SimpleNamespace(status=lambda: StorageStatus.OK)
    rt.lifecycle._sandbox.current_project_store = lambda: fake_store  # type: ignore[method-assign]
    rt.lifecycle._store.get_state = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(execution_status=ConversationStatus.PAUSED)
    )
    rt.lifecycle._maybe_snapshot = AsyncMock()  # type: ignore[method-assign]
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    app = FastAPI()
    app.add_middleware(AgentAuthMiddleware, store=store)
    app.include_router(make_auth_router())
    app.include_router(make_preview_router(store, rt))
    with TestClient(app, base_url="http://testserver") as owner:
        metadata = owner.get(f"/conversations/{cid}/preview?owner_id=owner-a")
        assert metadata.status_code == 200, metadata.text
        assert rt.preview._connections.preview_capture_ownership.active(cid)
        assert rt.preview._connections.preview_capture_ownership.remaining(cid) > 0

        # Capability mint is the middle request in the same bounded handoff.
        minted = owner.post(
            f"/conversations/{cid}/preview/capability?owner_id=owner-a",
            json={"target_path": "/", "transport": "path"},
        )
        assert minted.status_code == 200
        assert rt.preview._connections.preview_capture_ownership.active(cid)

        bootstrap_url = minted.json()["bootstrap_url"]
        with TestClient(app, base_url=f"http://{urlsplit(bootstrap_url).netloc}") as preview_client:
            redeemed = _redeem(
                preview_client,
                bootstrap_url,
                minted.json()["bootstrap_intent"],
            )
            preview_response = preview_client.get(f"/__disco/isolated-preview/{cid}/")
            assert preview_response.status_code in {200, 404, 503}
        assert redeemed.status_code == 200
        # Redemption releases its own capability generation; the metadata
        # handoff remains bounded until its owner completes or expires.
        assert rt.preview._connections.preview_capture_ownership.active(cid)
        remaining = rt.preview._connections.preview_capture_ownership._owners[cid]
        assert len(remaining) == 1
        rt.preview.complete_capture(cid, next(iter(remaining)))
        assert not rt.preview._connections.preview_capture_ownership.active(cid)

    rt.connections.on_disconnect(cid, grace_s=0.0)
    await rt.connections._suspend_tasks[cid]
    fake_executor.kill.assert_awaited_once()


async def test_preview_redemption_holds_capture_lock_across_slow_restore_read() -> None:
    """The redemption request owns the sandbox through resolution and body read."""
    store = SqliteEventStore(":memory:")
    cid = "conv_slow_preview_capture"
    rt = _runtime(store)
    generation = rt.preview.begin_capture(cid)
    capability = PreviewCapability(
        owner_id="owner-a",
        conversation_id=cid,
        port=5173,
        http_methods=("GET",),
        path_prefix="/",
        expires_at=2_000_000_000,
        capture_generation=generation,
    )
    request = SimpleNamespace(state=SimpleNamespace(preview_capability=capability))
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_response(*_args, **_kwargs):
        assert rt.preview._connections.preview_capture_ownership.lock_for(cid).locked()
        started.set()
        await release.wait()
        return Response("preview", status_code=200)

    task = None
    blocked = None
    lock = rt.preview._connections.preview_capture_ownership.lock_for(cid)
    try:
        with patch.object(preview_proxy, "_resolved_preview_response", slow_response):
            task = asyncio.create_task(
                preview_proxy._preview_app_response(
                    store,
                    rt,
                    cast(Request, request),
                    cid,
                    "/",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            blocked = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0)
            assert not blocked.done()
            release.set()
            response = await asyncio.wait_for(task, timeout=1.0)
            await asyncio.wait_for(blocked, timeout=1.0)
            lock.release()
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=1.0)
        if blocked is not None and not blocked.done():
            await asyncio.wait_for(blocked, timeout=1.0)
        if blocked is not None and blocked.done() and lock.locked():
            lock.release()

    assert response.status_code == 200
    assert not rt.preview._connections.preview_capture_ownership.active(cid)


async def test_owner_preview_uses_one_capture_transaction() -> None:
    """A non-capability preview resolves under the same single outer lease."""
    store = SqliteEventStore(":memory:")
    cid = "conv_owner_preview_capture"
    rt = _runtime(store)
    request = SimpleNamespace(state=SimpleNamespace(preview_capability=None))

    async def resolved(*_args, **_kwargs):
        lock = rt.preview._connections.preview_capture_ownership.lock_for(cid)
        assert lock.locked()
        return Response("preview", status_code=200)

    with (
        patch.object(preview_proxy, "require_owned_conversation", return_value=cid),
        patch.object(
            preview_proxy,
            "current_session",
            return_value=SimpleNamespace(owner_id="owner-a"),
        ),
        patch.object(preview_proxy, "_resolved_preview_response", resolved),
    ):
        response = await asyncio.wait_for(
            preview_proxy._preview_app_response(
                store,
                rt,
                cast(Request, request),
                cid,
                "/",
            ),
            timeout=1.0,
        )

    assert response.status_code == 200
    assert not rt.preview._connections.preview_capture_ownership.lock_for(cid).locked()


async def test_rehydrate_after_recreate_clears_flag_and_rehydrates():
    """Mid-run recreate hook (bp-13 §2, orchestrator fix): _rehydrate_after_recreate
    must clear the idempotency flag THEN re-run _maybe_rehydrate — the mid-run drop
    path never goes through _teardown_sandbox, so without this hook the fresh
    instance stayed empty (conv_f3bdc842: 'all files were lost')."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-midrun-recreate"
    rt.lifecycle._rehydration._rehydrated.add(
        cid
    )  # the run already rehydrated once before the drop

    rt.lifecycle._rehydration._maybe_rehydrate = AsyncMock()
    await rt.lifecycle._rehydrate_after_recreate(cid)

    assert (
        cid not in rt.lifecycle._rehydration._rehydrated
    )  # flag cleared BEFORE the rehydrate call
    rt.lifecycle._rehydration._maybe_rehydrate.assert_awaited_once_with(cid)


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
    builds a fresh instance). Unknown and foreign-owned conversation_ids are
    NOT owned by this runtime (shared or isolated stores) and must be retained;
    only a locally-owned terminal status authorizes destroy."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    idle_cid = await _make_conversation(store, ConversationStatus.IDLE)
    foreign_cid = "conv-foreign-terminal"
    store.create_conversation(foreign_cid, owner_id="foreign")
    await store.append(
        foreign_cid,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hello")),
    )
    await store.append(foreign_cid, StatusEvent(status=ConversationStatus.FINISHED))

    svc = MagicMock()
    svc.list_live_instances = AsyncMock(return_value=[idle_cid, "conv-ghost-no-row", foreign_cid])
    svc.destroy_by_conversation = AsyncMock()
    rt.sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    await rt.lifecycle.reconcile_orphaned_runs()

    destroyed = {c.args[0] for c in svc.destroy_by_conversation.await_args_list}
    assert idle_cid in destroyed, "IDLE conversation's container must be swept"
    assert "conv-ghost-no-row" not in destroyed, (
        "unknown conversation's container is unowned and must be retained"
    )
    assert foreign_cid not in destroyed, "foreign-owned terminal container must be retained"


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
    rt.sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    await rt.lifecycle.reconcile_orphaned_runs()

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
    rt.sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    start = time.perf_counter()
    await rt.lifecycle.reconcile_orphaned_runs()
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
    rt.sandbox._sandbox_service_now = MagicMock(return_value=svc)  # type: ignore[method-assign]

    await rt.lifecycle.reconcile_orphaned_runs()

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
    rt.mcp._start_mcp_pool = AsyncMock()
    rt.mcp._close_mcp_pool = AsyncMock()
    rt.lifecycle.reconcile_orphaned_runs = AsyncMock()
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
    rt.run_registry.register_task(cid, task)  # type: ignore[arg-type]
    try:
        with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
            count = await rt.lifecycle.sweep_idle_once()
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
    rt.run_registry.register_task(cid, t)  # type: ignore[arg-type]
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.lifecycle.sweep_idle_once()
    assert count == 1
    assert not rt._run_resources.has_executor(cid)  # a done task does not block suspend


async def test_suspend_releases_workspace_lock_before_kill(tmp_path):
    """Regression for preview-harness timeout: concurrent auto-suspend held the
    workspace lock while awaiting slow Podman `executor.kill()` (~2.5 min),
    blocking sealed-preview restoration which also needs that lock. `_suspend`
    must detach synchronously inside the lock and reclaim outside, so the lock
    is already free while kill is blocked and the old executor is already
    detached."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, ConversationStatus.PAUSED)

    kill_entered = asyncio.Event()
    kill_release = asyncio.Event()

    async def _blocked_kill() -> None:
        kill_entered.set()
        await kill_release.wait()

    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock(side_effect=_blocked_kill)
    rt._run_resources.set_executor(cid, fake_executor)
    # Minimal suspend path for a PAUSED conversation: patch the durable snapshot
    # so the test only exercises the detach-vs-reclaim lock ordering.
    rt.lifecycle._maybe_snapshot = AsyncMock()  # type: ignore[method-assign]

    suspend_task = asyncio.create_task(rt.lifecycle._suspend(cid))

    # Wait until kill is entered — proves _suspend reached the reclaim phase.
    await asyncio.wait_for(kill_entered.wait(), timeout=2)

    # Acquire the same fence from a separate task while old kill remains blocked.
    lock = rt._workspace_fence.lock(cid)
    lock_acquired = asyncio.Event()

    async def _probe_fence() -> None:
        async with lock:
            lock_acquired.set()

    probe_task = asyncio.create_task(_probe_fence())
    await asyncio.wait_for(lock_acquired.wait(), timeout=2)
    assert not rt._run_resources.has_executor(cid), "old executor must be detached before kill"

    # Release the blocked kill and let suspend finish.
    kill_release.set()
    await asyncio.wait_for(suspend_task, timeout=2)
    await probe_task


async def test_hard_kill_waits_for_detached_suspend_reclaim(tmp_path, monkeypatch):
    """A successful hard kill owns teardown already detached by auto-suspend."""

    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, ConversationStatus.PAUSED)

    reclaim_entered = asyncio.Event()
    reclaim_release = asyncio.Event()
    hard_kill_joined = asyncio.Event()
    join_calls = 0

    async def _blocked_kill() -> None:
        reclaim_entered.set()
        await reclaim_release.wait()

    original_await_reclaims = rt._run_resources.await_reclaims

    async def _observed_await_reclaims(conversation_id: str) -> None:
        nonlocal join_calls
        join_calls += 1
        if join_calls == 2:
            hard_kill_joined.set()
        await original_await_reclaims(conversation_id)

    monkeypatch.setattr(rt._run_resources, "await_reclaims", _observed_await_reclaims)
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock(side_effect=_blocked_kill)
    rt._run_resources.set_executor(cid, fake_executor)
    rt.lifecycle._maybe_snapshot = AsyncMock()  # type: ignore[method-assign]

    suspend_task = asyncio.create_task(rt.lifecycle._suspend(cid))
    await asyncio.wait_for(reclaim_entered.wait(), timeout=2)
    assert rt._run_resources.has_reclaims(cid)

    hard_kill = asyncio.create_task(rt.kill(cid))
    await asyncio.wait_for(hard_kill_joined.wait(), timeout=2)
    assert not hard_kill.done()
    assert (await store.get_state(cid)).execution_status is ConversationStatus.PAUSED

    reclaim_release.set()
    await asyncio.wait_for(asyncio.gather(suspend_task, hard_kill), timeout=2)
    assert not rt._run_resources.has_reclaims(cid)
    assert (await store.get_state(cid)).execution_status is ConversationStatus.IDLE
