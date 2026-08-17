"""In-memory fake tool world (guidelines §15.5, PR S6).

`FakeToolWorld` is a deterministic, in-memory workspace that records every tool
call the loop executes and lets a test materialize a `workspace-manifest.json`
(path -> content) the OutputTruthOracle consumes — all without a real sandbox.

`build_recording_executor` wraps the loop's own `FakeExecutor` (injected — no disco
import here) so the REAL loop drives it: it advertises a tool set, records each
executed `ToolCall` into the world (applying `file_write`-style mutations), and
returns a `ToolResult`. The toolset is what the loop's planning gate filters against,
so a write tool can be advertised yet (per the live product bug) still execute when a
scripted model calls it during PLANNING.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Tool names whose call mutates the workspace (used to apply effects + to mirror the
# planning-safe boundary the oracle enforces).
_WRITE_TOOLS = frozenset({"file_write", "write_file", "edit", "apply_patch"})


@dataclass
class FakeToolWorld:
    """A recorded in-memory workspace. `files` is the materialized workspace;
    `calls` is every executed (tool_name, arguments) in order."""

    files: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    # Per-tool forced failures: {tool_name: error_message} -> the executor returns a
    # failed ToolResult (success=False) so a verifier-failure path can be simulated.
    fail_tools: dict[str, str] = field(default_factory=dict)

    def record(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.calls.append({"tool_name": tool_name, "arguments": dict(arguments)})
        if tool_name in _WRITE_TOOLS:
            path = arguments.get("path") or arguments.get("file") or arguments.get("filename")
            if path is not None:
                self.files[str(path)] = str(arguments.get("content", ""))

    def workspace_manifest(self) -> dict[str, str]:
        """The OutputTruthOracle-shaped manifest (path -> content)."""
        return dict(self.files)


def build_recording_executor(
    *,
    fake_executor_cls: type,
    tool_result_cls: type,
    tool_spec_cls: type,
    tool_names: list[str],
    world: FakeToolWorld | None = None,
) -> Any:
    """Build a `FakeExecutor` subclass instance that advertises `tool_names`,
    records calls into `world`, and returns success (or a forced failure).

    Injected classes (no disco import):
      fake_executor_cls : `loop_fakes.FakeExecutor`
      tool_result_cls   : `disco.core.ToolResult`
      tool_spec_cls     : `disco.core.llm.ToolSpec`
    """
    the_world = world if world is not None else FakeToolWorld()
    specs = [
        tool_spec_cls(name=name, description=name, parameters_schema={}) for name in tool_names
    ]

    class _RecordingExecutor(fake_executor_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(tools=specs)
            self.world = the_world

        async def execute(self, call: Any) -> Any:
            self.calls.append(call)
            self.world.record(call.tool_name, dict(call.arguments))
            err = the_world.fail_tools.get(call.tool_name)
            if err is not None:
                return tool_result_cls(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    success=False,
                    content=err,
                    error=err,
                )
            return tool_result_cls(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content="ok",
            )

    return _RecordingExecutor()


def make_prose_step_builder(agent_step_cls: type) -> Callable[[str], Any]:
    """A `prose_step(text)` builder for FakeModelScript, from the injected
    `AgentStep` class."""

    def _prose(text: str) -> Any:
        return agent_step_cls(thought=text, tool_call=None, finished=False)

    return _prose
