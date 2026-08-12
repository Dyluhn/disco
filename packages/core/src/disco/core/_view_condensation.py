"""View condensation — Condenser protocol and implementations.

Extracted from ``view.py`` so the condensation logic stays under the module
logical-LOC limit. These classes own the condensation strategy (when/how much
to summarize) and the pointer-manifest hard-reset path.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

from ._view_plan import (
    _SUMMARY_REPAIR_INSTRUCTION,
    _fallback_summary,
    summary_rejection_reason,
)
from .events import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    Event,
    EventSource,
    LLMConvertible,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
)

if TYPE_CHECKING:
    from .view import View

_LOG = logging.getLogger("disco.view")


class CondensationRequest(BaseModel):
    soft: bool  # soft = maintain a bound; hard = must condense now
    reason: Literal["tokens", "events", "request", "hard_reset"]


class Summarizer(Protocol):
    """Produces summary text for a span of messages."""

    async def summarize(self, messages: list[LLMMessage]) -> str: ...


def _build_pointer_manifest(span: list[Event], artifact_paths: list[str]) -> str:
    """C16 — pointer-only summary used by the hard_reset path."""
    start_seq = span[0].seq
    end_seq = span[-1].seq
    n = len(span)
    deliverables: list[str] = []
    spills: list[str] = []
    memory: list[str] = []
    other: list[str] = []
    for p in artifact_paths:
        bn = os.path.basename(p)
        if bn.startswith(".disco-spill-"):
            spills.append(p)
        elif p.endswith(".pmx/MEMORY.md") or bn == "MEMORY.md":
            memory.append(p)
        else:
            deliverables.append(p)
    lines: list[str] = []
    lines.append(
        f"[Hard reset — pointer-only flush; span seq {start_seq}–{end_seq} "
        f"({n} events) DROPPED from the live view.]"
    )
    lines.append(
        "The dropped content is NOT summarized here. It is fully recoverable "
        "from on-disk artifacts in your workspace. Use file_read (or grep) to "
        "retrieve the original content when you need it — the pointer list "
        "below IS the manifest, and the bytes are on disk RIGHT NOW. Do NOT "
        "trust your prose memory of the dropped span."
    )
    if deliverables:
        lines.append("")
        lines.append("Deliverables (files the agent wrote this run):")
        for p in deliverables:
            lines.append(f"  • {p}")
    if spills:
        lines.append("")
        lines.append("Spill logs (shell-output overflow; head/tail preserved):")
        for p in spills:
            lines.append(f"  • {p}")
    if memory:
        lines.append("")
        lines.append("Standing memory (recorded this run):")
        for p in memory:
            lines.append(f"  • {p}")
    if other:
        lines.append("")
        lines.append("Other on-disk artifacts (uncategorized):")
        for p in other:
            lines.append(f"  • {p}")
    return "\n".join(lines)


class Condenser(Protocol):
    """Decides whether/how to condense. The loop calls should_condense() (sync,
    pure over the View) then awaits condense() (async); the strategy is
    [INTERIOR] and swappable. condense() returns at most one CondensationEvent to
    append, or None (no-op). No event is ever deleted."""

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None: ...

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None: ...


class NoOpCondenser:
    """Phase 0 placeholder satisfying the `Condenser` protocol: never condenses."""

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        return None

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None:
        return None


def _compute_turn_starts(live: list[Event]) -> list[int]:
    """C10 — compute turn-start indices for the keep-recent boundary."""
    last_action: ActionEvent | None = None
    turn_starts: list[int] = [0]
    for i in range(1, len(live)):
        e = live[i]
        if isinstance(e, ActionEvent):
            turn_starts.append(i)
            last_action = e
        elif isinstance(e, (ObservationEvent, AgentErrorEvent)):
            if last_action is not None and e.action_id == last_action.id:
                continue
            turn_starts.append(i)
            last_action = None
        else:
            turn_starts.append(i)
            last_action = None
    return turn_starts


def _compute_condense_span(
    live: list[Event],
    keep_head: int,
    keep_recent: int,
    min_forget: int,
) -> list[Event] | None:
    """Compute the forgettable span, or None if there's nothing worth forgetting."""
    turn_starts = _compute_turn_starts(live)
    n_turns = len(turn_starts)
    if len(live) < keep_head + min_forget:
        return None
    if n_turns <= keep_recent:
        return None
    tail_start = turn_starts[n_turns - keep_recent]
    if tail_start <= keep_head:
        return None
    span = live[keep_head:tail_start]
    if len(span) < min_forget:
        return None
    return span


def _is_cumulative_model_summary(event: CondensationEvent) -> bool:
    """Whether one tombstone is the cumulative LLM-authored history summary."""
    return event.reason == "tokens" and not event.summary.startswith("[microcompacted:")


