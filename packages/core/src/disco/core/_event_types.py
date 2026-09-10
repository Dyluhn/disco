"""Base event infrastructure and payload value objects — event-state-contract.md §2.

This module owns the envelope (``BaseEvent``), the discriminator enums
(``EventSource``, ``EventKind``, ``ConversationStatus``, ``SecurityRisk``), the
``LLMConvertible`` marker, and the shared payload value objects
(``LLMMessage``, ``ToolCall``, ``ToolResult``, ``PlanStep``, ``ReportSection``,
``AlternativeOption``, ``ClarifyQuestionItem``, ``QuestionsV2Item``,
``RuntimeConstraintDeclaration``, ``PlanVerificationTransition``,
``PlanVerifierFailure``, ``PlanVerifierPass``).

Concrete event model classes live in the sibling ``_event_interaction``,
``_event_control``, ``_event_outputs``, and ``_event_audit`` modules.  Render
helpers live in ``_event_render``.  The ``events`` facade assembles the
``Event`` union and the JSON adapter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .dod import DoDPredicate
from .effects import (
    ActionProfile,
    EffectReceipt,
    FinalWorkspaceSeal,
    RecoveryLeaseTransition,
)
from .verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    HostVerificationResult,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationExecutionIdentity,
    VerificationRequirementsDirective,
)

# Bump on a *breaking* change to any event shape. Adding an optional field with
# a default is backward-compatible and does NOT require a bump (§4 rule 2).
SCHEMA_VERSION = 1
APPKIT_EJECTION_SOURCE_TRIGGER = "appkit-ejection-source"
APPKIT_EJECTION_TARGET_TRIGGER = "appkit-ejection"
APPKIT_EJECTION_LOST_GUARANTEES = (
    "semantic-only AppKit mutation boundaries",
    "deterministic AppKit regeneration and writable-zone protection",
    "AppKit verifier and AppKit-verified revision status",
    "AppKit-governed deployment guarantees",
)


class EventSource(str, Enum):
    """Who/what produced an event. Security and UI both depend on this (§1.6)."""

    USER = "user"
    AGENT = "agent"
    ENVIRONMENT = "environment"  # tool results, injected feedback, hooks
    SYSTEM = "system"  # lifecycle/status, condensation, errors


class EventKind(str, Enum):
    """Discriminator for the Event union. Serialized by value (§4 rule 4)."""

    MESSAGE = "message"
    ACTION = "action"
    OBSERVATION = "observation"
    AGENT_ERROR = "agent_error"
    CONDENSATION = "condensation"
    STATUS = "status"
    WORKSPACE_VERSION = "workspace_version"
    WORKSPACE_RESTORED = "workspace_restored"
    WORKSPACE_MUTATION = "workspace_mutation"
    BUILD_PLATFORM_ADMISSION = "build_platform_admission"
    APPKIT_EJECTION = "appkit_ejection"
    ERROR = "error"  # conversation-level error (distinct from agent_error)
    PLAN = "plan"  # a proposed, structured plan awaiting approval (Build plan-mode)
    REPORT = "report"  # a finished Deep Research multi-section grounded report
    RESEARCH_CHECKPOINT = "research_checkpoint"  # user-stopped resumable research state
    ALTERNATIVES = "alternatives"  # 2–3 user-choosable options after repeated tool failure
    KNOWLEDGE = "knowledge"  # a scoped best-practice snippet (Cluster 7)
    RUNTIME_CONSTRAINT = "runtime_constraint"  # host-authored typed constraint + recovery
    DATASOURCE = "datasource"  # durable API/schema docs, condensation-immune (Cluster 7)
    DELIVERABLE = "deliverable"  # the agent's finished-artifact handoff signal
    VERIFIER_STARTED = "verifier_started"  # REL-1b: host verifier audit marker
    VERIFIER_VERDICT = "verifier_verdict"  # REL-1b: host verifier result marker
    VERIFIER_SHADOW = "verifier_shadow"  # REL-1b: inline-vs-host verifier comparison
    SCHEDULE = "schedule"  # a schedule was created or deleted (RP-08)
    SCHEDULE_RUN = "schedule_run"  # a scheduled run fired (RP-08)
    CLARIFY = "clarify"  # legacy pre-plan typed clarification questions (RP-13)
    QUESTIONS_V2 = "questions_v2"  # structured pre-plan intake (gap-close §K)
    CONTEXT_RESOLVED = "context_resolved"  # CXT-3: agent's DEFERRED snip mark (resolved range)
    CONTEXT_SUMMARY = "context_summary"  # CXT-3: durable summary written for a resolved range


def _new_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


class BaseEvent(BaseModel):
    """Common envelope for every event. Subtypes add a typed payload.

    [CONTRACT] These fields exist on every event and never change meaning.

    `frozen=True` makes events immutable at the type level (invariant #1):
    accidental mutation becomes a runtime error, not a silent bug. The only
    field "filled in later" is `seq`, set by the store via `model_copy` (a new
    instance), never by in-place mutation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=_new_id)
    # The `kind` discriminator is declared on each concrete subclass as a
    # `Literal[EventKind.X]` (the idiomatic Pydantic discriminated-union shape;
    # see `Event` below with `Field(discriminator="kind")`). Every concrete event
    # therefore carries `kind` — BaseEvent is the never-instantiated envelope.
    source: EventSource
    timestamp: datetime = Field(default_factory=_now)  # VOLATILE (§6.3)
    schema_version: int = Field(default=SCHEMA_VERSION)

    # Assigned by the EventStore at append time. None before persistence.
    # [CONTRACT] Monotonic, gap-free, per-conversation. Do NOT set manually.
    seq: int | None = Field(default=None)

    # Durable execution-generation attribution.  A fresh model-view admission
    # creates this id; every event caused by that view carries it.  Unlike
    # ``meta`` this is semantic: consumers use it to quarantine output from an
    # older worker that arrives after a newer user intent has been admitted.
    # None keeps historical event logs backward compatible and is valid for
    # user/host-owned events that do not originate in an agent run.
    agent_view_id: str | None = Field(default=None, min_length=1, max_length=128)

    # Free-form, non-semantic metadata (tracing ids, UI hints). VOLATILE.
    # Never used for reconstruction or equality.
    meta: dict[str, Any] = Field(default_factory=dict)


