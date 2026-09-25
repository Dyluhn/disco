"""A parked Preview owner must survive successful driver recomposition."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from disco.agent_server.build_messages import _build_brief_message
from disco.agent_server.preview_manager import PreviewManager
from disco.agent_server.preview_status import live_preview_identity
from disco.agent_server.routes.preview_capability import _canonical_preview_authority
from disco.core import ConversationStatus, DeliverableEvent, StatusEvent
from disco.core.appkit import BuildBrief
from test_driver_context_resolution import _runtime, _seed_conversation
from test_preview_resource_contract import _FakeSandbox, _sealed_contract


@pytest.fixture
async def parked_preview(monkeypatch):
    rt = _runtime()
    cid = "conv_parked_preview"
    await _seed_conversation(rt, cid)
    await rt._store.append(cid, _build_brief_message(BuildBrief(app_kind="web_app")))
    await rt._store.append(
        cid, DeliverableEvent(title="App", path="dist/index.html", artifact_kind="app")
    )
    await rt._store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    rt._loop_for(cid)
    host = rt.preview.live_session(cid)
    assert host is not None
    sandbox = _FakeSandbox()
    manager = PreviewManager(
        sandbox, port_pool=[3000], health_attempts=1, health_interval_s=0, owns_sandbox=True
    )
    host._preview_manager = manager
    contract = replace(_sealed_contract(7, "a"), conversation_id=cid)
    restored = await manager.restore_sealed(contract)
    assert restored is not None
    start = AsyncMock(wraps=manager.start)
    close = AsyncMock(wraps=manager.aclose)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "aclose", close)
    create = AsyncMock(side_effect=AssertionError("recomposition must not create a sandbox"))
    monkeypatch.setattr(host._service, "create", create)
    snapshot = await rt._resolve_driver_context(cid)
    try:
        yield SimpleNamespace(
            rt=rt, cid=cid, host=host, sandbox=sandbox, manager=manager,
            contract=contract, restored=restored, snapshot=snapshot,
            start=start, close=close, create=create,
        )
    finally:
        await manager.aclose()
        rt._store.close()


@pytest.mark.parametrize("parking", ["contract_fold", "model_change"])
async def test_recomposition_adopts_parked_preview_without_restarting(parked_preview, parking):
    state = parked_preview
    rt, cid = state.rt, state.cid
    if parking == "contract_fold":
        await rt.contract._fold_contract_from_history(cid)
    else:
        rt.settings._evict_model_binding(cid)
    assert rt._run_resources.executor(cid) is None
    assert rt._run_resources.pending_session(cid) is state.host
    assert rt._loop_registry.loop(cid) is None
    identity = live_preview_identity(state.host)
    sandbox_identity = (state.sandbox.id, state.sandbox.generation)

    loop = rt._loop_factory.loop_for_resolved(cid, state.snapshot)

    assert rt.preview.live_session(cid) is state.host
    assert rt._run_resources.pending_session(cid) is None
    assert rt._loop_registry.loop(cid) is loop
    assert state.host._preview_manager is state.manager
    assert state.manager.owns_sandbox()
    assert live_preview_identity(rt.preview.live_session(cid)) == identity
    assert (state.sandbox.id, state.sandbox.generation) == sandbox_identity
    assert await state.manager.resolve_sealed_contract(state.contract) is state.restored
    state.start.assert_not_awaited()
    state.close.assert_not_awaited()
    state.create.assert_not_awaited()
    assert state.sandbox.destroy_calls == 0

    await rt._store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    authority = await _canonical_preview_authority(rt._store, rt, cid, 3000)
    assert authority is not None
    assert await _canonical_preview_authority(rt._store, rt, cid, 5173) is None
    assert await rt.live_sessions.resolve_owned_cid_prefix("parked_p", "local") == cid
    assert await rt.live_sessions.resolve_owned_cid_prefix("parked_p", "other-owner") is None
    state.restored.projection_id = "pv_replacement"
    assert await _canonical_preview_authority(rt._store, rt, cid, 3000) != authority
    state.sandbox.generation += 1
    assert await state.manager.resolve_sealed_contract(state.contract) is None


@pytest.mark.parametrize("after_adoption", [False, True])
async def test_failed_recomposition_restores_exact_parked_preview(
    parked_preview, monkeypatch, after_adoption
):
    state = parked_preview
    rt, cid = state.rt, state.cid
    await rt.contract._fold_contract_from_history(cid)
    original = rt._loop_factory._composer.compose_build_loop

    def fail(*args, **kwargs):
        if after_adoption:
            original(*args, **kwargs)
        raise RuntimeError("composition failed")

    compose = Mock(side_effect=fail)
    monkeypatch.setattr(rt._loop_factory._composer, "compose_build_loop", compose)
    with pytest.raises(RuntimeError, match="composition failed"):
        rt._loop_factory.loop_for_resolved(cid, state.snapshot)

    compose.assert_called_once()
    assert rt._run_resources.executor(cid) is None
    assert rt._run_resources.pending_session(cid) is state.host
    assert rt._loop_registry.loop(cid) is None
    assert rt._driver_contexts.compose_snapshot(cid) is None
    assert rt._driver_contexts.resolved_snapshot(cid) is None
    assert state.host._preview_manager is state.manager
    assert await state.manager.resolve_sealed_contract(state.contract) is state.restored
    state.start.assert_not_awaited()
    state.close.assert_not_awaited()
    state.create.assert_not_awaited()
    assert state.sandbox.destroy_calls == 0
