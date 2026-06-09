"""The Event type hierarchy — event-state-contract.md §2.

Events are the atomic unit of all conversation and agent state. The log of
events is append-only and the single source of truth (BoD Principle 3); State
(state.py) and the LLM View (view.py) are *pure functions* of the ordered log.

Field names, types, and method signatures here are **normative** (the contract
marks them [CONTRACT]); other subsystems deserialize and compare these shapes
without further coordination. Method *bodies* are implementation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# Bump on a *breaking* change to any event shape. Adding an optional field with
# a default is backward-compatible and does NOT require a bump (§4 rule 2).
SCHEMA_VERSION = 1


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
    ERROR = "error"  # conversation-level error (distinct from agent_error)
    PLAN = "plan"  # a proposed, structured plan awaiting approval (Build plan-mode)
    REPORT = "report"  # a finished Deep Research multi-section grounded report
    ALTERNATIVES = "alternatives"  # 2–3 user-choosable options after repeated tool failure
    KNOWLEDGE = "knowledge"  # a scoped best-practice snippet (Cluster 7)
    DATASOURCE = "datasource"  # durable API/schema docs, condensation-immune (Cluster 7)
    DELIVERABLE = "deliverable"  # the agent's finished-artifact handoff signal


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
    kind: EventKind  # discriminator
    source: EventSource
    timestamp: datetime = Field(default_factory=_now)  # VOLATILE (§6.3)
    schema_version: int = Field(default=SCHEMA_VERSION)

    # Assigned by the EventStore at append time. None before persistence.
    # [CONTRACT] Monotonic, gap-free, per-conversation. Do NOT set manually.
    seq: int | None = Field(default=None)

    # Free-form, non-semantic metadata (tracing ids, UI hints). VOLATILE.
    # Never load-bearing for reconstruction or equality.
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


class PlanStep(BaseModel):
    """One capstone in a proposed plan. The agent emits a list of these via the
    `submit_plan` tool; the UI tracks each one's progress during the build."""

    model_config = ConfigDict(frozen=True)
    title: str  # short, plain-language capstone ("Scaffold the page + styles")
    detail: str | None = None  # optional elaboration


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


# ---- concrete event types ---------------------------------------------------


class MessageEvent(BaseEvent, LLMConvertible):
    """A message from user, agent, or environment."""

    kind: Literal[EventKind.MESSAGE] = EventKind.MESSAGE
    message: LLMMessage

    def to_llm_message(self) -> LLMMessage:
        return self.message


class ActionEvent(BaseEvent, LLMConvertible):
    """The agent chose to take one tool action. One action per event
    (Principle 6). Carries the agent's reasoning and self-assessed risk."""

    kind: Literal[EventKind.ACTION] = EventKind.ACTION
    source: EventSource = EventSource.AGENT
    thought: str  # the agent's reasoning for this action
    tool_call: ToolCall
    # Agent's self-assessed risk; the independent analyzer may override
    # downstream (security contract). Part of the event for audit.
    self_assessed_risk: SecurityRisk = SecurityRisk.UNKNOWN
    # VOLATILE: correlates to the model completion that produced this action.
    llm_response_id: str | None = None

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="assistant",
            content=self.thought,
            tool_calls=[
                {
                    "id": self.tool_call.call_id,
                    "name": self.tool_call.tool_name,
                    "arguments": _snip_args(self.tool_call.arguments),
                }
            ],
        )


# A-S2 (the Snip shaper): the per-observation char cap applied at RENDER time
# (to_llm_message), NOT at storage. The full content stays in the event payload
# (and on disk for spilled artifacts) — only the LLM-facing message is trimmed.
# Reversible: the model re-runs the tool or file_reads the artifact for the rest.
_OBS_SNIP_CHARS = 8_000
_OBS_SNIP_HEAD = 5_000
_OBS_SNIP_TAIL = 2_000


def snip_content(content: str, *, max_chars: int, head: int, tail: int) -> str:
    """Trim an over-long string to head + tail with a recoverable marker. Pure +
    deterministic (same input → same output) so it preserves View.of purity."""
    if len(content) <= max_chars:
        return content
    dropped = len(content) - head - tail
    return (
        f"{content[:head]}\n"
        f"… [snipped {dropped:,} chars — re-run the tool or use file_read for the full output] …\n"
        f"{content[-tail:]}"
    )