class LLMConvertible:
    """Marker mixin. Events that subclass this can be rendered into an LLM
    message via `to_llm_message()`. The View (§5) only ever materializes
    LLMConvertible events. [CONTRACT]

    Deliberately a plain class with no fields so it composes with the frozen
    Pydantic BaseEvent without disturbing the model.
    """

    def to_llm_message(self) -> LLMMessage:
        raise NotImplementedError


# ---- payload value objects --------------------------------------------------


class LLMMessage(BaseModel):
    """Provider-neutral message shape. The LLM router (separate subsystem) maps
    this to/from provider formats. [CONTRACT at the router boundary.]"""

    model_config = ConfigDict(frozen=True)
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Optional structured content (tool calls/results) carried opaquely; the
    # router owns provider-specific shaping.
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None  # VOLATILE
    images: list[str] | None = None  # data-URL base64 (data:image/png;base64,...)


class ToolCall(BaseModel):
    """A request to execute one tool. Produced by the agent, consumed by the
    tool subsystem (separate contract)."""

    model_config = ConfigDict(frozen=True)
    tool_name: str
    arguments: dict[str, Any]
    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex}")  # VOLATILE


class ToolResult(BaseModel):
    """The outcome of executing a ToolCall. Produced by the tool subsystem."""

    model_config = ConfigDict(frozen=True)
    call_id: str  # VOLATILE (correlates to ToolCall)
    tool_name: str
    success: bool
    content: str  # human/LLM-readable result text
    structured: dict[str, Any] | None = None  # optional machine payload
    error: str | None = None  # populated iff success is False
    # K1 reliability-kernel shadow schema. Host-produced effect evidence is
    # deliberately separate from model/domain-controlled ``structured`` data.
    # The empty default keeps every historical event log backward compatible.
    effect_receipts: tuple[EffectReceipt, ...] = ()
    # Host-authored runtime constraints this call proved. Same host-only lane as
    # effect_receipts: never populated from model/domain-controlled data, so a
    # model-authored lookalike carries no authority.
    runtime_constraints: tuple[RuntimeConstraintDeclaration, ...] = ()
    # Host-classified behavior of this validated invocation. Persisting the
    # profile on the result makes progress/recovery reconstruction deterministic
    # across restart and replay. Calls refused before execution leave it absent.
    action_profile: ActionProfile | None = None


