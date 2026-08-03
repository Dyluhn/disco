"""State-free legacy ``ConversationRuntime`` delegates retained until PKG-13."""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    AgentErrorEvent,
    EventSource,
    ObservationEvent,
)

if TYPE_CHECKING:

    from disco.core import ToolCall, ToolResult
    from disco.core.appkit import BuildBrief

    from .runtime import ConversationRuntime


def set_surface(self, conversation_id: str, surface: str) -> None:

    self._settings._set_surface(conversation_id, surface)


def set_model_override(self, conversation_id: str, model_id: str | None) -> None:

    self._settings.set_model_override(conversation_id, model_id)


def set_autonomous(self, conversation_id: str, value: bool = True) -> None:

    self._settings.set_autonomous(conversation_id, value)


def is_autonomous(self, conversation_id: str) -> bool:

    return self._settings.is_autonomous(conversation_id)


def set_quiet(self, conversation_id: str, value: bool = True) -> None:

    self._settings.set_quiet(conversation_id, value)


def is_quiet(self, conversation_id: str) -> bool:

    return self._settings.is_quiet(conversation_id)


def set_assist(self, conversation_id: str, value: bool = True) -> None:

    self._settings.set_assist(conversation_id, value)


def is_assist(self, conversation_id: str) -> bool:

    return self._settings.is_assist(conversation_id)


async def apply_settings_change(
    self,
    conversation_id: str,
    *,
    model_override: str | None = None,
    assist: bool | None = None,
    model_provided: bool | None = None,
) -> bool:

    return await self._settings.apply_settings_change(
        conversation_id, model_override=model_override, assist=assist, model_provided=model_provided
    )


def set_artifact_mode(self, conversation_id: str, on: bool) -> None:

    self._settings.set_artifact_mode(conversation_id, on)


def set_appkit_mode(self, conversation_id: str, on: bool) -> None:

    self._settings.set_appkit_mode(conversation_id, on)


def set_research_sources(self, conversation_id: str, sources: list[str]) -> None:

    self._settings.set_research_sources(conversation_id, sources)


def get_research_sources(self, conversation_id: str | None) -> tuple[str, ...]:

    return self._settings.get_research_sources(conversation_id)


def activate_contract_for_brief(self, conversation_id: str, build_brief: BuildBrief | None) -> None:

    self._contract.activate_contract_for_brief(conversation_id, build_brief)


def set_build_kind(self, conversation_id: str, kind: str | None) -> None:

    self._contract.set_build_kind(conversation_id, kind)


def expected_delivery_mode(self, conversation_id: str) -> str | None:

    return self._contract.expected_delivery_mode(conversation_id)


def note_build_verify_result(self, conversation_id: str, *, passed: bool) -> None:

    self._contract.note_build_verify_result(conversation_id, passed=passed)


def get_last_selected_model(self) -> str | None:

    return self._settings.model_binding.get_last_selected_model()


def set_last_selected_model(self, model_id: str | None) -> None:

    self._settings.model_binding.set_last_selected_model(model_id)


async def execute_pi_tool(self, conversation_id: str, tool_call: ToolCall) -> ToolResult:

    executor = self._run_resources.executor(conversation_id)
    if executor is None:
        self._loop_factory.loop_for(conversation_id)
        executor = self._run_resources.executor(conversation_id)
    if executor is None:
        raise RuntimeError("tool executor was not composed")
    action = ActionEvent(
        source=EventSource.AGENT, thought=f"[pi] {tool_call.tool_name}", tool_call=tool_call
    )
    await self._store.append(conversation_id, action)
    result = await executor.execute(tool_call)
    if result.success:
        await self._store.append(
            conversation_id, ObservationEvent(tool_result=result, action_id=action.id)
        )
    else:
        await self._store.append(
            conversation_id,
            AgentErrorEvent(
                error=result.error or "tool failed",
                action_id=action.id,
                tool_call_id=tool_call.call_id,
            ),
        )
    return result


async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:

    return await self._lifecycle.reconcile_orphaned_runs(owner_id=owner_id)


def sandbox_state(self, conversation_id: str) -> str | None:

    return self._lifecycle.sandbox_state(conversation_id)


def sandbox_instance_ids(self, conversation_id: str) -> list[str]:

    return self._lifecycle.sandbox_instance_ids(conversation_id)


async def sweep_idle_once(self) -> int:

    return await self._lifecycle.sweep_idle_once()


async def sweep_abandoned_gates_once(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:

    return await self._lifecycle.sweep_abandoned_gates_once(owner_id=owner_id)


def running_conversation_ids(self) -> set[str]:

    return set(self._run_registry.active_conversation_ids())


def _install_schedule_compatibility(runtime_cls: type[ConversationRuntime]) -> None:
    """Bridge PKG-11-WORKFLOWS migration; PKG-13-FACADES removes these delegates."""
    runtime_cls.running_conversation_ids = running_conversation_ids


def install_runtime_compatibility(runtime_cls: type[ConversationRuntime]) -> None:
    """Install explicit delegates without a dynamic attribute/service-locator seam."""

    runtime_cls.set_surface = set_surface

    runtime_cls.set_model_override = set_model_override

    runtime_cls.set_autonomous = set_autonomous

    runtime_cls.is_autonomous = is_autonomous

    runtime_cls.set_quiet = set_quiet

    runtime_cls.is_quiet = is_quiet

    runtime_cls.set_assist = set_assist

    runtime_cls.is_assist = is_assist

    runtime_cls.apply_settings_change = apply_settings_change

    runtime_cls.set_artifact_mode = set_artifact_mode

    runtime_cls.set_appkit_mode = set_appkit_mode

    runtime_cls.set_research_sources = set_research_sources

    runtime_cls.get_research_sources = get_research_sources

    runtime_cls.activate_contract_for_brief = activate_contract_for_brief

    runtime_cls.set_build_kind = set_build_kind

    runtime_cls.expected_delivery_mode = expected_delivery_mode

    runtime_cls.note_build_verify_result = note_build_verify_result

    runtime_cls.get_last_selected_model = get_last_selected_model

    runtime_cls.set_last_selected_model = set_last_selected_model

    runtime_cls.execute_pi_tool = execute_pi_tool

    runtime_cls.reconcile_orphaned_runs = reconcile_orphaned_runs

    runtime_cls.sandbox_state = sandbox_state

    runtime_cls.sandbox_instance_ids = sandbox_instance_ids

    runtime_cls.sweep_idle_once = sweep_idle_once

    runtime_cls.sweep_abandoned_gates_once = sweep_abandoned_gates_once

    _install_schedule_compatibility(runtime_cls)
