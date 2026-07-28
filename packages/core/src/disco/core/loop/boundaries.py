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
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..events import ActionEvent, Event, SecurityRisk, ToolCall, ToolResult
from ..llm import OperatingMode, OverflowSignal, StreamChunk, ToolSpec
from ..state import ConversationState
from ..verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationExecutionIdentity,
    VerifierReferenceImage,
)
from ..verify_medium import VerifierMediumHint
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
    # REL-1c — true when this finish originated from the contract's
    # ready_for_*_verification finalizer alias rather than plain `finish`.
    requested_verification: bool = False
    llm_response_id: str | None = None  # carried into ActionEvent
    empty_reasoning_diagnostic: dict[str, Any] | None = None


class HostVerificationDeliverable(BaseModel):
    """Host-verifier input reconstructed by the finish path.

    Core owns only this neutral payload shape; the production verifier lives in
    agent-server and may use sandbox/browser/server helpers behind the protocol.
    """

    model_config = ConfigDict(frozen=True)

    conversation_id: str
    artifact_path: str
    artifact_kind: str = "files"
    deployment_url: str = ""
    requested_verification: bool = False
    # Durable authority and freshness binding supplied by the host finish path.
    # Historical/standalone callers may omit run identity, but product receipts
    # preserve it whenever the Build runtime emitted the typed admission events.
    run_intent_id: str | None = Field(default=None, min_length=1, max_length=160)
    run_identity: str | None = Field(
        default=None,
        pattern=r"^run:sha256:[0-9a-f]{64}$",
    )
    agent_view_id: str | None = Field(default=None, min_length=1, max_length=128)
    deliverable_event_id: str | None = Field(default=None, min_length=1, max_length=128)
    workspace_revision: int = Field(default=0, ge=0)
    workspace_generation: str = Field(default="", max_length=128)
    workspace_epoch: int | None = Field(default=None, ge=1)
    observed_after_seq: int = Field(default=0, ge=0)
    required_claims: tuple[HostVerificationClaim, ...] = ()
    verification_contract: AdmittedVerificationContract | None = None
    verification_check: VerificationCheckContract | None = None
    execution_identity: VerificationExecutionIdentity | None = None
    artifact_identity: VerificationArtifactIdentity | None = None
    verification_medium: str = Field(default="target", min_length=1, max_length=96)
    preview_selection: PreviewSelectionIdentity | None = None
    preview_binding_required: bool = False


class VerifierScreenshot(BaseModel):
    """Screenshot evidence allowed into the bounded verifier context."""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    image_data_url: str = ""


class VerifierContextSeed(BaseModel):
    """The complete verifier-model context.

    This is intentionally narrower than the builder's View: the verifier gets
    only the contract, deliverable paths, deterministic check results, and a
    screenshot reference/image. It never receives the builder transcript.
    """

    model_config = ConfigDict(frozen=True)

    contract: dict[str, Any] = Field(default_factory=dict)
    deliverable_paths: list[str] = Field(default_factory=list)
    check_results: dict[str, Any] = Field(default_factory=dict)
    screenshot: VerifierScreenshot = Field(default_factory=VerifierScreenshot)
    reference_images: tuple[VerifierReferenceImage, ...] = ()
    medium: VerifierMediumHint | None = None
    claims: tuple[HostVerificationClaim, ...] = ()


class TypedVerifierVerdict(BaseModel):
    """Typed verdict returned by the bounded verifier model."""

    model_config = ConfigDict(frozen=True)

    verified: bool
    verdict: Literal["pass", "fail", "degraded", "unavailable", "unverifiable"]
    detail: str = ""
    failures: list[dict[str, Any]] = Field(default_factory=list)
    next_action: str = ""
    failure_fingerprint: str = ""


class SealabilityProbeResult(BaseModel):
    """Result of the host's finish-time workspace sealability probe (F-27).

    Produced by running the SAME snapshot machinery the final workspace seal
    runs, against a throwaway destination. ``blocking`` carries the walker's
    exact per-entry skip strings ("<path>: <reason>") that the strict final
    seal would refuse — symlinks, hardlinked/non-regular entries, and
    oversized files: DETERMINISTIC content judgments only. Transient capture
    failures (read/lstat/list races) are never blocking — reporting them
    would launder infrastructure noise into a product refusal, the reverse of
    the laundering F-27 fixed. Deliberate non-deliverable exclusions
    (dependency caches, runtime secret paths) are never blocking either.
    ``sealable=True`` with an empty ``blocking`` also covers "nothing to
    probe" (no live workspace) — the commit-time seal remains the sole
    publication authority either way.
    """

    model_config = ConfigDict(frozen=True)

    sealable: bool
    blocking: tuple[str, ...] = ()
    detail: str = ""


# REL-27 — host-injected finish-time sealability probe. Injected by the runtime
# for build-like loops; None means no probe exists and every finish path is
# byte-identical to the pre-seam behavior. The probe must be side-effect-free
# on durable storage (throwaway destination) and bounded by the loop's timeout.
SealabilityProbe = Callable[[], Awaitable[SealabilityProbeResult]]


@runtime_checkable
class HostVerifier(Protocol):
    """Host-owned target verifier dispatcher seam.

    The admitted check's exact issuer/receipt kind/operation are carried on the
    deliverable. Implementations must return an unavailable result when no
    registered adapter owns that check; they may never silently substitute a
    browser or another target's verifier.
    """

    async def verify(self, deliverable: HostVerificationDeliverable) -> dict[str, Any]: ...


@runtime_checkable
class VerifierJudge(Protocol):
    """Model-judged verifier boundary.

    The loop calls this with a :class:`VerifierContextSeed` only. Implementations
    may use an LLM internally, but the transcript stays behind this protocol and
    the loop persists only the resulting typed verdict summary.
    """

    async def judge(self, seed: VerifierContextSeed) -> TypedVerifierVerdict: ...


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
    async def list_dir_bounded(
        self, path: str, limit: int
    ) -> tuple[list[tuple[str, str]], bool]: ...

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
