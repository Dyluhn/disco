"""An explicit live launch must serve mutable bytes, never the sealed copy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from disco.agent_server import build_loop_components, preview_live_handoff
from disco.agent_server.preview_manager import PreviewManager
from disco.agent_server.preview_status import live_preview_identity, managed_preview_metadata
from disco.agent_server.routes.preview_capability import _live_preview_authority
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.preview import PreviewStartArgs, PreviewStartTool, PreviewStatusTool
from test_preview_resource_contract import _FakeSandbox, _sealed_contract


class _WorkspaceSandbox(_FakeSandbox):
    def __init__(self, identity: str, content: bytes) -> None:
        super().__init__()
        self.id = identity
        self.files = {"index.html": content}
        self.healthy = True
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self._preview_manager = None

    async def fetch_inside(self, port, path, *, timeout_s=5):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if not self.healthy or port not in self._serving:
            return None
        return 200, self.files["index.html"], "text/html"


def _ctx(host):
    return ToolContext(
        sandbox=host,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="local",
        conversation_id="conv_handoff",
    )


@pytest.fixture
async def handoff(monkeypatch):
    sealed = _WorkspaceSandbox("sbx_preview_manager_test", b"sealed bytes")
    host = _WorkspaceSandbox("sbx_mutable_host", b"edited host bytes")
    old = PreviewManager(sealed, owns_sandbox=True, port_pool=[3000, 5173], health_attempts=1)
    host._preview_manager = old
    restored = await old.restore_sealed(_sealed_contract(7, "a"))
    assert restored is not None
    candidates = []

    def candidate(sandbox):
        assert sandbox is host
        result = PreviewManager(sandbox, port_pool=[3000, 5173], health_attempts=1)
        candidates.append(result)
        return result

    monkeypatch.setattr(preview_live_handoff, "PreviewManager", candidate)
    state = SimpleNamespace(
        host=host, sealed=sealed, old=old, restored=restored, candidates=candidates
    )
    try:
        yield state
    finally:
        host.release = None
        sealed.fail_destroy = False
        for manager in [old, *candidates]:
            await manager.aclose()


async def _start(state):
    return await PreviewStartTool().run(
        PreviewStartArgs(serve_dir=".", name="web"), _ctx(state.host)
    )


async def test_explicit_launch_serves_current_and_future_host_bytes_with_new_authority(handoff):
    state = handoff
    identity = live_preview_identity(state.host)

    async def metadata(cid):
        assert cid == "conv_handoff"
        return managed_preview_metadata(state.host._preview_manager)[0]

    runtime = SimpleNamespace(preview=SimpleNamespace(preview=metadata))
    authority = await _live_preview_authority(runtime, [], "conv_handoff", 3000)
    assert isinstance(authority, str) and authority
    outcome = await _start(state)
    assert outcome.success
    manager = state.host._preview_manager
    assert manager is not state.old and not manager.owns_sandbox()
    assert manager._sandbox is state.host
    assert live_preview_identity(state.host) != identity
    assert await _live_preview_authority(runtime, [], "conv_handoff", 3000) != authority
    assert await _live_preview_authority(runtime, [], "conv_handoff", 5173) is None
    selected = manager.canonical_session()
    assert selected.sandbox_instance_id == state.host.id
    assert (await manager._sandbox.fetch_inside(selected.port, "/"))[1] == b"edited host bytes"
    state.host.files["index.html"] = b"later host edit"
    assert (await manager._sandbox.fetch_inside(selected.port, "/"))[1] == b"later host edit"
    assert state.sealed.files == {"index.html": b"sealed bytes"}
    assert state.sealed.destroy_calls == 1
    assert state.host.destroy_calls == 0


async def test_old_owner_remains_routable_until_replacement_health_answers(handoff):
    state = handoff
    state.host.entered, state.host.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_start(state))
    try:
        await asyncio.wait_for(state.host.entered.wait(), 1)
        assert state.host._preview_manager is state.old
        assert state.old.canonical_session() is state.restored
        assert (await state.sealed.fetch_inside(3000, "/"))[1] == b"sealed bytes"
        assert "previous sealed app" in managed_preview_metadata(state.old)[0]["update_error"]
        assert state.sealed.destroy_calls == 0
    finally:
        state.host.release.set()
        outcome = await task
    assert outcome.success
    assert state.host._preview_manager is not state.old


@pytest.mark.parametrize(
    "failure", ["unhealthy", "owner_changed", "generation_changed", "cancelled"]
)
async def test_failed_handoff_keeps_previous_owner_and_reports_old_bytes(handoff, failure):
    state = handoff
    identity = live_preview_identity(state.host)
    state.host.entered, state.host.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_start(state))
    try:
        await asyncio.wait_for(state.host.entered.wait(), 1)
        if failure == "unhealthy":
            state.host.healthy = False
        elif failure == "owner_changed":
            state.host._preview_manager = replacement = SimpleNamespace()
        elif failure == "generation_changed":
            state.restored.projection_id = "pv_new_owner_generation"
        else:
            task.cancel()
    finally:
        state.host.release.set()
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result.success and result.error == "preview_update_failed"
        if failure in {"owner_changed", "generation_changed"}:
            assert (
                result.content == "Preview ownership changed; this update installed no replacement."
            )
        else:
            assert "previous sealed app" in result.content
    assert state.host._preview_manager is (replacement if failure == "owner_changed" else state.old)
    if failure in {"unhealthy", "cancelled"}:
        assert live_preview_identity(state.host) == identity
    assert state.old.canonical_session() is state.restored
    assert state.sealed.destroy_calls == 0
    assert "previous sealed app" in managed_preview_metadata(state.old)[0]["update_error"]
    assert state.candidates[0]._closed
    assert not state.host._serving


async def test_readonly_status_does_not_replace_sealed_owner(handoff):
    result = await PreviewStatusTool().run(SimpleNamespace(name=None), _ctx(handoff.host))
    assert result.success
    assert handoff.host._preview_manager is handoff.old
    assert not handoff.candidates
    assert handoff.sealed.destroy_calls == 0


@pytest.mark.parametrize("install_failure", ["", "dependency install failed"])
async def test_appkit_sync_uses_same_host_workspace_or_preserves_sealed_owner(
    handoff, monkeypatch, install_failure
):
    preparation = AsyncMock(
        return_value=build_loop_components._PreviewPreparation(install_failure=install_failure)
    )
    monkeypatch.setattr(build_loop_components, "_prepare_appkit_dependencies", preparation)
    await build_loop_components._sync_appkit_live_preview(handoff.host)
    preparation.assert_awaited_once_with(handoff.host)
    if install_failure:
        assert handoff.host._preview_manager is handoff.old
        assert not handoff.candidates
        assert install_failure in handoff.restored.update_error
    else:
        assert handoff.host._preview_manager is not handoff.old
        assert handoff.host._preview_manager._sandbox is handoff.host
        assert handoff.sealed.files == {"index.html": b"sealed bytes"}


async def test_retired_cleanup_failure_remains_owned_for_teardown_retry(handoff):
    handoff.sealed.fail_destroy = True
    result = await _start(handoff)
    assert result.success
    live = handoff.host._preview_manager
    assert live is not handoff.old
    assert live._retired_managers == [handoff.old]
    assert handoff.host.destroy_calls == 0
    handoff.sealed.fail_destroy = False
    await live.aclose()
    assert not live._retired_managers
    assert handoff.sealed.destroy_calls == 2


async def test_handoff_cleanup_can_overlap_current_owner_teardown(handoff, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = handoff.old.aclose
    calls = 0

    async def close():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        await original()

    monkeypatch.setattr(handoff.old, "aclose", close)
    task = asyncio.create_task(_start(handoff))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        live = handoff.host._preview_manager
        assert live is not handoff.old
        await live.aclose()
        assert not live._retired_managers
    finally:
        release.set()
    await task
    assert live._closed
    assert handoff.host.destroy_calls == 0
    assert handoff.sealed.destroy_calls == 1


async def test_failed_candidate_cleanup_retains_owner_and_original_error(handoff, monkeypatch):
    handoff.host.healthy = False
    original_stop = handoff.host.sessions.stop_foreground_server
    stop = AsyncMock(side_effect=RuntimeError("candidate termination failed"))
    monkeypatch.setattr(handoff.host.sessions, "stop_foreground_server", stop)
    result = await _start(handoff)
    assert not result.success and result.error == "preview_update_failed"
    assert "successful HTTP response" in result.content
    assert handoff.host._preview_manager is handoff.old
    assert handoff.old._retired_managers == handoff.candidates
    assert handoff.host._serving
    assert handoff.sealed.destroy_calls == 0
    monkeypatch.setattr(handoff.host.sessions, "stop_foreground_server", original_stop)
    await handoff.old.aclose()
    assert not handoff.old._retired_managers
    assert not handoff.host._serving
    assert handoff.host.destroy_calls == 0


async def test_health_proven_inside_only_host_runtime_can_replace_sealed_owner(handoff):
    handoff.host._can_expose = False
    result = await _start(handoff)
    assert result.success
    live = handoff.host._preview_manager
    assert live is not handoff.old
    selected = live.canonical_session()
    assert selected.status.value == "unavailable" and selected.url is None
    assert selected.sandbox_instance_id == handoff.host.id
    assert (await handoff.host.fetch_inside(selected.port, "/"))[1] == b"edited host bytes"
    assert handoff.sealed.destroy_calls == 1


async def test_already_closing_owner_does_not_launch_candidate(handoff):
    handoff.old._resource._closed = True
    result = await _start(handoff)
    assert not result.success and result.error == "preview_update_failed"
    assert "closing" in result.content
    assert not handoff.candidates
    assert not handoff.host._serving


async def test_parent_close_during_candidate_health_drains_both_owned_managers(handoff):
    handoff.host.entered, handoff.host.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_start(handoff))
    closing = None
    try:
        await asyncio.wait_for(handoff.host.entered.wait(), 1)
        assert handoff.old._retired_managers == handoff.candidates
        closing = asyncio.create_task(handoff.old.aclose())
        await asyncio.sleep(0)
        assert handoff.old._closed
    finally:
        handoff.host.release.set()
        if closing is not None:
            await closing
        result = await task
    assert not result.success
    assert handoff.host._preview_manager is handoff.old
    assert not handoff.old._retired_managers
    assert not handoff.host._serving
    assert handoff.sealed.destroy_calls == 1