class PlanStep(BaseModel):
    """One capstone in a proposed plan. The agent emits a list of these via the
    `submit_plan` tool; the UI tracks each one's progress during the build."""

    model_config = ConfigDict(frozen=True)
    title: str  # short, plain-language capstone ("Scaffold the page + styles")
    detail: str | None = None  # optional elaboration
    # C18 / C1c — the step's optional machine-checkable done_condition, PERSISTED on the
    # event so it survives resume (the in-memory `_plan_step_predicates` map is lost on a
    # restart between submit_plan and approval; reading the predicate back off the durable
    # PlanEvent is what lets the C1c DoD gate re-arm after a crash).
    done_condition: DoDPredicate | None = None


class ReportSection(BaseModel):
    """One section of a Deep Research report — a section of the long-form output
    grounded in a specific subset of the per-run corpus. `cited_passage_ids` are
    the stable passage ids the citation UI resolves to source cards (mirror of
    the per-block citations in the existing GroundedAnswer shape). `confidence`
    + `disputed_notes` carry the honesty-at-scale signal: when sources agree
    cleanly the section is "high"; when they disagree the conflict is named."""

    model_config = ConfigDict(frozen=True)
    id: str  # stable identifier ("s0", "s1", ... — for ToC anchoring)
    title: str  # section heading the report renders verbatim
    markdown: str  # the section body — markdown with inline [[passage_id]] citations
    cited_passage_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "mixed", "low"] = "high"
    # Plain-language notes naming any conflicts between sources for this section.
    # When sources contradict on a claim, the model writes a short note here so
    # the reader sees the disagreement, not a falsely-confident synthesis.
    disputed_notes: list[str] = Field(default_factory=list)
    unsupported_count: int = 0  # claims that failed NLI verification for this section


class SecurityRisk(str, Enum):
    """Mirrors §17 / the security contract. UNKNOWN is non-comparable."""

    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConversationStatus(str, Enum):
    """The loop's explicit execution state machine (BoD §12.1)."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STUCK = "STUCK"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    # The agent proposed a plan and the loop is halted until the human approves
    # it (Build plan-mode). Mirrors WAITING_FOR_CONFIRMATION but gates the whole
    # plan up front, not one risky action.
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    # The run is halted for the user to decide. Reached two ways: (1) the agent
    # itself calls `ask_user` with options; (2) the harness circuit breaker fires
    # after `_circuit_breaker_threshold` consecutive failures and hands off rather
    # than grinding. The user clicks an option (when present) or steers via
    # send_message. (There is no `propose_alternatives` tool — the model uses
    # `ask_user`; the breaker is harness-driven.)
    AWAITING_USER_DECISION = "AWAITING_USER_DECISION"
    # The agent called `ask_user` with a FREE-FORM question (no options) and the
    # loop is halted until the user TYPES an answer. The two-way Ask-gate: unlike
    # AWAITING_USER_DECISION (pick a card), this is an open question the user
    # answers in prose. The user's reply (send_message / steer) IS the resume
    # signal — same re-kick semantics as AWAITING_USER_DECISION, just a different
    # surface (AskPanel, not the alternatives cards).
    AWAITING_USER_QUESTION = "AWAITING_USER_QUESTION"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class AlternativeOption(BaseModel):
    """One concrete next-step option the agent proposed after exhausting retries.
    Each option carries a human-readable description + a structured ToolCall the
    loop will execute as the next action if the user picks it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str  # stable id for the user's pick_alternative frame
    title: str  # short label rendered on the option card (e.g. "Try with sudo")
    description: str  # one or two sentences: why this might work / what changes
    tool_name: str  # the tool the loop will call if this option is picked
    arguments: dict[str, Any] = Field(default_factory=dict)


class ClarifyQuestionItem(BaseModel):
    """One typed question in a ClarifyEvent. Each item has a stable id, the
    question text, a type that drives the UI input, and (after the user answers)
    the user's answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str  # stable id for this question within the clarify card
    question: str  # the question text (rendered as Markdown)
    type: Literal["short_text", "long_text", "choice"] = "short_text"
    # For choice-type questions: the allowed options.
    options: list[str] = Field(default_factory=list)
    # Populated by the user's response; empty until answered.
    answer: str = ""


class QuestionsV2Item(BaseModel):
    """One question in a QuestionsV2Event.

    Unlike the legacy clarify item, each v2 item always supports both a bounded
    option pick and free text. The UI accepts either; selecting "Other" can be
    refined in the free-text box.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    allow_free_text: bool = True
    answer: str = ""


