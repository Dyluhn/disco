"""The Event type hierarchy — event-state-contract.md §2.

Events are the atomic unit of all conversation and agent state. The log of
events is append-only and the single source of truth (BoD Principle 3); State
(state.py) and the LLM View (view.py) are *pure functions* of the ordered log.

Field names, types, and method signatures here are **normative** (the contract
marks them [CONTRACT]); other subsystems deserialize and compare these shapes
without further coordination. Method *bodies* are implementation.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .dod import DoDPredicate, predicate_to_dict
from .effects import (
    ActionProfile,
    EffectReceipt,
    FinalWorkspaceSeal,
    RecoveryLeaseTransition,
)
from .verification import (
    HostVerificationClaim,
    HostVerificationResult,
    VerificationClaimStatus,
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
    ALTERNATIVES = "alternatives"  # 2–3 user-choosable options after repeated tool failure
    KNOWLEDGE = "knowledge"  # a scoped best-practice snippet (Cluster 7)
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


# ---- concrete event types ---------------------------------------------------


class MessageEvent(BaseEvent, LLMConvertible):
    """A message from user, agent, or environment."""

    kind: Literal[EventKind.MESSAGE] = EventKind.MESSAGE
    message: LLMMessage
    # Optional complete user/scenario proof-requirement snapshot.  This can make
    # completion stricter, but can never certify a result; only host-owned
    # VerifierVerdictEvents carry receipts.  Reference pixels are kept behind the
    # verifier boundary rather than injected into the driving model's view.
    verification_requirements: VerificationRequirementsDirective | None = None

    @model_validator(mode="after")
    def _requirements_are_user_authority(self) -> MessageEvent:
        if self.verification_requirements is not None and self.source is not EventSource.USER:
            raise ValueError("verification requirements may be attached only to user events")
        return self

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

# CW-6 — per-build override for the observation snip cap. to_llm_message() is a pure
# projection method with no tier/window access, but the snip MUST be raised for the
# assist-OFF (capable) tier in tandem with the read budget — otherwise a large
# file_read observation is snipped to a corrupted head/tail and the model re-reads
# forever. ViewBuilder.build sets this from the derived ContextCaps for the duration
# of a build; default None → the byte-identical assist-ON 8k snip. Task-local
# (ContextVar) so concurrent conversations of different tiers never cross-contaminate.
obs_snip_override: ContextVar[int | None] = ContextVar("disco_obs_snip_override", default=None)


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

# CW-3 — the leading marker of the always-fresh workspace snapshot block (the pinned
# CURRENT WORKSPACE message rendered by view_render.workspace_snapshot_message). Shared
# here (the lowest common module both the loop renderer and the provider import) so the
# provider can locate the block to place an Anthropic cache breakpoint after it, and so
# recovery/pointer prose can reference it by a LOCATION-INDEPENDENT name ("the CURRENT
# WORKSPACE block in this prompt" — never "below"/"above": the block moved to the
# cacheable prefix, so directional words are wrong).
WORKSPACE_SNAPSHOT_SENTINEL = "# CURRENT WORKSPACE"


# K1 — the elision-marker family. `_snip_args` renders an over-long arg as a
# placeholder in the action history. A weak model can COPY that placeholder back
# into a REAL tool argument (e.g. a file_write body), which — if executed — would
# write the marker over real content (DATA LOSS) and re-feed the marker into the
# next file_read (an 88× read loop, reproduced live). The emitted marker is an
# instruction-like sentinel, not flowing prose. The detector below matches BOTH
# the canonical sentinel and historical angle-bracket markers, so old histories
# stay guarded while new histories are less copyable.
_ELISION_MARKER_RE = re.compile(
    r"(?:"
    r"\[\[\s*DISCO-ELIDED:\s*\d[\d,]*\s*chars\b[^\]]*?\]\]"
    r"|"
    r"<\s*\d[\d,]*\s*chars\b[^>]*?\b(?:elided|full content)\b[^>]*>"
    r")"
)
# F01 — fail closed on protocol fragments too.  The historical poisoned file
# contained ``[[DISCO-ELIDED: see above — 6837 char file content ...]]``, which
# is recognizably the reserved sentinel but is not the canonical count-first
# rendering above.  Stream/provider truncation can also drop the final brackets.
# Requiring the reserved bracketed prefix avoids matching ordinary prose that
# merely uses the word "elided"; an optional single opening bracket covers a
# partially emitted prefix.
_ELISION_PARTIAL_RE = re.compile(r"\[{1,2}\s*DISCO-ELIDED\s*:", re.IGNORECASE)
# Historical angle-bracket markers can likewise arrive without their closing
# ``>``.  Require both the count/content anchor and an elision signature within
# one bounded line, so benign HTML and phrases such as ``<5 chars>`` stay valid.
_ELISION_ANGLE_PARTIAL_RE = re.compile(
    r"<\s*(?:\d[\d,]*\s*chars?|content)\b[^\r\n>]{0,500}"
    r"\b(?:elided|placeholder|full\s+content)\b",
    re.IGNORECASE,
)
# CW P1-a — capture the char count from an existing marker so the assist-OFF retarget
# pass can re-render it (pinned vs non-pinned) without re-deriving the original length.
_ELISION_COUNT_RE = re.compile(r"(?:<\s*|\[\[\s*DISCO-ELIDED:\s*)(\d[\d,]*)\s*chars\b")

# BW-02 (trace conv_20fa8482) — a model can PARAPHRASE the neutral marker, dropping the
# leading "<N chars …>" anchor while copying the marker's stable TAIL prose verbatim into
# a real tool argument (observed: "<content elided — re-issue the call or file_read the
# path for the full content; do not copy this placeholder into a tool argument>"). With
# no digit anchor, `_ELISION_MARKER_RE` misses it, so K1 passed it and a 132-byte
# placeholder overwrote a real file (DATA LOSS → build corruption → degenerate tool-less
# resumes). This SECOND detector is for the REJECTION path ONLY: it matches a bounded
# angle-bracket placeholder `<…>` (no greedy cross-`>`) that carries BOTH the marker's
# signature TAIL phrase AND an elision keyword — requiring BOTH so it does NOT
# false-positive on ordinary file content that merely says "elided" in prose. It is NOT
# used by the RETARGET pass (which still needs the char count from `_ELISION_COUNT_RE` to
# reconstruct the neutral marker, and a paraphrase carries no count to reconstruct).
_ELISION_PARAPHRASE_RE = re.compile(
    r"<"
    r"(?=[^>]*\b(?:elided|full content|placeholder)\b)"
    r"[^>]*"
    # signature TAIL phrases — kept in lockstep with `_arg_snip_marker_neutral`. Legacy
    # phrases stay (back-compat for any in-flight marker); the de-temptified wording adds
    # "do not copy or re-send" + "already applied to the workspace".
    r"(?:re-issue the call or file_read the path|do not copy this placeholder"
    r"|do not copy or re-send|already applied to the workspace)"
    r"[^>]*>"
)


def _arg_snip_marker(n: int) -> str:
    return (
        f"[[DISCO-ELIDED: {n:,} chars — history display only; metadata, not file content; "
        "this historical tool argument was already submitted. Do not copy or "
        "re-send this marker. Use current resource state and request only the "
        "minimal range needed for the next action.]]"
    )


# Back-compat helper name: callers/tests may still import the old assist-ON marker
# constructor, but the emitted format is now one canonical sentinel.
def _arg_snip_marker_below(n: int) -> str:
    return _arg_snip_marker(n)


# Back-compat helper name: the assist-OFF retarget pass now also emits the same
# sentinel, preserving one canonical marker across render paths.
def _arg_snip_marker_neutral(n: int) -> str:
    return _arg_snip_marker(n)


def _snip_args(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > _ARG_SNIP_CHARS:
            out[k] = _arg_snip_marker(len(v))
        else:
            out[k] = v
    return out


def retarget_elided_arg_markers(messages: list[LLMMessage]) -> list[LLMMessage]:
    """CW P1-a (round-2) — assist-OFF render pass: rewrite each elided tool-call
    ARGUMENT marker to the NEUTRAL, non-dangling marker.

    Historical render paths emitted tier-specific angle-bracket markers. This
    pass now rewrites any count-bearing elision marker to the canonical sentinel
    with NO per-arg pinned-vs-omitted guessing. Pure: returns a new list; input
    unchanged.
    """
    out: list[LLMMessage] = []
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            out.append(msg)
            continue
        new_tcs: list[dict[str, Any]] = []
        mutated = False
        for tc in msg.tool_calls:
            if not isinstance(tc, dict):
                new_tcs.append(tc)
                continue
            args = tc.get("arguments")
            if not isinstance(args, dict):
                new_tcs.append(tc)
                continue
            new_args: dict[str, Any] | None = None
            for k, v in args.items():
                if not isinstance(v, str):
                    continue
                count = _ELISION_COUNT_RE.match(v)
                if count is None or _ELISION_MARKER_RE.search(v) is None:
                    continue  # not one of our markers — leave the model's arg alone
                n = int(count.group(1).replace(",", ""))
                replacement = _arg_snip_marker_neutral(n)
                if replacement == v:
                    continue
                if new_args is None:
                    new_args = dict(args)
                new_args[k] = replacement
            if new_args is not None:
                new_tc = dict(tc)
                new_tc["arguments"] = new_args
                new_tcs.append(new_tc)
                mutated = True
            else:
                new_tcs.append(tc)
        out.append(msg.model_copy(update={"tool_calls": new_tcs}) if mutated else msg)
    return out


def find_elided_arg_markers(arguments: dict[str, object]) -> list[str]:
    """K1 execution guard: return argument paths whose string value carries an
    elision placeholder (the `_snip_args` marker copied back by a weak model).
    Empty list ⇒ the arguments are clean and safe to execute. Pure + deterministic.

    Matches BOTH the canonical `[[DISCO-ELIDED: N chars ...]]` marker, historical
    structural `<N chars … {elided|full content} …>` markers, and a model-
    PARAPHRASED placeholder that dropped the count anchor but kept the marker's
    signature tail prose (BW-02), and reserved/malformed protocol fragments (F01).
    Recurses through JSON objects and arrays so nested edit batches, JSON Patch,
    semantic app content, and future structured mutators cannot bypass the
    universal executor boundary. Rejection-only — retargeting remains count-based."""

    def _walk(value: object, path: str) -> list[str]:
        if isinstance(value, str):
            if any(
                pattern.search(value) is not None
                for pattern in (
                    _ELISION_MARKER_RE,
                    _ELISION_PARAPHRASE_RE,
                    _ELISION_PARTIAL_RE,
                    _ELISION_ANGLE_PARTIAL_RE,
                )
            ):
                return [path]
            return []
        if isinstance(value, dict):
            found: list[str] = []
            for key, nested in value.items():
                child = f"{path}.{key}" if path else str(key)
                found.extend(_walk(nested, child))
            return found
        if isinstance(value, (list, tuple)):
            found = []
            for index, nested in enumerate(value):
                found.extend(_walk(nested, f"{path}[{index}]"))
            return found
        return []

    return _walk(arguments, "")


def value_is_only_elision_marker(value: object) -> bool:
    """True when `value` is a string consisting of NOTHING BUT an elision
    placeholder (plus surrounding whitespace) — i.e. the model copied the
    `_snip_args` marker back verbatim with no real content of its own around it.

    Used by the K1 recovery path: a PURE marker copy-back can be safely
    re-expanded to the original content the marker stood in for (the engine still
    holds it in the event log), because re-expansion can't clobber any real text
    the model authored — there is none. A marker EMBEDDED in real text (the model
    wrote a header, a marker, a footer) returns False so the recovery never
    silently drops the model's surrounding edits; that case falls through to the
    rejection-and-re-read path instead. Pure + deterministic."""
    if not isinstance(value, str):
        return False
    stripped = _ELISION_ANGLE_PARTIAL_RE.sub(
        "",
        _ELISION_PARTIAL_RE.sub(
            "", _ELISION_PARAPHRASE_RE.sub("", _ELISION_MARKER_RE.sub("", value))
        ),
    )
    # Nothing matched ⇒ not a marker at all; or matched but real text remains.
    return value != stripped and stripped.strip() == ""


# Execution-receipt trailer: bound on the trusted exit_code magnitude. Real OS
# exit statuses fit in a byte and signal-death encodings in ~3 digits; anything
# outside this window is not a plausible exit status, so the trailer is omitted
# rather than rendering an unbounded (or hostile) payload value to the model.
_EXIT_RECEIPT_MAX_ABS = 999_999


def _execution_receipt_trailer(structured: dict[str, Any] | None) -> str | None:
    """Render the compact execution receipt (e.g. ``[exit 0]``) for an exec-family
    observation, or None when the structured payload carries no exit status.

    Success-path exec observations render only stdout to the model, while the
    exit status the host already proved lives solely in ``structured`` — so the
    model would re-run a command purely to learn whether it succeeded (the
    TOOL_CALL_THRASH failure). Surfacing the receipt at RENDER time keeps the
    durable event bytes (and everything downstream of them: frontend transcript,
    exact-read receipt matching, spill caps, output-truth stdout comparisons)
    byte-identical, and applies retroactively to already-stored events.

    Strictly sourced from the tool's own structured payload — never synthesized:
    a payload without a plausible integer ``exit_code`` yields no trailer.
    Tool-agnostic by construction (any tool whose structured result reports an
    exit_code benefits); pure + deterministic so View.of purity is preserved.
    """
    if not structured:
        return None
    exit_code = structured.get("exit_code")
    # bool is an int subclass — a True/False "exit_code" is not a real status.
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    if abs(exit_code) > _EXIT_RECEIPT_MAX_ABS:
        return None
    if structured.get("timed_out") is True:
        return f"[exit {exit_code}, timed out]"
    return f"[exit {exit_code}]"


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
        # CW-6: assist-OFF raises this cap (in tandem with the read budget) so a
        # large file_read observation is not snipped to a corrupted head/tail. The
        # override is set per-build by ViewBuilder from the derived caps; when unset
        # (assist-ON / no window) it is the byte-identical 8k calibration below.
        max_chars, head, tail = _OBS_SNIP_CHARS, _OBS_SNIP_HEAD, _OBS_SNIP_TAIL
        override = obs_snip_override.get()
        if override is not None and override > _OBS_SNIP_CHARS:
            # Scale head/tail to the raised cap using the same 5:2 ratio so a
            # genuinely-oversize output (beyond the raised cap) still degrades
            # gracefully; the cap itself is what spares an in-budget read.
            max_chars, head, tail = override, override * 5 // 8, override * 2 // 8
        content = snip_content(
            self.tool_result.content,
            max_chars=max_chars,
            head=head,
            tail=tail,
        )
        # Execution receipt: appended AFTER the snip so the cap can never destroy
        # it, and only at this render seam so durable event bytes stay unchanged.
        # (The failure path is a different event class — AgentErrorEvent already
        # carries "exited N" in its error text — so there is no double report.)
        trailer = _execution_receipt_trailer(self.tool_result.structured)
        if trailer is not None:
            content = f"{content}\n{trailer}" if content else trailer
        return LLMMessage(
            role="tool",
            content=content,
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
    # [REL-RC-E] The tool's human-readable recovery guidance (ToolOutcome.content), capped. `error`
    # stays the canonical CODE (e.g. "bad_range") — the stuck-detector's byte-identical matching and
    # the classifier key on it — while `detail` carries the actionable text ("(N lines) valid range
    # 1..N+1") so the model isn't left retrying blind against a bare error code.
    detail: str | None = None
    action_id: str | None = None  # the action that failed, if any
    tool_call_id: str | None = None  # for pairing with the assistant tool_call
    # A validated invocation may fail after exercising capabilities or even
    # after a partial host-observed effect. Preserve the executor's typed
    # evidence here rather than discarding it when a failed ToolResult is
    # projected into the event log. Pre-execution refusals keep both defaults.
    action_profile: ActionProfile | None = None
    effect_receipts: tuple[EffectReceipt, ...] = ()

    def to_llm_message(self) -> LLMMessage:
        # Pre-formatted content (e.g. wrapped in <system-reminder>...</…>) is
        # rendered as-is; raw error strings get the "ERROR:" prefix for the
        # model's parse. This lets the rejection path inject ambient reminders
        # without the user-tone framing of a tool-failure message.
        if self.error.startswith("<"):
            content = self.error
        else:
            content = f"ERROR: {self.error}"
            # [REL-RC-E] surface the tool's recovery guidance (if it adds info beyond the code) so a
            # domain error like bad_range shows the model the valid range instead of a bare code.
            detail = (self.detail or "").strip()
            if detail and detail != self.error.strip():
                content = f"{content}\n{detail}"
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


class StatusEvent(BaseEvent):
    """A lifecycle/status transition. NOT LLMConvertible. Drives the UI and the
    loop's state machine reconstruction (§3)."""

    kind: Literal[EventKind.STATUS] = EventKind.STATUS
    source: EventSource = EventSource.SYSTEM
    status: ConversationStatus
    detail: str | None = None
    # A run may fail before it has materialized a model view (driver/sandbox
    # preflight).  Bind that status to the exact durable ingress instead of
    # emitting an unowned ERROR that an older worker could append over a newer
    # run.  Once a view exists, ``agent_view_id`` is the authority instead.
    run_intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Host-owned reseals use a separate, explicit authority edge instead of an
    # untagged FINISHED that could be confused with late agent output.
    host_mutation_id: str | None = Field(default=None, min_length=1, max_length=128)
    plan_verification_transition: PlanVerificationTransition | None = None
    plan_verifier_failure: PlanVerifierFailure | None = None
    plan_verifier_pass: PlanVerifierPass | None = None
    # K4 typed recovery state. Historical status events remain valid; ``detail``
    # stays as the legacy UI/debug surface until shadow parity permits removal.
    recovery_lease_transition: RecoveryLeaseTransition | None = None

    @model_validator(mode="after")
    def _one_terminal_authority(self) -> StatusEvent:
        authorities = (
            self.agent_view_id,
            self.host_mutation_id,
            self.run_intent_id,
        )
        if sum(authority is not None for authority in authorities) > 1:
            raise ValueError(
                "status cannot carry more than one of agent-view, host-mutation, "
                "or run-intent authority"
            )
        if self.plan_verifier_pass is not None and (
            self.status != ConversationStatus.RUNNING or self.detail != "plan_verification_passed"
        ):
            raise ValueError("plan_verifier_pass requires RUNNING/plan_verification_passed")
        if self.detail == "plan_verification_passed" and self.plan_verifier_pass is None:
            raise ValueError("RUNNING/plan_verification_passed requires a typed plan_verifier_pass")
        return self


