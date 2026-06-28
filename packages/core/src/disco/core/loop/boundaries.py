"""The loop's collaborator boundaries — agent-loop-contract.md §3.

The loop is the orchestrator; it invokes four collaborators across clean
boundaries. Three (`SecurityAnalyzer`, `ConfirmationPolicy`, `ToolExecutor`) are
*defined elsewhere* — the Security design and the Tool/Sandbox contract — and
appear here only as the Protocols the loop binds to. `Agent` is the brain that
wraps the LLM router (concrete impl in agent.py). `StopHook` is the completion
gate (§7.4).

Field names/types/signatures are normative; the loop neither knows nor cares
about the interiors behind these seams.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..events import ActionEvent, Event, SecurityRisk, ToolCall, ToolResult
from ..llm import OperatingMode, OverflowSignal, StreamChunk, ToolSpec
from ..state import ConversationState
from ..view import View

# A watch-it-write hook: awaited with each streamed tool-call argument fragment
# (StreamChunk.tool_args_delta) so the loop can surface a file body as it
# assembles. Optional everywhere — None means "don't stream" (tests, CLI).
StreamHook = Callable[[StreamChunk], Awaitable[None]]


def is_finish_tool_name(name: str, finish_alias: str | None) -> bool:
    """P6 — THE single source of truth for "is this the finish signal?", shared by every
    surface that must treat the contract verification finalizer (ready_for_*_verification)
    identically to the `finish` virtual tool: the Agent's batched-call selection, the
    engine dispatch, the driver advertisement/requery, and planning suppression. Lives in
    boundaries (a low-level shared module) so engine.py AND agent.py can import it without
    a circular import. Plain "finish" is always the signal; the contract finalizer is too
    when a contract governs the run (finish_alias set)."""
    return name == "finish" or (finish_alias is not None and name == finish_alias)


class AgentStep(BaseModel):
    """The product of one `Agent.step()`. The loop converts this into events.

    [CONTRACT] Exactly ONE proposed action (or a finish/no-op). The loop enforces
    one-action-per-iteration; the Agent must not return multiple tool calls to be
    run without observation (the single `tool_call` field makes that structural).
    """

    model_config = ConfigDict(frozen=True)
    thought: str = ""
    tool_call: ToolCall | None = None  # None => no action this step
    self_assessed_risk: SecurityRisk = SecurityRisk.UNKNOWN
    finished: bool = False  # agent declares the goal complete
    # W-31 — the provider cut this assistant message off mid-sentence
    # (finish_reason=="length") with no tool call. The loop must NOT surface it
    # as a complete turn (it injects a "continue where you left off" reminder and
    # re-steps). Only meaningful on a tool-less prose step.
    truncated: bool = False
    llm_response_id: str | None = None  # carried into ActionEvent


@runtime_checkable
class Agent(Protocol):
    """[CONTRACT] The 'brain': given the current View (model-facing messages) and
    the available tools, produce the next step — text and/or one tool call, with
    a self-assessed risk. Wraps the LLMRouter; does NOT execute tools."""

    async def step(
        self,
        view: View,
        tools: list[ToolSpec],
        *,
        mode: OperatingMode,
        overflow_signal: OverflowSignal,
        on_stream: StreamHook | None = None,
        temperature: float | None = None,
        assist: bool = False,
        attempt: int = 1,
        provider_prefs: dict | None = None,
    ) -> AgentStep: ...


@runtime_checkable
class ToolExecutor(Protocol):
    """[CONTRACT BOUNDARY — defined in the Tool/Sandbox contract, next doc]
    Executes one ToolCall (often in the sandbox) and returns a ToolResult. The
    loop neither knows nor cares whether execution is local or sandboxed."""

    async def execute(self, call: ToolCall) -> ToolResult: ...

    def available_tools(self) -> list[ToolSpec]: ...  # what the model may call


@runtime_checkable
class Sandbox(Protocol):
    """[CONTRACT BOUNDARY — the Tool/Sandbox contract owns the full surface]
    The minimal file-IO slice of a sandbox instance the loop's projection /
    memory-mirror steps touch. The concrete `SandboxInstance` (packages/tools)
    structurally satisfies this; core stays dependency-light by binding only to
    this duck-typed view (it neither imports nor owns the sandbox)."""

    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[str]: ...

    async def file_exists(self, path: str) -> bool:
        """B4 — existence check resolved in the SANDBOX's own namespace. The
        container backend's files live INSIDE the box (its `workspace_path` is
        None), so a host-side `Path` check would falsely report a just-written
        file as missing. C18 asks the sandbox instead. Returns False for a
        missing file or a path that escapes the workspace jail; never raises on
        a plain absence."""
        ...

    @property
    def workspace_path(self) -> str | None:
        """The sandbox's workspace root as an absolute path on the HOST filesystem
        (process backend) or None when unavailable. Consumed by C18 `file_exists`
        resolution and the C1c DoD evaluator so predicates resolve against the
        real sandbox FS rather than the agent-server CWD."""
        ...


@runtime_checkable
class SecurityAnalyzer(Protocol):
    """[CONTRACT BOUNDARY — defined in the Security design, BoD §17] Scores a
    proposed action's risk BEFORE execution. May override the agent's
    self-assessment."""

    def assess(self, action: ActionEvent) -> SecurityRisk: ...


@runtime_checkable
class ConfirmationPolicy(Protocol):
    """[CONTRACT BOUNDARY — Security design] Decides whether a given risk requires
    human confirmation."""

    def should_confirm(self, risk: SecurityRisk) -> bool: ...


@runtime_checkable
class StopHook(Protocol):
    """[CONTRACT] Consulted when the agent declares finished. Returning False
    VETOES completion and the loop continues (with injected feedback)."""

    async def allow_stop(self, state: ConversationState, events: list[Event]) -> bool: ...