def _summary_input_messages(events: list[Event], span: list[Event]) -> list[LLMMessage]:
    """Build the bounded delta given to the summarizer.

    Keep the first user task as an anchor, the latest prior cumulative summary,
    and only the newly forgotten span.  Recent live history, workspace snapshots,
    and older superseded summaries are intentionally excluded.
    """
    start_seq = span[0].seq or 0
    messages: list[LLMMessage] = []
    anchor = next(
        (
            event
            for event in events
            if isinstance(event, MessageEvent)
            and event.source == EventSource.USER
            and (event.seq or 0) < start_seq
        ),
        None,
    )
    if anchor is not None:
        messages.append(anchor.to_llm_message())
    prior = [
        event
        for event in events
        if isinstance(event, CondensationEvent)
        and _is_cumulative_model_summary(event)
        and event.forgotten_end_seq < start_seq
    ]
    if prior:
        latest = max(prior, key=lambda event: event.seq or 0)
        messages.append(LLMMessage(role=latest.summary_role, content=latest.summary))
    messages.extend(event.to_llm_message() for event in span if isinstance(event, LLMConvertible))
    return messages


async def _summarize_with_repair(
    summarizer: Summarizer,
    messages: list[LLMMessage],
    start_seq: int,
    end_seq: int,
) -> str | None:
    """Call the summarizer, with one bounded repair on protocol-markup rejection."""
    summary = await summarizer.summarize(messages)
    if not summary.strip():
        return None
    rejection = summary_rejection_reason(summary)
    if rejection is None:
        return summary
    _LOG.warning("summarizer output rejected (%s); requesting one repair", rejection)
    repaired = await summarizer.summarize(
        [*messages, LLMMessage(role="user", content=_SUMMARY_REPAIR_INSTRUCTION)]
    )
    if repaired.strip() and summary_rejection_reason(repaired) is None:
        return repaired
    _LOG.warning("summarizer repair also rejected; using truthful fallback")
    return _fallback_summary(start_seq, end_seq)


class LLMSummarizingCondenser:
    """[INTERIOR] The real condenser (§5.2). When the View grows past a token bound,
    summarize the OLDEST forgettable span into ONE ``CondensationEvent`` tombstone."""

    def __init__(
        self,
        *,
        context_window: int | None = None,
        soft_frac: float = 0.65,
        hard_frac: float = 0.80,
        max_tokens: int | None = None,
        hard_max_tokens: int | None = None,
        keep_head: int = 1,
        keep_recent: int = 6,
        min_forget: int = 2,
        budget_tokens: int = 24_000,
        hard_budget_tokens: int = 32_000,
    ) -> None:
        soft_ceiling = budget_tokens * 4
        hard_ceiling = hard_budget_tokens * 4
        if context_window is not None:
            window_soft = int(context_window * soft_frac)
            window_hard = int(context_window * hard_frac)
            derived_soft = (
                window_soft if window_soft <= budget_tokens else min(window_soft, soft_ceiling)
            )
            derived_hard = (
                window_hard if window_hard <= hard_budget_tokens else min(window_hard, hard_ceiling)
            )
        else:
            derived_soft, derived_hard = budget_tokens, hard_budget_tokens
        self._max = max_tokens if max_tokens is not None else derived_soft
        self._hard = hard_max_tokens if hard_max_tokens is not None else derived_hard
        self._keep_head = keep_head
        self._keep_recent = keep_recent
        self._min_forget = min_forget

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        if token_count is None:
            return None
        if token_count >= self._hard:
            return CondensationRequest(soft=False, reason="tokens")
        if token_count >= self._max:
            return CondensationRequest(soft=True, reason="tokens")
        return None

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None:
        forgotten = [
            (e.forgotten_start_seq, e.forgotten_end_seq)
            for e in events
            if isinstance(e, CondensationEvent)
        ]

        def is_forgotten(seq: int | None) -> bool:
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        live: list[Event] = [
            e
            for e in events
            if isinstance(e, LLMConvertible) and e.seq is not None and not is_forgotten(e.seq)
        ]

        span = _compute_condense_span(live, self._keep_head, self._keep_recent, self._min_forget)
        if span is None:
            return None

        start_seq, end_seq = span[0].seq, span[-1].seq
        if start_seq is None or end_seq is None:
            return None

        if reason == "hard_reset":
            if not artifact_paths:
                return None
            return CondensationEvent(
                forgotten_start_seq=start_seq,
                forgotten_end_seq=end_seq,
                summary=_build_pointer_manifest(span, list(artifact_paths)),
                summary_role="user",
                reason="hard_reset",
            )

        summary = await _summarize_with_repair(
            summarizer,
            _summary_input_messages(events, span),
            start_seq,
            end_seq,
        )
        if summary is None:
            return None
        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="tokens",
        )
