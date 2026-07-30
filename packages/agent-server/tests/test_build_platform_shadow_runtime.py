from __future__ import annotations

from unittest import mock

from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import DefaultLLMRouter
from disco.core.loop import RouterAgent
from disco.tools import AppKitToolExecutor, DefaultToolExecutor


def _compose(
    monkeypatch,
    *,
    conversation_id: str,
    appkit: bool,
) -> tuple[ConversationRuntime, object]:
    monkeypatch.setenv("DISCO_BUILD_PLATFORM_SHADOW", "1")
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface(conversation_id, "agent")
    if appkit:
        runtime.set_appkit_mode(conversation_id, True)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    with mock.patch.object(runtime, "_sandbox_service_now"):
        loop = runtime._compose_build_loop(conversation_id, router, agent)
    return runtime, loop


def test_runtime_records_freeform_shadow_without_routing_it(monkeypatch) -> None:
    runtime, loop = _compose(
        monkeypatch,
        conversation_id="shadow-freeform",
        appkit=False,
    )
    record = runtime._build_shadows.snapshot()["shadow-freeform"]
    assert record.matches
    assert record.source == "freeform"
    assert record.active_route == "legacy"
    assert isinstance(loop.executor, DefaultToolExecutor)
    assert not isinstance(loop.executor, AppKitToolExecutor)


def test_runtime_records_appkit_shadow_and_keeps_strict_executor(monkeypatch) -> None:
    runtime, loop = _compose(
        monkeypatch,
        conversation_id="shadow-appkit",
        appkit=True,
    )
    record = runtime._build_shadows.snapshot()["shadow-appkit"]
    assert record.matches
    assert record.source == "appkit"
    assert record.active_route == "legacy"
    assert isinstance(loop.executor, AppKitToolExecutor)


def test_shadow_disabled_has_zero_runtime_observation(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_BUILD_PLATFORM_SHADOW", raising=False)
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface("shadow-off", "agent")
    with mock.patch.object(runtime, "_sandbox_service_now"):
        loop = runtime._compose_build_loop(
            "shadow-off",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert runtime._build_shadows.snapshot() == {}
    assert isinstance(loop.executor, DefaultToolExecutor)


def test_broken_observer_cannot_break_legacy_loop_composition(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_BUILD_PLATFORM_SHADOW", "1")
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface("shadow-broken", "agent")
    with (
        mock.patch.object(runtime, "_sandbox_service_now"),
        mock.patch(
            "disco.agent_server.build_loop_assembler.observe_legacy_build",
            side_effect=RuntimeError("observer defect"),
        ),
    ):
        loop = runtime._compose_build_loop(
            "shadow-broken",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert runtime._build_shadows.snapshot() == {}
    assert isinstance(loop.executor, DefaultToolExecutor)
