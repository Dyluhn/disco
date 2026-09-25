"""LIVE identity is fresh metadata; process discovery belongs to status polling."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest
from disco.agent_server.host_proxy import HostPreviewProxyMiddleware, make_preview_session_resolver
from disco.agent_server.host_proxy_parts import _live_http
from disco.agent_server.live_session_directory import LiveSessionDirectory
from disco.agent_server.preview_manager import PreviewManager
from disco.agent_server.preview_models import PreviewSession, PreviewStatus
from disco.agent_server.preview_service import PreviewService
from disco.agent_server.routes.preview_capability import _canonical_preview_authority
from disco.agent_server.run_registry import RunResourceRegistry
from disco.core import ConversationStatus, DeliverableEvent, StatusEvent
from disco.core.auth import (
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_COOKIE,
    AuthSession,
    PreviewCapabilitySigner,
)
from disco.core.store.sqlite import SqliteEventStore
from starlette.applications import Starlette


@pytest.fixture
async def live_identity(monkeypatch):
    cid = "conv_aaaaaaaafull"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="owner-a", surface="build")
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    await store.append(cid, DeliverableEvent(title="App", path="index.html", artifact_kind="app"))
    probe = AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="[]"))
    sandbox = SimpleNamespace(
        id="sandbox-one",
        generation=1,
        shares_host_network=False,
        _instance=SimpleNamespace(exec_shell=probe),
        sessions=SimpleNamespace(namespace="owned"),
        destroy=AsyncMock(),
    )
    manager = PreviewManager(sandbox, port_pool=[8000])
    sandbox._preview_manager = manager
    selected = PreviewSession(
        name="web",
        port=8000,
        command="python app.py 8000",
        exec_dir="/workspace",
        intent={},
        status=PreviewStatus.RUNNING,
        sandbox_instance_id=sandbox.id,
        sandbox_generation=1,
    )
    manager._sessions["web"] = selected
    manager._resource._selection_order.append("web")
    registry = RunResourceRegistry()
    executor = SimpleNamespace(sandbox=sandbox)
    registry.set_executor(cid, executor)
    directory = LiveSessionDirectory(registry, store)
    service = PreviewService(directory, store, None, None, None, None, None, None, None)
    runtime = SimpleNamespace(preview=service)
    state = SimpleNamespace(
        cid=cid,
        store=store,
        sandbox=sandbox,
        manager=manager,
        selected=selected,
        registry=registry,
        executor=executor,
        directory=directory,
        service=service,
        runtime=runtime,
        probe=probe,
    )
    try:
        yield state
    finally:
        store.close()


async def _client(state, monkeypatch):
    """Use the real authority, service, projection and owner directory in middleware."""
    authority = await _canonical_preview_authority(state.store, state.runtime, state.cid, 8000)
    assert authority is not None
    signer = PreviewCapabilitySigner(redemption_store=state.store)
    intent = signer.mint_intent(
        session=AuthSession("owner-a", "csrf", "s", 2**31),
        conversation_id=state.cid,
        port=8000,
        target_path="/",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
        authority_id=authority,
    )
    redeemed = signer.redeem_intent(
        intent, cid8="aaaaaaaa", port=8000, path_scope="host", partitioned_cookies=True
    )
    assert redeemed is not None
    state.headers = {
        "Cookie": f"{PREVIEW_COOKIE}={redeemed[0]}",
        "Origin": "http://p2-aaaaaaaa-8000.localhost",
    }
    state.identity_reads = Mock(wraps=state.service.live_preview_identity)
    monkeypatch.setattr(state.service, "live_preview_identity", state.identity_reads)
    state.availability = AsyncMock(side_effect=AssertionError("HTTP must not poll status"))
    monkeypatch.setattr(state.service, "preview", state.availability)
    state.owner_reads = AsyncMock(wraps=state.directory.resolve_owned_cid_prefix)
    monkeypatch.setattr(state.directory, "resolve_owned_cid_prefix", state.owner_reads)
    state.inside = AsyncMock(return_value=(201, [("Content-Type", "text/plain")], b"created"))
    monkeypatch.setattr(_live_http, "request_inside", state.inside)
    state.upstream = AsyncMock(side_effect=AssertionError("no fallback or duplicate dispatch"))
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=state.upstream,
        session_resolver=make_preview_session_resolver(state.directory),
        require_capability=True,
        redemption_store=state.store,
        canonical_authority_resolver=lambda cid, port, _version: _canonical_preview_authority(
            state.store, state.runtime, cid, port
        ),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://p2-aaaaaaaa-8000.localhost"
    )


async def test_live_http_reads_fresh_identity_twice_without_process_probes(
    live_identity, monkeypatch
):
    state = live_identity
    async with await _client(state, monkeypatch) as client:
        response = await client.post("/api/items", headers=state.headers, content=b"new item")
    assert response.status_code == 201
    assert state.identity_reads.call_args_list == [call(state.cid), call(state.cid)]
    assert state.owner_reads.await_args_list == [
        call("aaaaaaaa", "owner-a"),
        call("aaaaaaaa", "owner-a"),
    ]
    state.availability.assert_not_awaited()
    state.probe.assert_not_awaited()
    state.inside.assert_awaited_once()
    assert state.inside.call_args.args[:4] == (state.sandbox, 8000, "POST", "/api/items")
    state.upstream.assert_not_awaited()


@pytest.mark.parametrize("change", ["generation", "entry", "owner", "selection", "detached"])
async def test_live_identity_changes_during_body_never_dispatch(live_identity, monkeypatch, change):
    state = live_identity
    async with await _client(state, monkeypatch) as client:

        async def body():
            yield b"first"
            if change == "generation":
                state.selected.projection_id = "pv_restarted"
            elif change == "entry":
                await state.store.append(
                    state.cid,
                    DeliverableEvent(title="Other", path="other/index.html", artifact_kind="app"),
                )
            elif change == "owner":
                monkeypatch.setattr(
                    state.store, "conversation_owned_by", AsyncMock(return_value=False)
                )
            elif change == "selection":
                state.manager._sessions["web"] = replace(state.selected, projection_id="pv_other")
            else:
                state.registry.pop_executor(state.cid)
            yield b"last"

        response = await client.post("/api/items", headers=state.headers, content=body())
    assert response.status_code == 409
    assert state.identity_reads.call_count == 2
    state.probe.assert_not_awaited()
    state.inside.assert_not_awaited()
    state.upstream.assert_not_awaited()


@pytest.mark.parametrize(
    "condition",
    [
        "running",
        "no_session",
        "no_manager",
        "no_instance",
        "starting",
        "crashed",
        "unavailable",
        "stopped",
        "closed_pending",
        "disposed",
        "malformed_port",
        "missing_generation",
        "malformed_registry",
    ],
)
@pytest.mark.parametrize("isolated", [False, True])
async def test_identity_matches_previous_status_metadata_without_probing(
    live_identity, monkeypatch, condition, isolated
):
    state = live_identity
    state.manager._resource._owns_sandbox = isolated
    if condition == "no_session":
        state.registry.pop_executor(state.cid)
    elif condition == "no_manager":
        state.sandbox._preview_manager = None
        # Legacy status selection consults surface policy without a manager.
        state.service._runtime_projection._settings = SimpleNamespace(_surface_of=lambda _: "build")
    elif condition == "no_instance":
        state.sandbox._instance = None
    elif condition in {"starting", "crashed", "unavailable", "stopped"}:
        state.selected.status = PreviewStatus(condition)
    elif condition == "closed_pending":
        state.manager._resource._closed = True
    elif condition == "disposed":
        # Exercise actual disposal without process control: this fake owns its box.
        state.manager._resource._owns_sandbox = True
        await state.manager.aclose()
    elif condition == "malformed_port":
        state.selected.port = True
    elif condition == "missing_generation":
        state.selected.projection_id = ""
    elif condition == "malformed_registry":
        monkeypatch.setattr(
            state.manager, "canonical_lifecycle_session", Mock(side_effect=ValueError)
        )
    metadata = await state.service.preview(state.cid)
    generation, port = metadata["generation"], metadata["port"]
    expected = (
        (generation, port)
        if isinstance(generation, str) and generation and type(port) is int
        else None
    )
    state.probe.reset_mock()
    assert state.service.live_preview_identity(state.cid) == expected
    state.probe.assert_not_awaited()
    if condition in {"starting", "crashed", "stopped", "disposed"}:
        assert state.manager.canonical_port() is None
    if condition in {
        "stopped",
        "disposed",
        "no_session",
        "no_manager",
        "missing_generation",
        "malformed_registry",
    }:
        assert expected is None


async def test_identity_port_mismatch_cannot_mint_authority(live_identity):
    state = live_identity
    assert await _canonical_preview_authority(state.store, state.runtime, state.cid, 8001) is None
    state.probe.assert_not_awaited()