class WorkspaceVersionEvent(BaseEvent):
    """A durable workspace version finished persisting.

    ``FINISHED`` is appended by the loop before the runtime copies the sandbox
    workspace into project storage.  Consumers must therefore use this event,
    rather than the terminal status, as the commit signal for version history.
    NOT LLMConvertible — this is storage/UI synchronization bookkeeping.
    """

    kind: Literal[EventKind.WORKSPACE_VERSION] = EventKind.WORKSPACE_VERSION
    source: EventSource = EventSource.SYSTEM
    version_seq: int
    tree_digest: str
    trigger: str
    # K6 final-state proof. Historical events remain valid with no seal; strict
    # promotion requires this field only after the lifecycle/store migration.
    final_seal: FinalWorkspaceSeal | None = None

    @model_validator(mode="after")
    def _seal_matches_version_event(self) -> WorkspaceVersionEvent:
        seal = self.final_seal
        if seal is None:
            return self
        if self.trigger != "finish":
            raise ValueError("a final workspace seal requires a finish-triggered version event")
        if seal.version_seq != self.version_seq:
            raise ValueError("final seal version sequence does not match event")
        if seal.tree_digest != self.tree_digest:
            raise ValueError("final seal tree digest does not match event")
        if self.seq is not None and seal.terminal_seq >= self.seq:
            raise ValueError("final seal terminal sequence must precede version event")
        return self