# H2: elide large ARGUMENT values (e.g. a file_write's full body) at RENDER time.
# A written file's content doesn't belong in the action history every turn — it's on
# disk + readable. Re-sending a 59 KB body each action is the dominant cost bloat.
# Pure + deterministic (preserves View.of purity); reversible (the marker tells the
# model the file exists + to file_read it).
_ARG_SNIP_CHARS = 1_500


def _snip_args(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > _ARG_SNIP_CHARS:
            out[k] = f"<{len(v):,} chars elided — already applied; use file_read for the content>"
        else:
            out[k] = v
    return out


class ObservationEvent(BaseEvent, LLMConvertible):
    """The result of an ActionEvent's tool call (success path)."""

    kind: Literal[EventKind.OBSERVATION] = EventKind.OBSERVATION
    source: EventSource = EventSource.ENVIRONMENT
    tool_result: ToolResult
    # Correlates this observation to its action. NOT volatile for
    # reconstruction (needed to pair action/observation) but IS ignored by
    # stuck-equality (§6.3) since the action content is what matters.
    action_id: str

    def to_llm_message(self) -> LLMMessage:
        # A-S2 Snip: cap a single large observation before it hits the context.
        return LLMMessage(
            role="tool",
            content=snip_content(
                self.tool_result.content,
                max_chars=_OBS_SNIP_CHARS,
                head=_OBS_SNIP_HEAD,
                tail=_OBS_SNIP_TAIL,
            ),
            tool_call_id=self.tool_result.call_id,
        )


class AgentErrorEvent(BaseEvent, LLMConvertible):
    """An error observation — tool failed, action invalid, execution raised, or
    the human declined the proposed action. Distinct from ErrorEvent (which is
    conversation-fatal).

    `tool_call_id` carries the proposed action's call_id when the event is paired
    with an ActionEvent; this lets the provider adapter (OpenAI etc.) properly
    pair the assistant's tool_calls with their resulting messages. Required for
    refused / rejected actions where no real tool result exists."""

    kind: Literal[EventKind.AGENT_ERROR] = EventKind.AGENT_ERROR
    source: EventSource = EventSource.ENVIRONMENT
    error: str
    action_id: str | None = None  # the action that failed, if any
    tool_call_id: str | None = None  # for pairing with the assistant tool_call

    def to_llm_message(self) -> LLMMessage:
        # Pre-formatted content (e.g. wrapped in <system-reminder>...</…>) is
        # rendered as-is; raw error strings get the "ERROR:" prefix for the
        # model's parse. This lets the rejection path inject ambient reminders
        # without the user-tone framing of a tool-failure message.
        content = self.error if self.error.startswith("<") else f"ERROR: {self.error}"
        return LLMMessage(role="tool", content=content, tool_call_id=self.tool_call_id)


class CondensationEvent(BaseEvent):
    """A TOMBSTONE. Marks a span of prior events as forgotten and records the
    summary that replaces them. NOT LLMConvertible — the View applies it (§5).
    [CONTRACT: this is how forgetting is represented.]"""

    kind: Literal[EventKind.CONDENSATION] = EventKind.CONDENSATION
    source: EventSource = EventSource.SYSTEM
    # The seq range [start, end] (inclusive) this condensation forgets. The View
    # drops events whose seq falls in any active range.
    forgotten_start_seq: int
    forgotten_end_seq: int
    # The summary inserted in place of the span (inline, for atomicity).
    summary: str
    summary_role: Literal["system", "user"] = "user"
    reason: Literal["request", "tokens", "events", "hard_reset"] = "tokens"


class StatusEvent(BaseEvent):
    """A lifecycle/status transition. NOT LLMConvertible. Drives the UI and the
    loop's state machine reconstruction (§3)."""

    kind: Literal[EventKind.STATUS] = EventKind.STATUS
    source: EventSource = EventSource.SYSTEM
    status: ConversationStatus
    detail: str | None = None


class PlanEvent(BaseEvent, LLMConvertible):
    """A structured plan the agent proposed (in PLANNING mode) and the human is
    asked to approve before any work runs. LLMConvertible so the committed plan
    stays in the agent's context during execution — it renders as an assistant
    message restating the steps it agreed to carry out.

    `context` is an optional markdown body carrying the planner's findings from
    exploring the workspace + web (Claude-Code-style: explain WHY this plan, what
    you learned, the trade-offs). Empty by default for backward compatibility."""

    kind: Literal[EventKind.PLAN] = EventKind.PLAN
    source: EventSource = EventSource.AGENT
    summary: str  # one or two sentences: what this plan delivers
    steps: list[PlanStep]
    revision: int = 1  # bumps each time the user sends the plan back for changes
    context: str = ""  # optional markdown rationale + exploration findings

    def to_llm_message(self) -> LLMMessage:
        lines = [f"{i}. {s.title}" for i, s in enumerate(self.steps, start=1)]
        body = "\n".join(lines)
        # Include the context so the executing agent has its own findings in-View
        # — same role Claude Code's plan-file markdown plays during execution.
        ctx = f"\n\nContext:\n{self.context}" if self.context else ""
        return LLMMessage(
            role="assistant",
            content=f"Plan (revision {self.revision}): {self.summary}\n{body}{ctx}",
        )


class ReportEvent(BaseEvent, LLMConvertible):
    """A finished Deep Research report — the multi-section grounded synthesis
    produced from the per-run corpus. LLMConvertible so a follow-up turn in the
    same conversation has the committed report headers + summary in its View
    (the long body would blow the context window; we render headers only).

    `bounded_by` names the hard-cap that terminated the run, if any: "sources"
    (max_sources hit), "rounds" (max_rounds_per_subq hit on all sub-questions),
    "wall_clock" (max_wall_clock_s hit), or "subquestions" (decompose produced
    more than max_subquestions). None means the run completed naturally."""

    kind: Literal[EventKind.REPORT] = EventKind.REPORT
    source: EventSource = EventSource.AGENT
    query: str  # the original user question this report answers
    summary: str  # executive summary (one or two paragraphs, top-of-report)
    sections: list[ReportSection]
    # The cited subset (only passages actually referenced by some section). The UI
    # resolves [[passage_id]] markers against this list to render source cards.
    # Plain dict[str, Any] to keep `core` free of any `retrieval` import — the
    # producer (deep_research engine) populates with Passage.model_dump().
    passages: list[dict[str, Any]] = Field(default_factory=list)
    # The full discovery set (URL, title, snippet, status). All_hits for the
    # All-Searched / Cited tabs at report scale. Same plain-dict reason.
    all_hits: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_count: int = 0  # whole-report total (sum across sections)
    bounded_by: str | None = None  # named cap if hit, else None (natural finish)
    # The depth tier the run used ("quick" / "standard_deep" / "exhaustive"),
    # surfaced for the UI's cost/time honesty + audit. Optional for backward-compat.
    depth_tier: str | None = None

    def to_llm_message(self) -> LLMMessage:
        # Render headers + summary only — the full body is too large for the View
        # and the citations would resolve to ids the model can't look up anyway.
        # This keeps the committed report present in-context for a follow-up turn
        # ("expand section 3") without bloating the window.
        headers = "\n".join(f"## {s.title}" for s in self.sections)
        bound = f"\n\n(Bounded by: {self.bounded_by}.)" if self.bounded_by else ""
        return LLMMessage(
            role="assistant",
            content=f"Research report for: {self.query}\n\n{self.summary}\n\n{headers}{bound}",
        )


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


class AlternativesEvent(BaseEvent, LLMConvertible):
    """The structured recovery hand-off. Emitted when the agent voluntarily calls
    `ask_user` WITH options — it enumerates 2–3 concrete next-step alternatives,
    those become this event, the loop halts at AWAITING_USER_DECISION, and the
    user picks one (or steers explicitly). The picked option's ToolCall becomes
    the next action. (The harness circuit breaker reaches the same status but with
    a free-form question, no clickable options.)

    Mirrors PlanEvent's shape (gate event + LLMConvertible so the proposed
    options stay in-context for any follow-up turn). `failed_action_id`
    correlates this gate with the action that triggered the recovery."""

    kind: Literal[EventKind.ALTERNATIVES] = EventKind.ALTERNATIVES
    source: EventSource = EventSource.AGENT
    failed_action_id: str  # the original ActionEvent.id that exhausted retries
    summary: str  # what we're choosing between, one sentence
    options: list[AlternativeOption]

    def to_llm_message(self) -> LLMMessage:
        lines = [
            f"{i}. {o.title} — {o.description}"
            for i, o in enumerate(self.options, start=1)
        ]
        body = "\n".join(lines)
        return LLMMessage(
            role="assistant",
            content=(
                f"After repeated failures on the prior step, I proposed these "
                f"alternatives: {self.summary}\n{body}"
            ),
        )


class KnowledgeEvent(BaseEvent, LLMConvertible):
    """A scoped best-practice snippet injected into the agent's context (Cluster
    7). `scope` is an optional applicability hint (a task keyword or path glob)
    the View can use to inject only relevant knowledge; `snippet` is the
    guidance. LLMConvertible so it renders into the model context, and pinned
    against condensation so standing guidance survives a long run."""

    kind: Literal[EventKind.KNOWLEDGE] = EventKind.KNOWLEDGE
    source: EventSource = EventSource.SYSTEM
    scope: str = ""
    snippet: str

    def to_llm_message(self) -> LLMMessage:
        scope = f" (applies to: {self.scope})" if self.scope else ""
        return LLMMessage(
            role="user",
            content=f"<knowledge{scope}>\n{self.snippet}\n</knowledge>",
        )


class DatasourceEvent(BaseEvent, LLMConvertible):
    """Durable data-API / schema documentation the agent learned or was given
    (Cluster 7). The long-horizon hazard this fixes: an API contract learned
    mid-run lives only in an Observation's inline text, which condensation later
    compresses into lossy prose → the agent hallucinates an endpoint. A
    DatasourceEvent is condensation-IMMUNE, so the exact contract stays verbatim
    across an arbitrarily long build."""

    kind: Literal[EventKind.DATASOURCE] = EventKind.DATASOURCE
    source: EventSource = EventSource.SYSTEM
    name: str  # the data source / API name
    docs: str  # endpoint shape, auth, params, example response — verbatim

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=f"<datasource name=\"{self.name}\">\n{self.docs}\n</datasource>",
        )


