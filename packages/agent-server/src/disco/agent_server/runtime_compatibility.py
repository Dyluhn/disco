"""State-free legacy ``ConversationRuntime`` delegates retained until PKG-13."""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    EventSource,
    ObservationEvent,
)

if TYPE_CHECKING:

    from disco.core import ToolCall, ToolResult

    from .runtime import ConversationRuntime


async def execute_disco_tool(self, conversation_id: str, tool_call: ToolCall) -> ToolResult:

    executor = self._run_resources.executor(conversation_id)
    if executor is None:
        self._loop_factory.loop_for(conversation_id)
        executor = self._run_resources.executor(conversation_id)
    if executor is None:
        raise RuntimeError("tool executor was not composed")
    action = ActionEvent(
        source=EventSource.AGENT, thought=f"[disco] {tool_call.tool_name}", tool_call=tool_call
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


def install_runtime_compatibility(runtime_cls: type[ConversationRuntime]) -> None:
    """Install explicit delegates without a dynamic attribute/service-locator seam."""

    runtime_cls.execute_disco_tool = execute_disco_tool