class WorkspaceRestoredEvent(BaseEvent):
    """A user-requested workspace rollback was applied.

    NOT LLMConvertible — this is audit/UI bookkeeping. The restored files live in
    the workspace/version store; the model sees the current workspace through the
    fresh per-turn workspace snapshot instead of this event body.
    """

    kind: Literal[EventKind.WORKSPACE_RESTORED] = EventKind.WORKSPACE_RESTORED
    source: EventSource = EventSource.USER
    version_seq: int
    tree_digest: str
    label: str = ""


class WorkspaceMutationEvent(BaseEvent):
    """Conservative fence for a host-owned workspace edit.

    Runtime uploads, editor writes, restores, and imports do not travel through
    the agent Action/Observation envelope. Writers persist this event before
    their first mutation while holding the shared workspace lock. It makes an
    earlier final seal historical without pretending to identify exact bytes;
    those belong to a later immutable final seal.
    """

    kind: Literal[EventKind.WORKSPACE_MUTATION] = EventKind.WORKSPACE_MUTATION
    source: EventSource = EventSource.SYSTEM
    operation: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    paths: tuple[str, ...] = ()
    # Set only on typed ``agent.view-admitted`` records. Sequence order alone
    # cannot prove which user intent a view consumed when workers overlap.
    run_intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Strict attribution starts at v1. None preserves the old sequence-only
    # reducer for historical logs until their first typed view admission.
    run_protocol_version: Literal[1] | None = None

    @model_validator(mode="after")
    def _valid_run_protocol_shape(self) -> WorkspaceMutationEvent:
        if self.operation == "agent.view-admitted":
            if (
                self.run_protocol_version != 1
                or self.run_intent_id is None
                or self.agent_view_id is None
            ):
                raise ValueError("typed view admission requires intent, view, and protocol v1")
        elif self.operation.startswith("agent.run-intent."):
            if self.run_intent_id is not None or self.agent_view_id is not None:
                raise ValueError("run intent cannot carry admission authority")
        elif self.run_intent_id is not None or self.run_protocol_version is not None:
            raise ValueError("run protocol fields are valid only on intent/admission events")
        return self

    @field_validator("paths")
    @classmethod
    def _bounded_diagnostic_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 64:
            raise ValueError("workspace mutation diagnostic paths exceed 64")
        normalized = tuple(path.strip() for path in value)
        if any(not path or len(path) > 512 for path in normalized):
            raise ValueError("workspace mutation paths must be non-empty and bounded")
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("workspace mutation paths must be sorted and unique")
        return normalized


