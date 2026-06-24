"""Shared fakes for the Build Soak §20 contract tests.

Drives the REAL AgentLoop with a scripted model + an in-memory tool world (no live
model, no sandbox) so the loop emits a real event log the contracts assert against.
Reuses `loop_fakes` (ScriptedAgent / FakeExecutor / build_loop)."""

from __future__ import annotations

from disco.core import ToolResult
from disco.core.llm import OperatingMode, ToolSpec
from loop_fakes import FakeExecutor, ScriptedAgent, build_loop

# The production Build planning allowlist (runtime.py:1466) — read/explore + plan.
PROD_PLANNING_TOOLS = frozenset({"submit_plan", "file_list", "file_read", "search", "extract"})

# A realistic Build toolset the executor advertises (read tools + mutating tools).
DEFAULT_TOOLS = [
    "submit_plan",
    "file_read",
    "file_list",
    "search",
    "extract",
    "file_write",
    "shell",
    "serve",
]

_WRITE_TOOLS = {"file_write", "write_file", "edit", "apply_patch"}


class BuildExecutor(FakeExecutor):
    """Advertises a Build toolset and records an in-memory workspace. Every call
    succeeds (so a wrong-tool-in-planning call EXECUTES unless the product gate
    rejects it first — which is exactly the contract under test)."""

    def __init__(self, tool_names: list[str] | None = None) -> None:
        names = tool_names or DEFAULT_TOOLS
        super().__init__(
            tools=[ToolSpec(name=n, description=n, parameters_schema={}) for n in names]
        )
        self.world: dict[str, str] = {}

    async def execute(self, call) -> ToolResult:
        self.calls.append(call)
        if call.tool_name in _WRITE_TOOLS:
            path = call.arguments.get("path")
            if path is not None:
                self.world[str(path)] = str(call.arguments.get("content", ""))
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


def build_plan_loop(agent, *, conversation_id: str, executor: BuildExecutor | None = None):
    """A loop wired exactly like the production Build surface: starts in PLANNING
    with the production planning allowlist, executes in LONG_HORIZON."""
    return build_loop(
        agent,
        conversation_id=conversation_id,
        executor=executor or BuildExecutor(),
        mode=OperatingMode.PLANNING,
        planning_tools=PROD_PLANNING_TOOLS,
        execution_mode=OperatingMode.LONG_HORIZON,
    )


__all__ = [
    "PROD_PLANNING_TOOLS",
    "BuildExecutor",
    "ScriptedAgent",
    "build_plan_loop",
]