class DeliverableEvent(BaseEvent, LLMConvertible):
    """The agent's explicit 'here is the finished thing' handoff (Build). It names
    WHAT was produced and WHERE, so the UI can present a real handoff (open the
    live app / download the files) instead of leaving the user to guess what the
    run made. Distinct from the live preview (which shows work-in-progress): a
    DeliverableEvent is the agent affirming a result is ready.

    `kind`:
      • "app"   — a runnable result the user opens in the live preview (a built
                  site / running dev server rooted at `path`).
      • "files" — one or more workspace artifacts to download/inspect (`path` is
                  the file or directory).

    LLMConvertible so the agent's own context reflects what it has already handed
    off (it shouldn't re-deliver the same thing); the body stays terse."""

    kind: Literal[EventKind.DELIVERABLE] = EventKind.DELIVERABLE
    source: EventSource = EventSource.AGENT
    title: str  # short human label, e.g. "Landing page" / "Sales report"
    path: str  # workspace-relative path (dir for an app root, file/dir for files)
    artifact_kind: Literal["app", "files"] = "app"

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=(
                f"<deliverable kind=\"{self.artifact_kind}\" path=\"{self.path}\">\n"
                f"{self.title}\n</deliverable>"
            ),
        )


class ErrorEvent(BaseEvent):
    """A conversation-level (fatal-ish) error, e.g. MaxIterationsReached.
    NOT LLMConvertible."""

    kind: Literal[EventKind.ERROR] = EventKind.ERROR
    source: EventSource = EventSource.SYSTEM
    code: str
    detail: str


# ---- the discriminated union the store/serde use ----------------------------

Event = Annotated[
    MessageEvent
    | ActionEvent
    | ObservationEvent
    | AgentErrorEvent
    | CondensationEvent
    | StatusEvent
    | PlanEvent
    | ReportEvent
    | AlternativesEvent
    | KnowledgeEvent
    | DatasourceEvent
    | DeliverableEvent
    | ErrorEvent,
    Field(discriminator="kind"),
]

# Single shared validator/serializer for the union. Consumers parse arbitrary
# event dicts (post-migration) through this; the `kind` field selects the
# concrete type. (§4 serialization contract.)
EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)


def event_to_json_dict(event: Event) -> dict[str, Any]:
    """Serialize one event to a JSON-safe dict (§4 rule 1): ISO datetimes,
    enums by value. The inverse is `event_from_json_dict`."""
    return event.model_dump(mode="json")


def event_from_json_dict(raw: dict[str, Any]) -> Event:
    """Deserialize a (already-migrated) JSON dict back into the concrete event
    type via the discriminated union. Callers should run `migrate_event` first
    (see migration.py) so old persisted events stay readable forever."""
    return EventAdapter.validate_python(raw)