class RuntimeConstraintDeclaration(BaseModel):
    """A host-authored constraint a tool declares, before it becomes an event.

    Declared at the point the host actually enforced something, so the loop never
    re-derives a constraint by reading refusal prose. This travels the same
    host-only lane as ``effect_receipts``: model- and domain-controlled payloads
    stay in ``structured`` and are never promoted into it, which is what makes a
    model-authored lookalike powerless.

    ``transient=True`` marks a failure that may succeed on retry. Those are
    deliberately representable and deliberately never pinned -- a blip must not
    become a permanent belief.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    constraint_key: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="", max_length=160)
    guidance: str = Field(min_length=1, max_length=1200)
    alternative: str = Field(default="", max_length=600)
    capability_generation: str = Field(default="", max_length=160)
    transient: bool = False


class PlanVerificationTransition(BaseModel):
    """Append-only authority record carried by one plan_approved status event.

    The StatusEvent envelope supplies timestamp and sequence. Fingerprints bind
    the old and new revision-scoped model predicates without copying them into
    the immutable external DoD row; the referenced PlanEvents retain the full
    predicate bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    old_plan_revision: int | None = None
    old_plan_event_id: str | None = None
    old_predicate_fingerprints: list[str] = Field(default_factory=list)
    new_plan_revision: int
    new_plan_event_id: str
    new_predicate_fingerprints: list[str] = Field(default_factory=list)
    old_authority: Literal["plan"] = "plan"
    new_authority: Literal["plan"] = "plan"
    external_authority: Literal["external"] = "external"
    external_predicate_fingerprints: list[str] = Field(default_factory=list)
    reason: str = "approved_plan_revision"


class PlanVerifierFailure(BaseModel):
    """Typed failure of the currently approved plan-owned verifier set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_revision: int
    plan_event_id: str
    # The full approved set remains the authority/retry identity.  The failed
    # subset is separate so render-time retirement can retire only a directive
    # whose actual unmet predicates were causally replaced.  Empty preserves
    # backward compatibility with pre-field durable events and fails closed.
    predicate_fingerprints: list[str]
    failed_predicate_fingerprints: list[str] = Field(default_factory=list)
    spec_fingerprint: str
    failure_fingerprint: str
    failure_kinds: list[str]
    attempt_for_approved_plan: int = Field(ge=1)
    approvals_with_same_predicates: int = Field(ge=1)
    authority: Literal["plan"] = "plan"
    replan_allowed: bool = True


class PlanVerifierPass(BaseModel):
    """Typed successful evaluation of the current approved plan-owned verifier set.

    A verifier-only repair may correctly leave already-valid product bytes untouched.
    This receipt is the durable host-truth edge proving the replacement predicates were
    actually evaluated; it is not model context and carries no authority over external
    acceptance requirements.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_revision: int = Field(ge=1)
    plan_event_id: str = Field(min_length=1)
    predicate_fingerprints: list[str] = Field(min_length=1)
    spec_fingerprint: str = Field(min_length=1)
    authority: Literal["plan"] = "plan"


__all__ = [
    "APPKIT_EJECTION_LOST_GUARANTEES",
    "APPKIT_EJECTION_SOURCE_TRIGGER",
    "APPKIT_EJECTION_TARGET_TRIGGER",
    "SCHEMA_VERSION",
    "ActionProfile",
    "AdmittedVerificationContract",
    "AlternativeOption",
    "BaseEvent",
    "ClarifyQuestionItem",
    "ConversationStatus",
    "DoDPredicate",
    "EffectReceipt",
    "EventKind",
    "EventSource",
    "FinalWorkspaceSeal",
    "HostVerificationClaim",
    "HostVerificationResult",
    "LLMConvertible",
    "LLMMessage",
    "PlanStep",
    "PlanVerificationTransition",
    "PlanVerifierFailure",
    "PlanVerifierPass",
    "PreviewSelectionIdentity",
    "QuestionsV2Item",
    "RecoveryLeaseTransition",
    "ReportSection",
    "RuntimeConstraintDeclaration",
    "SecurityRisk",
    "ToolCall",
    "ToolResult",
    "VerificationArtifactIdentity",
    "VerificationClaimStatus",
    "VerificationDeliveryContract",
    "VerificationExecutionIdentity",
    "VerificationRequirementsDirective",
    "_new_id",
    "_now",
]