class BuildPlatformAdmissionEvent(BaseEvent):
    """Durable route/composition identity for one Build run intent."""

    kind: Literal[EventKind.BUILD_PLATFORM_ADMISSION] = EventKind.BUILD_PLATFORM_ADMISSION
    source: EventSource = EventSource.SYSTEM
    route: Literal["legacy", "platform"]
    profile_id: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{0,62}\.[a-z][a-z0-9_-]{0,62}@[1-9][0-9]*(?:\.[0-9]+){0,2}$"
    )
    run_intent_id: str = Field(min_length=1, max_length=160)
    composition_authority: Literal["legacy", "build_platform_core"]
    execution_bridge: Literal["legacy_host"] = "legacy_host"
    composition_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    run_identity: str | None = Field(
        default=None,
        pattern=r"^run:sha256:[0-9a-f]{64}$",
    )
    transition: Literal["initial", "appkit_ejection"] = "initial"
    supersedes_admission_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Exact target proof requirements resolved into this composition. Optional
    # for backward-compatible legacy admissions; platform admissions persist it
    # so restart/code drift cannot silently change the completion authority.
    verification_claims: tuple[HostVerificationClaim, ...] = ()

    @model_validator(mode="after")
    def _route_identity_is_exact(self) -> BuildPlatformAdmissionEvent:
        claim_ids = [claim.claim_id for claim in self.verification_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Build admission verification claim ids must be unique")
        if self.route == "platform":
            if self.composition_authority != "build_platform_core":
                raise ValueError("Platform route requires Build Platform Core authority")
            if self.composition_digest is None or self.run_identity is None:
                raise ValueError("Platform route requires composition and run identities")
        elif (
            self.composition_authority != "legacy"
            or self.composition_digest is not None
            or self.run_identity is not None
        ):
            raise ValueError("legacy route cannot claim a Platform composition identity")
        if self.transition == "appkit_ejection":
            if self.profile_id != "disco.freeform_web@1" or self.supersedes_admission_id is None:
                raise ValueError(
                    "AppKit ejection must supersede an admission with the Freeform profile"
                )
        elif self.supersedes_admission_id is not None:
            raise ValueError("an initial Build admission cannot supersede another admission")
        return self


class AppKitEjectionEvent(BaseEvent):
    """A confirmed governed-to-Freeform revision boundary.

    The immutable source revision remains available for history/preview. The
    target revision is explicitly not AppKit-verified; later generic verification
    may verify it as Freeform but can never retroactively restore AppKit guarantees.
    """

    kind: Literal[EventKind.APPKIT_EJECTION] = EventKind.APPKIT_EJECTION
    source: EventSource = EventSource.SYSTEM
    action_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(min_length=1, max_length=128)
    source_profile_id: Literal["disco.appkit_web@1"] = "disco.appkit_web@1"
    target_profile_id: Literal["disco.freeform_web@1"] = "disco.freeform_web@1"
    source_version_seq: int = Field(ge=1)
    source_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ejected_version_seq: int = Field(ge=1)
    ejected_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lost_guarantees: tuple[str, ...] = Field(min_length=1)
    appkit_verified: Literal[False] = False

    @model_validator(mode="after")
    def _revision_is_distinct_and_truthful(self) -> AppKitEjectionEvent:
        if self.source_version_seq == self.ejected_version_seq:
            raise ValueError("AppKit ejection must create a distinct workspace revision")
        if self.source_tree_digest == self.ejected_tree_digest:
            raise ValueError("AppKit ejection revision must record the profile-boundary change")
        if self.lost_guarantees != APPKIT_EJECTION_LOST_GUARANTEES:
            raise ValueError("AppKit ejection must disclose the complete lost-guarantee set")
        return self


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
        lines: list[str] = []
        for i, step in enumerate(self.steps, start=1):
            lines.append(f"{i}. {step.title}")
            if step.detail:
                lines.append(f"   Detail: {step.detail}")
            if step.done_condition is not None:
                condition = json.dumps(
                    predicate_to_dict(step.done_condition),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                lines.append(f"   Done condition: {condition}")
        body = "\n".join(lines)
        # Include the context so the executing agent has its own findings in-View
        # — same role Claude Code's plan-file markdown plays during execution.
        ctx = f"\n\nContext:\n{self.context}" if self.context else ""
        return LLMMessage(
            role="assistant",
            content=f"Plan (revision {self.revision}): {self.summary}\n{body}{ctx}",
        )


# The per-sub-question ROUND cap ("rounds") bounds iteration DEPTH, not coverage —
# every sub-question still produces a full section — so it is NOT a truncation and must
# never surface as "bounded by / some sub-questions were not covered". Only genuine
# coverage truncations (e.g. sources / wall_clock / subquestions) are surfaced.
_NON_TRUNCATING_BOUNDS = frozenset({"rounds"})


def report_truncation(bounded_by: str | None) -> str | None:
    """The ``bounded_by`` value to SURFACE as a real truncation, or None when coverage
    completed (a natural finish, or the non-truncating 'rounds' depth cap). Shared by
    every consumer (screen notice, PDF/markdown export, LLM-context header) so they
    never diverge on what counts as a truncation."""
    b = (bounded_by or "").strip()
    return b if b and b not in _NON_TRUNCATING_BOUNDS else None


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
        _trunc = report_truncation(self.bounded_by)
        bound = f"\n\n(Bounded by: {_trunc}.)" if _trunc else ""
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
        lines = [f"{i}. {o.title} — {o.description}" for i, o in enumerate(self.options, start=1)]
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
            content=f'<datasource name="{self.name}">\n{self.docs}\n</datasource>',
        )


class DeliverableEvent(BaseEvent, LLMConvertible):
    """The agent's explicit 'here is the finished thing' handoff (Build). It names
    WHAT was produced and WHERE, so the UI can present a real handoff (open the
    live app / download the files) instead of leaving the user to guess what the
    run made. Distinct from the live preview (which shows work-in-progress): a
    DeliverableEvent is the agent affirming a result is ready.

    `kind`:
      • "app"   — a runnable result the user opens in the live preview (a built
                  site / running dev server selected by the entry file at `path`).
      • "files" — one or more workspace artifacts to download/inspect (`path` is
                  the file or directory).

    LLMConvertible so the agent's own context reflects what it has already handed
    off (it shouldn't re-deliver the same thing); the body stays terse."""

    kind: Literal[EventKind.DELIVERABLE] = EventKind.DELIVERABLE
    source: EventSource = EventSource.AGENT
    title: str  # short human label, e.g. "Landing page" / "Sales report"
    path: str  # workspace-relative path (entry file for apps, file/dir for files)
    artifact_kind: Literal["app", "files"] = "app"
    # Optional canonical URL the deliverable is reachable at (a deploy target, a
    # tunnel, or the live preview). When the agent serves to a known address it
    # passes it on `serve(url=…)`; the UI surfaces an "Open deployed app" link.
    deployment_url: str = ""

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=(
                f'<deliverable kind="{self.artifact_kind}" path="{self.path}">\n'
                f"{self.title}\n</deliverable>"
            ),
        )


