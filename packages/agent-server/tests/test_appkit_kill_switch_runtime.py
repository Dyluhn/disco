"""DISCO_APPKIT_ENABLED=0 — the AppKit kill switch, runtime + route layer.

The read gate lives in ``_effective_appkit_mode`` (the single choke point every
executor-composition decision flows through), so an EXISTING appkit
conversation degrades to the plain free-form executor while the flag is off —
and comes back when it is re-enabled, no stored state touched. The create
route refuses an explicit appkit_mode request loudly (409) rather than
silently downgrading it.
"""

from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.routes._common import CreateConversationBody
from disco.agent_server.routes.conversations import _create_conversation_response
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.flags import APPKIT_ENABLED_ENV
from disco.core.llm import DefaultLLMRouter
from disco.core.loop import RouterAgent
from disco.tools import AppKitToolExecutor, DefaultToolExecutor
from fastapi import HTTPException


def _rt() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"))


def _loop_for(rt: ConversationRuntime, cid: str):
    rt.set_surface(cid, "agent")
    rt.set_appkit_mode(cid, True)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._compose_build_loop(cid, router, agent)
    rt._loops[cid] = loop
    return loop


def test_effective_appkit_mode_gated_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _rt()
    rt.set_appkit_mode("c1", True)
    monkeypatch.setenv(APPKIT_ENABLED_ENV, "0")
    assert rt._effective_appkit_mode("c1") is False
    # Re-enable: the stored per-conversation flag was never touched.
    monkeypatch.delenv(APPKIT_ENABLED_ENV, raising=False)
    assert rt._effective_appkit_mode("c1") is True


def test_effective_appkit_mode_recovers_from_store_not_runtime_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(APPKIT_ENABLED_ENV, raising=False)
    store = SqliteEventStore(":memory:")
    store.create_conversation("persisted", appkit_mode=True)
    rt = ConversationRuntime(store)
    rt._appkit_mode.clear()
    assert rt._effective_appkit_mode("persisted") is True
    with pytest.raises(ValueError, match="immutable"):
        rt.set_appkit_mode("persisted", False)


def test_runtime_rejects_artifact_appkit_identity_collision() -> None:
    rt = _rt()
    rt.set_appkit_mode("appkit", True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        rt.set_artifact_mode("appkit", True)

    rt.set_artifact_mode("artifact", True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        rt.set_appkit_mode("artifact", True)


def test_disabled_flag_composes_plain_executor_for_appkit_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APPKIT_ENABLED_ENV, "0")
    rt = _rt()
    loop = _loop_for(rt, "kd1")
    assert isinstance(loop.executor, DefaultToolExecutor)
    assert not isinstance(loop.executor, AppKitToolExecutor)
    # Full separation: the strict-mode mutators are not callable either.
    assert "app_create" not in loop.executor.callable_tool_names()


def test_enabled_flag_keeps_strict_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APPKIT_ENABLED_ENV, raising=False)
    rt = _rt()
    loop = _loop_for(rt, "ke1")
    assert isinstance(loop.executor, AppKitToolExecutor)


@pytest.mark.asyncio
async def test_create_route_refuses_appkit_mode_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APPKIT_ENABLED_ENV, "0")
    store = SqliteEventStore(":memory:")
    body = CreateConversationBody(surface="agent", appkit_mode=True)
    with pytest.raises(HTTPException) as exc_info:
        await _create_conversation_response(store, None, body, None)
    assert exc_info.value.status_code == 409
    assert "DISCO_APPKIT_ENABLED" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_create_route_untouched_without_appkit_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The switch must remove ONLY appkit: a plain create still works while off.
    monkeypatch.setenv(APPKIT_ENABLED_ENV, "0")
    store = SqliteEventStore(":memory:")
    body = CreateConversationBody(surface="agent")
    result = await _create_conversation_response(store, None, body, None)
    assert result["conversation_id"].startswith("conv_")


@pytest.mark.asyncio
async def test_create_route_rejects_artifact_and_appkit_together() -> None:
    store = SqliteEventStore(":memory:")
    body = CreateConversationBody(surface="agent", artifact_mode=True, appkit_mode=True)
    with pytest.raises(HTTPException) as exc_info:
        await _create_conversation_response(store, None, body, None)
    assert exc_info.value.status_code == 409
    assert "mutually exclusive" in str(exc_info.value.detail)
    assert await store.list_conversations(owner_id="local") == []
