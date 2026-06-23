"""Tool anatomy — tool-sandbox-contract.md §2/§3.

Every tool is three separable things (BoD §10.1): a schema (what the model sees
+ how arguments are validated), a validator/repair wrapper (executor.py), and an
executor (`Tool.run`). This separation lets the same tool run in different
sandbox backends and keeps validation uniform.

[CONTRACT DEVIATION — flagged for a contract patch] §2 types `ToolDef.needs` as
`frozenset[Requirement]` and references NETWORK/FILESYSTEM, but the router
contract's `Requirement` enum holds only *model* capabilities (VISION/
LONG_CONTEXT/TOOL_CALLING/JSON_MODE). Tool `needs` are *execution-environment*
capabilities — a distinct concept — so we introduce a `Capability` enum here
rather than overload `Requirement`. The Tool/Sandbox contract §2 should be
patched to reference `Capability`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

from disco.core import SecurityRisk
from disco.core.llm import ToolSpec
from pydantic import BaseModel, ConfigDict, Field


class Capability(str, Enum):
    """Execution-environment capabilities a tool requires (distinct from the
    router's model `Requirement`). Drives scoping (§8) and the sandbox spec
    (§5/§7) — anything a tool doesn't declare, its instance is denied.

    Members match tool-sandbox-contract.md v1.1 §2."""

    NETWORK = "network"  # raw egress (browser to arbitrary sites, deploy preview)
    FILESYSTEM = "filesystem"  # workspace file access
    CODE_EXEC = "code_exec"  # runs arbitrary code
    SHELL = "shell"  # runs shell commands
    DISPLAY = "display"  # a GUI/display (browser noVNC view)


class ToolContext(BaseModel):
    """[CONTRACT] What a tool's run() receives: the sandbox handle, the
    workspace, scoped capabilities, and the per-call timeout — but NEVER raw
    secrets. A tool needing an external credential receives a scoped, short-lived
    CAPABILITY (a mediated handle), not the secret itself (§6)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    sandbox: Any | None  # SandboxInstance | None (None for in_process tools)
    sessions: Any | None = None  # ShellSessionManager | None (BP-01)
    kernel: Any | None = None  # KernelSession | None (BP-08)
    workspace_path: str
    timeout_s: int
    capabilities: Any  # CapabilitySet (scoped grants; secrets.py)
    owner_id: str
    conversation_id: str
    assist: bool = False
    # CW-6: the capability-derived file_read page budget (chars), threaded from the
    # runtime's derive_context_caps(assist, driver_context_window) so a file that
    # fits the assist-OFF snapshot pin can also be read in ONE shot. None ⇒ the tool
    # falls back to its static _READ_CHAR_BUDGET (behavior-preserving for executors
    # that don't thread it — assist-ON resolves to that same value).
    read_char_budget: int | None = None
    # ROOT-5: the conversation's EFFECTIVE driver endpoint (override-aware) for
    # LLM-using tools (e.g. slides_generate), as (base_url, model_id, api_key_env).
    # NEVER the resolved key — only the env-var NAME (§6, no raw secrets in ctx);
    # the tool resolves the secret itself. None ⇒ the tool falls back to the global
    # AGENT_DRIVER from ConfigStore (behavior-preserving for non-build executors).
    driver_llm: tuple[str, str, str | None] | None = None


class ToolOutcome(BaseModel):
    """What a tool's run() returns. The executor maps this into the event
    contract's ToolResult — separating them lets tools return rich results
    without knowing the event schema."""

    model_config = ConfigDict(frozen=True)
    success: bool
    content: str  # human/LLM-readable result text
    structured: dict[str, Any] | None = None
    error: str | None = None  # populated iff not success
    # Artifacts produced in the workspace (paths), surfaced to the UI's
    # Inspector/Assets view (BoD §13.4/§13.5).
    artifacts: list[str] = Field(default_factory=list)


class ToolDef(BaseModel):
    """[CONTRACT] The static definition of one tool. `to_spec()` produces the
    provider-neutral ToolSpec the model sees (router contract); `args_model`
    validates the model's arguments before execution — the SAME object, no
    drifting second copy (§3)."""

    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    args_model: type[BaseModel]  # validates ToolCall.arguments
    needs: frozenset[Capability] = frozenset()
    # Coarse static risk hint; the SecurityAnalyzer may refine per-call (§6.1).
    # None => the analyzer decides entirely.
    base_risk: SecurityRisk | None = None
    runs_in: Literal["sandbox", "in_process"] = "sandbox"
    # Planner-safety flag (Claude-Code plan-mode parity). True iff the tool only
    # OBSERVES — it makes no durable change to the workspace, sandbox, or the
    # outside world (reads/searches/the plan-submission signal). The loop shows
    # ONLY read-only tools to the PLANNING agent, so a plan is drafted from
    # context-gathering alone and writes/exec are off the table until approval.
    # FAIL-SAFE DEFAULT (False): an unmarked tool is treated as mutating and is
    # WITHHELD from the planner — a new write tool that forgets to set this can
    # never silently leak to the planner; the worst case is an over-restricted
    # planner, never an under-restricted one.
    read_only: bool = False
    # [EXTENSION resolving §6 OPEN] named capabilities this tool may invoke via
    # the orchestrator-mediated CapabilitySet (e.g. "search", "extract").
    uses_capabilities: frozenset[str] = frozenset()

    def to_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters_schema=self.args_model.model_json_schema(),
        )


@runtime_checkable
class Tool(Protocol):
    """[CONTRACT] A tool's executable side. `definition` is its static ToolDef;
    `run` performs the action given VALIDATED args and an execution Context.

    `args` is typed `Any` (not `BaseModel`) so concrete tools can narrow it to
    their specific `args_model` (e.g. `FileReadArgs`) without tripping Python's
    contravariant-parameter rule — the executor guarantees `args` is a validated
    instance of the tool's `args_model` before calling. ([INTERIOR] typing choice;
    the contract's illustrative `BaseModel` is the conceptual type.)"""

    definition: ToolDef

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutcome: ...


class ToolExecutionError(BaseModel):
    """A structured, model-facing error. Becomes the content of a failed
    ToolResult so the agent sees exactly what to fix next step (§3)."""

    model_config = ConfigDict(frozen=True)
    kind: Literal[
        "unknown_tool",
        "invalid_arguments",
        "execution_error",
        "denied",
        "timeout",
        "sandbox_error",
    ]
    message: str
    validation_errors: list[dict[str, Any]] | None = None
    expected_schema: dict[str, Any] | None = None