class VerifierStartedEvent(BaseEvent):
    """REL-1b — the host verifier started inspecting an artifact.

    NOT LLMConvertible — this is audit/UI bookkeeping for the host-owned
    verifier path, never model context.
    """

    kind: Literal[EventKind.VERIFIER_STARTED] = EventKind.VERIFIER_STARTED
    source: EventSource = EventSource.SYSTEM
    artifact_path: str = ""
    artifact_kind: str = "files"
    verifier: str = "host"
    requested_by_event_id: str | None = None


class VerifierVerdictEvent(BaseEvent):
    """REL-1b — the host verifier's verdict for an artifact.

    NOT LLMConvertible — later REL-1 steps may project this into phase/manifest
    state, but it is never rendered into the model's message view.
    """

    kind: Literal[EventKind.VERIFIER_VERDICT] = EventKind.VERIFIER_VERDICT
    source: EventSource = EventSource.SYSTEM
    artifact_path: str = ""
    artifact_kind: str = "files"
    verified: bool = False
    verdict: str | None = None
    detail: str | None = None
    failures: list[dict[str, Any]] = Field(default_factory=list)
    # Workspace-relative screenshot named by the host verifier's own verdict.
    # This is provenance only: its presence never implies ``verified=True``.
    # Keep the event boundary bounded because verifier output is externally
    # derived and persists in the append-only event log.
    screenshot_path: str | None = Field(default=None, max_length=512)
    verification_result: HostVerificationResult | None = None

    @model_validator(mode="after")
    def _typed_result_matches_event(self) -> VerifierVerdictEvent:
        result = self.verification_result
        if result is None:
            return self
        if result.artifact_path != self.artifact_path or result.artifact_kind != self.artifact_kind:
            raise ValueError("typed verification result artifact does not match event")
        typed_verified = result.status is VerificationClaimStatus.PASS
        if self.verified is not typed_verified:
            raise ValueError("verified flag does not match typed verification result")
        if self.verdict != result.status.value:
            raise ValueError("verdict label does not match typed verification result")
        if (self.screenshot_path or "") != result.screenshot_path:
            raise ValueError("screenshot provenance does not match typed verification result")
        return self


class VerifierShadowEvent(BaseEvent):
    """REL-1b — shadow-mode comparison between inline and host verifier results.

    NOT LLMConvertible — this records agreement evidence during shadow/canary
    rollout without changing the agent's model context.
    """

    kind: Literal[EventKind.VERIFIER_SHADOW] = EventKind.VERIFIER_SHADOW
    source: EventSource = EventSource.SYSTEM
    artifact_path: str = ""
    artifact_kind: str = "files"
    inline_verdict: str | None = None
    host_verdict: str | None = None
    agreement: bool | None = None
    detail: str | None = None


class ErrorEvent(BaseEvent):
    """A conversation-level (fatal-ish) error, e.g. MaxIterationsReached.
    NOT LLMConvertible."""

    kind: Literal[EventKind.ERROR] = EventKind.ERROR
    source: EventSource = EventSource.SYSTEM
    code: str
    detail: str


class ScheduleEvent(BaseEvent):
    """A schedule was created or deleted for this conversation (RP-08).
    NOT LLMConvertible — it is a system lifecycle event."""

    kind: Literal[EventKind.SCHEDULE] = EventKind.SCHEDULE
    source: EventSource = EventSource.SYSTEM
    action: Literal["created", "deleted"]
    schedule_id: str
    rrule: str  # cron expression
    description: str


class ScheduleRunEvent(BaseEvent):
    """A scheduled run fired and was appended to this conversation (RP-08).
    NOT LLMConvertible — it is a system lifecycle event.

    `coalesced` is True when the server was down across N missed fires and this
    single run stands in for all of them (run-once-coalesced policy)."""

    kind: Literal[EventKind.SCHEDULE_RUN] = EventKind.SCHEDULE_RUN
    source: EventSource = EventSource.SYSTEM
    schedule_id: str
    coalesced: bool = False


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


class ClarifyEvent(BaseEvent):
    """Pre-plan clarification gate (RP-13). When the request is ambiguous and
    the planner needs structured user input before committing to a plan, it
    calls the `clarify` virtual tool. The loop intercepts the call, emits this
    event carrying MULTIPLE typed questions, and halts at
    AWAITING_USER_QUESTION. The user answers each question; the answers are
    re-injected as context and planning proceeds.

    NOT LLMConvertible — the clarify card is a UI gate, not a model-facing event.
    The user's answers are re-injected as a regular USER MessageEvent, which the
    model reads on its next View."""

    kind: Literal[EventKind.CLARIFY] = EventKind.CLARIFY
    source: EventSource = EventSource.AGENT
    question: str  # overarching question / summary of what needs clarification
    items: list[ClarifyQuestionItem]


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


class QuestionsV2Event(BaseEvent):
    """Structured pre-plan intake gate (§K).

    In interactive planning, the model may emit exactly one batched
    `questions_v2` tool call before `submit_plan`. The loop turns that call into
    this typed event and halts at AWAITING_USER_QUESTION. Autonomous runs skip the
    gate and put assumptions in the plan context instead.
    """

    kind: Literal[EventKind.QUESTIONS_V2] = EventKind.QUESTIONS_V2
    source: EventSource = EventSource.AGENT
    question: str
    items: list[QuestionsV2Item]


