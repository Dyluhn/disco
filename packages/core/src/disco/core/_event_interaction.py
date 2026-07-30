"""Interaction events — the agent/user/tool dialogue surface.

These models carry message/action/observation records, both error envelopes,
and condensation tombstones. User-facing proposals and durable outputs live
in ``_event_outputs``; schedule, question, context, and verifier audit records
live in ``_event_audit``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from ._event_render import _OBS_SNIP_CHARS as _OBS_SNIP_CHARS
from ._event_render import _OBS_SNIP_HEAD as _OBS_SNIP_HEAD
from ._event_render import _OBS_SNIP_TAIL as _OBS_SNIP_TAIL
from ._event_render import (
    _execution_receipt_trailer,
    _snip_args,
    obs_snip_override,
    snip_content,
)
from ._event_types import (
    ActionProfile,
    BaseEvent,
    EffectReceipt,
    EventKind,
    EventSource,
    LLMConvertible,
    LLMMessage,
    SecurityRisk,
    ToolCall,
    ToolResult,
    VerificationRequirementsDirective,
)


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


class ErrorEvent(BaseEvent):
    """A conversation-level (fatal-ish) error, e.g. MaxIterationsReached.
    NOT LLMConvertible."""

    kind: Literal[EventKind.ERROR] = EventKind.ERROR
    source: EventSource = EventSource.SYSTEM
    code: str
    detail: str


__all__ = [
    "ActionEvent",
    "AgentErrorEvent",
    "CondensationEvent",
    "ErrorEvent",
    "MessageEvent",
    "ObservationEvent",
]
