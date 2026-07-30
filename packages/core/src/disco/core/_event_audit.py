"""Audit and UI-control events.

These models carry verifier records, schedule lifecycle, structured questions,
and context-resolution/summary audit markers. They are UI or audit bookkeeping,
never model-rendered content.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from ._event_types import (
    BaseEvent,
    ClarifyQuestionItem,
    EventKind,
    EventSource,
    HostVerificationResult,
    PreviewSelectionIdentity,
    QuestionsV2Item,
    VerificationArtifactIdentity,
    VerificationClaimStatus,
    VerificationExecutionIdentity,
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
    target_id: str = ""
    run_intent_id: str | None = None
    run_identity: str | None = None
    delivery_shape: str = ""
    delivery_entry_reference: str = ""
    check_id: str = ""
    receipt_kind: str = ""
    issuer_id: str = ""
    operation: str = ""
    delegated_issuer_ids: frozenset[str] = frozenset()
    verification_contract_digest: str | None = None
    deliverable_event_id: str | None = None
    execution_identity: VerificationExecutionIdentity | None = None
    artifact_identity: VerificationArtifactIdentity | None = None
    preview_selection: PreviewSelectionIdentity | None = None
    workspace_revision: int = 0
    workspace_generation: str = ""
    workspace_epoch: int | None = None
    observed_after_seq: int = 0


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
    target_id: str = ""
    check_id: str = ""
    receipt_kind: str = ""
    verification_contract_digest: str | None = None
    requested_by_event_id: str | None = None

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
        if self.target_id != result.target_id:
            raise ValueError("target identity does not match typed verification result")
        if self.check_id != result.check_id or self.receipt_kind != result.receipt_kind:
            raise ValueError("verifier check identity does not match typed verification result")
        if self.verification_contract_digest != result.verification_contract_digest:
            raise ValueError("verification contract digest does not match typed result")
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


class ClarifyEvent(BaseEvent):
    """Pre-plan clarification gate (RP-13).

    The event carries multiple typed questions and halts at
    AWAITING_USER_QUESTION. It is not LLMConvertible; answers return as a user
    MessageEvent.
    """

    kind: Literal[EventKind.CLARIFY] = EventKind.CLARIFY
    source: EventSource = EventSource.AGENT
    question: str
    items: list[ClarifyQuestionItem]


class QuestionsV2Event(BaseEvent):
    """Structured pre-plan intake gate (§K)."""

    kind: Literal[EventKind.QUESTIONS_V2] = EventKind.QUESTIONS_V2
    source: EventSource = EventSource.AGENT
    question: str
    items: list[QuestionsV2Item]


class ContextResolvedEvent(BaseEvent):
    """A deferred range-resolution marker that does not itself forget content."""

    kind: Literal[EventKind.CONTEXT_RESOLVED] = EventKind.CONTEXT_RESOLVED
    source: EventSource = EventSource.AGENT
    range_id: str = Field(default_factory=lambda: f"cxr_{uuid.uuid4().hex}")
    forgotten_start_seq: int
    forgotten_end_seq: int
    reason: str = "resolved"
    summary_ref_path: str | None = None


class ContextSummaryEvent(BaseEvent):
    """Records the durable summary that permits later range compaction."""

    kind: Literal[EventKind.CONTEXT_SUMMARY] = EventKind.CONTEXT_SUMMARY
    source: EventSource = EventSource.SYSTEM
    range_id: str
    rel_path: str
    summary: str
    artifact_kind: str = "summary"


__all__ = [
    "ClarifyEvent",
    "ContextResolvedEvent",
    "ContextSummaryEvent",
    "QuestionsV2Event",
    "ScheduleEvent",
    "ScheduleRunEvent",
    "VerifierShadowEvent",
    "VerifierStartedEvent",
    "VerifierVerdictEvent",
]