class ContextResolvedEvent(BaseEvent):
    """CXT-3 — the agent's DEFERRED 'snip' mark: a seq range [start,end] the agent
    has declared resolved (an exploration concluded, stale tool chatter) and that
    MAY be forgotten from the model view later — but ONLY once a durable summary
    exists and context pressure warrants it (context_compact_if_needed). On its
    own this event changes nothing: it does not tombstone, so View.of is unaffected
    until a CondensationEvent is actually emitted for the range.

    NOT LLMConvertible — pure intent/bookkeeping. The model only ever sees the
    inline summary of the CondensationEvent that eventually executes the snip."""

    kind: Literal[EventKind.CONTEXT_RESOLVED] = EventKind.CONTEXT_RESOLVED
    source: EventSource = EventSource.AGENT
    range_id: str = Field(default_factory=lambda: f"cxr_{uuid.uuid4().hex}")
    forgotten_start_seq: int
    forgotten_end_seq: int
    reason: str = "resolved"
    # Set once a durable summary file has been written for this range (CXT-2 SUMMARY).
    summary_ref_path: str | None = None


class ContextSummaryEvent(BaseEvent):
    """CXT-3 — records that a durable summary was written for a resolved range
    (the 'state exists elsewhere' precondition for compaction). The non-empty
    `summary` is the content that will replace the forgotten span when the snip
    executes, so its presence + non-emptiness is the durability proof.

    NOT LLMConvertible — internal context-compaction marker."""

    kind: Literal[EventKind.CONTEXT_SUMMARY] = EventKind.CONTEXT_SUMMARY
    source: EventSource = EventSource.SYSTEM
    range_id: str
    rel_path: str
    summary: str
    artifact_kind: str = "summary"


# ---- the discriminated union the store/serde use ----------------------------

Event = Annotated[
    MessageEvent
    | ActionEvent
    | ObservationEvent
    | AgentErrorEvent
    | CondensationEvent
    | StatusEvent
    | WorkspaceVersionEvent
    | WorkspaceRestoredEvent
    | WorkspaceMutationEvent
    | BuildPlatformAdmissionEvent
    | AppKitEjectionEvent
    | PlanEvent
    | ReportEvent
    | AlternativesEvent
    | KnowledgeEvent
    | DatasourceEvent
    | DeliverableEvent
    | VerifierStartedEvent
    | VerifierVerdictEvent
    | VerifierShadowEvent
    | ErrorEvent
    | ScheduleEvent
    | ScheduleRunEvent
    | ClarifyEvent
    | QuestionsV2Event
    | ContextResolvedEvent
    | ContextSummaryEvent,
    Field(discriminator="kind"),
]


def active_verification_requirements_event(
    events: Iterable[Event],
) -> MessageEvent | None:
    """Fold the latest causally valid complete user/scenario requirement snapshot."""

    active: MessageEvent | None = None
    for event in events:
        if (
            not isinstance(event, MessageEvent)
            or event.source is not EventSource.USER
            or event.verification_requirements is None
        ):
            continue
        expected = active.id if active is not None else None
        if event.verification_requirements.supersedes_event_id == expected:
            active = event
    return active


def current_build_platform_admission(
    events: Iterable[Event],
) -> BuildPlatformAdmissionEvent | None:
    """Fold the route pin for the current non-terminal Build segment.

    PAUSED and user/approval gates retain the pin. A genuine terminal status
    releases it, so a later run may apply the current rollout switch.
    """

    materialized = list(events)
    admissions = sorted(
        (
            event
            for event in materialized
            if isinstance(event, BuildPlatformAdmissionEvent) and type(event.seq) is int
        ),
        key=lambda event: event.seq or -1,
    )
    latest: BuildPlatformAdmissionEvent | None = None
    for candidate in admissions:
        assert isinstance(candidate.seq, int)
        prior_for_intent = [
            event
            for event in admissions
            if isinstance(event.seq, int)
            and event.seq < candidate.seq
            and event.run_intent_id == candidate.run_intent_id
        ]
        if candidate.transition == "initial":
            if not prior_for_intent:
                latest = candidate
            continue
        superseded = next(
            (
                event
                for event in prior_for_intent
                if event.id == candidate.supersedes_admission_id
                and event.profile_id == "disco.appkit_web@1"
            ),
            None,
        )
        ejection = current_appkit_ejection(materialized)
        ejection_action = next(
            (
                event
                for event in materialized
                if ejection is not None
                and isinstance(event, ActionEvent)
                and event.id == ejection.action_id
            ),
            None,
        )
        ejection_intent = max(
            (
                event
                for event in materialized
                if ejection_action is not None
                and isinstance(ejection_action.seq, int)
                and isinstance(event, WorkspaceMutationEvent)
                and isinstance(event.seq, int)
                and event.seq < ejection_action.seq
                and event.operation.startswith("agent.run-intent.")
            ),
            key=lambda event: event.seq or -1,
            default=None,
        )
        if (
            superseded is not None
            and isinstance(superseded.seq, int)
            and ejection is not None
            and isinstance(ejection.seq, int)
            and ejection_intent is not None
            and ejection_intent.id == candidate.run_intent_id
            and superseded.seq < ejection.seq < candidate.seq
        ):
            latest = candidate
    if latest is None or type(latest.seq) is not int:
        return None
    terminal = {
        ConversationStatus.FINISHED,
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.IDLE,
    }
    if any(
        isinstance(event, StatusEvent)
        and type(event.seq) is int
        and event.seq > latest.seq
        and event.status in terminal
        for event in materialized
    ):
        return None
    return latest


def current_appkit_ejection(events: Iterable[Event]) -> AppKitEjectionEvent | None:
    """Fold a fully confirmed and revision-bound AppKit ejection, if any."""

    materialized = list(events)
    candidates = sorted(
        (
            event
            for event in materialized
            if isinstance(event, AppKitEjectionEvent) and type(event.seq) is int
        ),
        key=lambda event: event.seq or -1,
        reverse=True,
    )
    for ejection in candidates:
        assert isinstance(ejection.seq, int)
        action = next(
            (
                event
                for event in materialized
                if isinstance(event, ActionEvent)
                and type(event.seq) is int
                and event.seq < ejection.seq
                and event.id == ejection.action_id
                and event.agent_view_id == ejection.agent_view_id
                and event.tool_call.call_id == ejection.tool_call_id
                and event.tool_call.tool_name == "request_custom_build"
            ),
            None,
        )
        if action is None or not isinstance(action.seq, int):
            continue
        intent = max(
            (
                event
                for event in materialized
                if isinstance(event, WorkspaceMutationEvent)
                and type(event.seq) is int
                and event.seq < ejection.seq
                and event.operation.startswith("agent.run-intent.")
            ),
            key=lambda event: event.seq or -1,
            default=None,
        )
        admission = next(
            (
                event
                for event in materialized
                if intent is not None
                and isinstance(intent.seq, int)
                and isinstance(event, WorkspaceMutationEvent)
                and type(event.seq) is int
                and intent.seq < event.seq < action.seq
                and event.operation == "agent.view-admitted"
                and event.run_intent_id == intent.id
                and event.agent_view_id == action.agent_view_id
            ),
            None,
        )
        if admission is None:
            continue
        waiting = next(
            (
                event
                for event in materialized
                if isinstance(event, StatusEvent)
                and type(event.seq) is int
                and action.seq < event.seq < ejection.seq
                and event.status is ConversationStatus.WAITING_FOR_CONFIRMATION
                and event.detail == action.id
            ),
            None,
        )
        resumed = next(
            (
                event
                for event in materialized
                if waiting is not None
                and isinstance(event, StatusEvent)
                and type(event.seq) is int
                and isinstance(waiting.seq, int)
                and waiting.seq < event.seq < ejection.seq
                and event.status is ConversationStatus.RUNNING
                and event.agent_view_id == action.agent_view_id
            ),
            None,
        )
        source = next(
            (
                event
                for event in materialized
                if isinstance(event, WorkspaceVersionEvent)
                and type(event.seq) is int
                and resumed is not None
                and isinstance(resumed.seq, int)
                and resumed.seq < event.seq < ejection.seq
                and event.trigger == APPKIT_EJECTION_SOURCE_TRIGGER
                and event.version_seq == ejection.source_version_seq
                and event.tree_digest == ejection.source_tree_digest
            ),
            None,
        )
        target = next(
            (
                event
                for event in materialized
                if isinstance(event, WorkspaceVersionEvent)
                and type(event.seq) is int
                and source is not None
                and isinstance(source.seq, int)
                and source.seq < event.seq < ejection.seq
                and event.trigger == APPKIT_EJECTION_TARGET_TRIGGER
                and event.version_seq == ejection.ejected_version_seq
                and event.tree_digest == ejection.ejected_tree_digest
            ),
            None,
        )
        if resumed is not None and source is not None and target is not None:
            return ejection
    return None


def _workspace_run_intent_state(
    events: Iterable[Event],
) -> tuple[WorkspaceMutationEvent | None, str | None, int, int, bool]:
    """Newest intent, latest view, progress, and whether strict v1 is active."""

    latest_intent: WorkspaceMutationEvent | None = None
    latest_view_id: str | None = None
    latest_admission_seq = -1
    latest_progress_seq = -1
    strict = False
    protocol_v1_seen = False
    view_action_ids: set[str] = set()
    for event in events:
        seq = event.seq
        if type(seq) is not int or seq < 1:
            continue
        if isinstance(event, WorkspaceMutationEvent):
            if event.operation.startswith("agent.run-intent."):
                protocol_v1_seen = protocol_v1_seen or event.run_protocol_version == 1
                if latest_intent is None or seq > (latest_intent.seq or -1):
                    latest_intent = event
                    latest_view_id = None
                    latest_admission_seq = -1
                    latest_progress_seq = -1
                    strict = protocol_v1_seen
                    view_action_ids.clear()
            elif (
                latest_intent is not None
                and event.operation == "agent.view-admitted"
                and event.run_intent_id == latest_intent.id
                and event.agent_view_id is not None
                and event.run_protocol_version == 1
            ):
                protocol_v1_seen = True
                latest_view_id = event.agent_view_id
                latest_admission_seq = seq
                latest_progress_seq = -1
                strict = True
                view_action_ids.clear()
            elif (
                latest_intent is not None and not strict and event.operation == "agent.run-admitted"
            ):
                latest_admission_seq = seq
        elif isinstance(event, ActionEvent):
            if strict and latest_view_id is not None and event.agent_view_id == latest_view_id:
                view_action_ids.add(event.id)
                latest_progress_seq = max(latest_progress_seq, seq)
            elif not strict and latest_admission_seq >= 0:
                latest_progress_seq = max(latest_progress_seq, seq)
        elif isinstance(event, ObservationEvent):
            if (
                strict
                and latest_view_id is not None
                and event.agent_view_id == latest_view_id
                and event.action_id in view_action_ids
            ):
                latest_progress_seq = max(latest_progress_seq, seq)
            elif not strict and latest_admission_seq >= 0:
                latest_progress_seq = max(latest_progress_seq, seq)
        elif isinstance(event, PlanEvent) or (
            isinstance(event, MessageEvent) and event.source is EventSource.AGENT
        ):
            if strict and latest_view_id is not None and event.agent_view_id == latest_view_id:
                latest_progress_seq = max(latest_progress_seq, seq)
            elif not strict and latest_admission_seq >= 0:
                latest_progress_seq = max(latest_progress_seq, seq)
        elif (
            isinstance(event, StatusEvent)
            and event.status is ConversationStatus.FINISHED
            and strict
            and latest_view_id is not None
            and event.agent_view_id == latest_view_id
        ):
            latest_progress_seq = max(latest_progress_seq, seq)
    return latest_intent, latest_view_id, latest_admission_seq, latest_progress_seq, strict


def latest_workspace_run_intent(events: Iterable[Event]) -> WorkspaceMutationEvent | None:
    """Return the newest durable workspace execution intent, if any."""

    latest, _view_id, _admission_seq, _progress_seq, _strict = _workspace_run_intent_state(events)
    return latest


def current_workspace_agent_view_id(events: Iterable[Event]) -> str | None:
    """Return the latest model-view generation bound to the newest intent."""

    _latest, view_id, _admission_seq, _progress_seq, _strict = _workspace_run_intent_state(events)
    return view_id


def current_workspace_agent_view_seq(events: Iterable[Event]) -> int | None:
    """Return the canonical sequence of the current strict view admission."""

    _latest, view_id, admission_seq, _progress_seq, _strict = _workspace_run_intent_state(events)
    return admission_seq if view_id is not None and admission_seq >= 1 else None


def event_matches_current_workspace_view(events: Iterable[Event], event: Event) -> bool:
    """Whether a persisted control target belongs to the latest strict view."""

    materialized = list(events)
    latest, view_id, _admission_seq, _progress_seq, strict = _workspace_run_intent_state(
        materialized
    )
    if latest is None or not strict:
        return True
    return view_id is not None and event.agent_view_id == view_id


def workspace_run_intent_admission_required(events: Iterable[Event]) -> bool:
    """Whether a fresh model-view boundary must acknowledge the newest intent."""

    latest, view_id, admission_seq, _progress_seq, strict = _workspace_run_intent_state(events)
    if latest is None:
        return False
    return view_id is None if strict else admission_seq <= (latest.seq or -1)


def pending_workspace_run_intent(events: Iterable[Event]) -> WorkspaceMutationEvent | None:
    """Return an intent not yet followed by an admitted view and real progress.

    Output from an older in-flight model request may land after a new user turn.
    It cannot consume that turn: admission must occur after the intent, at a
    model-view boundary that includes it, and progress must occur after admission.
    """

    latest, view_id, admission_seq, progress_seq, strict = _workspace_run_intent_state(events)
    if latest is None or (strict and view_id is None):
        return latest
    if not strict and admission_seq <= (latest.seq or -1):
        return latest
    return latest if progress_seq <= admission_seq else None


def workspace_terminal_matches_current_run(
    events_before_terminal: Iterable[Event],
    terminal: StatusEvent,
) -> bool:
    """Whether *terminal* can complete the newest admitted workspace run.

    In strict v1 histories, the terminal must come from the exact latest view
    generation and that generation must have made real progress. Historical
    pre-v1 histories retain their sequence-only behavior.
    """

    events = list(events_before_terminal)
    if terminal.host_mutation_id is not None:
        completed_seq = max(
            (
                event.seq
                for event in events
                if isinstance(event, StatusEvent)
                and event.status is ConversationStatus.FINISHED
                and type(event.seq) is int
            ),
            default=-1,
        )
        if completed_seq < 1:
            return False
        later_mutations = [
            event
            for event in events
            if isinstance(event, WorkspaceMutationEvent)
            and type(event.seq) is int
            and event.seq > completed_seq
            and not event.operation.startswith("agent.")
        ]
        if not later_mutations:
            return False
        latest_mutation = max(later_mutations, key=lambda event: event.seq or -1)
        return terminal.agent_view_id is None and terminal.host_mutation_id == latest_mutation.id

    latest, view_id, admission_seq, progress_seq, strict = _workspace_run_intent_state(events)
    if latest is None:
        return terminal.agent_view_id is None
    if not strict:
        return progress_seq > admission_seq > (latest.seq or -1)
    if view_id is None:
        return False
    return terminal.agent_view_id == view_id and progress_seq > admission_seq > (latest.seq or -1)


class AgentViewProjection:
    """Incremental semantic projection for strict run/view histories.

    Stores and evidence retain every raw event. Model, state, and transport
    consumers share this reducer so reconnect replay cannot invent a different
    winner from the server's authoritative projection.
    """

    def __init__(self) -> None:
        self._latest_intent_id: str | None = None
        self._current_view_id: str | None = None
        self._strict = False
        self._protocol_v1_seen = False

    def accept(self, event: Event) -> bool:
        """Advance through *event* and return whether it is semantically visible."""

        if isinstance(event, WorkspaceMutationEvent):
            if event.operation.startswith("agent.run-intent."):
                self._protocol_v1_seen = self._protocol_v1_seen or event.run_protocol_version == 1
                self._latest_intent_id = event.id
                self._current_view_id = None
                self._strict = self._protocol_v1_seen
            elif (
                self._latest_intent_id is not None
                and event.operation == "agent.view-admitted"
                and event.run_intent_id == self._latest_intent_id
                and event.run_protocol_version == 1
                and event.agent_view_id is not None
            ):
                self._protocol_v1_seen = True
                self._current_view_id = event.agent_view_id
                self._strict = True
        status_run_intent_id = event.run_intent_id if isinstance(event, StatusEvent) else None
        run_owned_status = status_run_intent_id is not None
        stale = self._strict and (
            (event.agent_view_id is not None and event.agent_view_id != self._current_view_id)
            or (
                run_owned_status
                and (
                    status_run_intent_id != self._latest_intent_id
                    or self._current_view_id is not None
                )
            )
            or (
                event.agent_view_id is None
                and not run_owned_status
                and (
                    event.source is EventSource.AGENT
                    or isinstance(event, (ObservationEvent, AgentErrorEvent, ErrorEvent))
                    or (
                        isinstance(event, StatusEvent)
                        and event.host_mutation_id is None
                        and event.status
                        not in {
                            # These are explicit host/control transitions. Run
                            # conclusions and parked gates must carry a view or
                            # pre-admission intent authority under strict v1.
                            ConversationStatus.RUNNING,
                            ConversationStatus.IDLE,
                        }
                    )
                )
            )
        )
        return not stale


def agent_view_consistent_events(events: Iterable[Event]) -> list[Event]:
    """Keep audit history while quarantining output that lost a later view race."""

    projection = AgentViewProjection()
    return [event for event in events if projection.accept(event)]


def _strict_workspace_actions_are_closed(events: Iterable[Event]) -> bool:
    """Prove every post-admission action has a generation-matched outcome."""

    materialized = sorted(
        events,
        key=lambda event: event.seq if type(event.seq) is int else -1,
    )
    _intent, view_id, admission_seq, _progress_seq, strict = _workspace_run_intent_state(
        materialized
    )
    if not strict or view_id is None:
        return True
    actions = {
        event.id: event
        for event in materialized
        if isinstance(event, ActionEvent) and type(event.seq) is int
    }
    open_actions: set[str] = set()
    for event in materialized:
        if type(event.seq) is not int or event.seq <= admission_seq:
            continue
        if isinstance(event, ActionEvent):
            open_actions.add(event.id)
            continue
        if not isinstance(event, (ObservationEvent, AgentErrorEvent)) or event.action_id is None:
            continue
        action = actions.get(event.action_id)
        if (
            action is not None
            and isinstance(event, AgentErrorEvent)
            and event.error == "execution_superseded"
            and event.agent_view_id == action.agent_view_id
        ):
            open_actions.discard(action.id)
            continue
        if action is None or action.id not in open_actions:
            return False
        if action.agent_view_id == view_id and event.agent_view_id == view_id:
            open_actions.discard(action.id)
            continue
        return False
    return not open_actions


def derive_final_workspace_fence(events: Iterable[Event]) -> tuple[int, int | None]:
    """Derive the only event-log fence a final workspace seal may claim.

    The terminal is the latest persisted status event, not merely the latest
    ``FINISHED`` found while scanning backwards.  This distinction prevents an
    older completion from blessing bytes after a later RUNNING/IDLE transition.
    Action requests and their observation/error outcomes form the effect
    envelope: any such event at or after the terminal means the workspace cut
    cannot be attributed to that completion.

    The helper deliberately accepts the generic :class:`Event` iterable used by
    both lifecycle code and stores.  It fails closed on unsequenced semantic
    events so callers can never substitute list position for the durable store's
    canonical sequence.
    """

    materialized = list(events)
    latest_status: StatusEvent | None = None
    latest_status_seq: int | None = None
    latest_effect_seq: int | None = None

    for event in materialized:
        if not isinstance(
            event,
            (
                StatusEvent,
                ActionEvent,
                ObservationEvent,
                AgentErrorEvent,
                WorkspaceMutationEvent,
            ),
        ):
            continue
        seq = event.seq
        if type(seq) is not int or seq < 1:
            raise ValueError("final workspace fence requires canonical persisted event sequences")
        if isinstance(event, StatusEvent):
            if latest_status_seq is None or seq > latest_status_seq:
                latest_status = event
                latest_status_seq = seq
        elif latest_effect_seq is None or seq > latest_effect_seq:
            latest_effect_seq = seq

    if latest_status is None or latest_status_seq is None:
        raise ValueError("final workspace fence requires a persisted status event")
    if (
        latest_status.source is not EventSource.SYSTEM
        or latest_status.status is not ConversationStatus.FINISHED
    ):
        raise ValueError("latest status event must be a system FINISHED status")
    before_terminal = [
        event for event in materialized if type(event.seq) is int and event.seq < latest_status_seq
    ]
    if not workspace_terminal_matches_current_run(before_terminal, latest_status):
        raise ValueError("latest FINISHED status is not attributable to the current agent run")
    if not _strict_workspace_actions_are_closed(before_terminal):
        raise ValueError("strict workspace actions are not generation-matched and closed")
    if latest_effect_seq is not None and latest_effect_seq >= latest_status_seq:
        raise ValueError("latest effect event must precede the terminal FINISHED status")
    return latest_status_seq, latest_effect_seq


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
