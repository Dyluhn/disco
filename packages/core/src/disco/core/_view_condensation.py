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
    VerifierVerdictEvent,
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


def _summary_anchor(events: list[Event], start_seq: int) -> LLMMessage | None:
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
    return _summary_event_message(anchor) if anchor is not None else None


def _prior_summary_message(events: list[Event], start_seq: int) -> LLMMessage | None:
    latest = max(
        (
            event
            for event in events
            if isinstance(event, CondensationEvent)
            and _is_cumulative_model_summary(event)
            and event.forgotten_end_seq < start_seq
        ),
        key=lambda event: event.seq or 0,
        default=None,
    )
    if latest is None:
        return None
    return LLMMessage(
        role=latest.summary_role,
        content=f"[prior-summary through seq={latest.forgotten_end_seq}]\n{latest.summary}",
    )


def _summary_probe_call_ids(events: list[Event]) -> set[str]:
    return {
        event.tool_call.call_id
        for event in events
        if isinstance(event, ActionEvent) and event.meta.get("verify_probe") is True
    }


def _summary_span_messages(
    events: list[Event], start_seq: int, end_seq: int
) -> list[LLMMessage]:
    probe_ids = _summary_probe_call_ids(events)
    messages: list[LLMMessage] = []
    for event in events:
        seq = event.seq
        if seq is None or not start_seq <= seq <= end_seq:
            continue
        message = _summary_event_message(event, host_probe_call_ids=probe_ids)
        if message is not None:
            messages.append(message)
    return messages


def _summary_input_messages(events: list[Event], span: list[Event]) -> list[LLMMessage]:
    """Build the anchored, provenance-bearing delta given to the summarizer."""

    start_seq = span[0].seq or 0
    end_seq = span[-1].seq or start_seq
    messages = [
        message
        for message in (
            _summary_anchor(events, start_seq),
            _prior_summary_message(events, start_seq),
        )
        if message is not None
    ]
    messages.extend(_summary_span_messages(events, start_seq, end_seq))
    return messages


def _summary_event_prefix(event: Event) -> str:
    kind = getattr(event.kind, "value", str(event.kind))
    source = getattr(event.source, "value", str(event.source))
    return f"[seq={event.seq or 0} kind={kind} source={source}]"


def _host_probe_summary_message(
    event: Event, prefix: str, probe_ids: set[str]
) -> LLMMessage | None:
    if isinstance(event, ActionEvent) and event.meta.get("verify_probe") is True:
        detail = "advisory finish probe started."
    elif isinstance(event, ObservationEvent) and event.tool_result.call_id in probe_ids:
        state = "PASS" if event.tool_result.success else "FAIL"
        detail = f"advisory finish probe {state}."
    elif isinstance(event, AgentErrorEvent) and event.tool_call_id in probe_ids:
        detail = "advisory finish probe did not run or failed."
    else:
        return None
    return LLMMessage(role="user", content=f"{prefix}\nHOST ACTIVITY: {detail}")


def _verifier_summary_message(event: Event, prefix: str) -> LLMMessage | None:
    if not isinstance(event, VerifierVerdictEvent):
        return None
    state = "PASS" if event.verified else "FAIL"
    detail = (event.detail or "").strip()[:500]
    suffix = f"; detail={detail}" if detail else ""
    return LLMMessage(
        role="user",
        content=(
            f"{prefix}\nHOST VERIFICATION {state}; artifact={event.artifact_path!r}; "
            f"verdict={event.verdict!r}; typed_result="
            f"{'present' if event.verification_result is not None else 'absent'}{suffix}"
        ),
    )


def _summary_event_message(
    event: Event, *, host_probe_call_ids: set[str] | None = None
) -> LLMMessage | None:
    """Render bounded event provenance for chronological, authority-aware summaries."""

    prefix = _summary_event_prefix(event)
    probe_ids = host_probe_call_ids or set()
    host_message = _host_probe_summary_message(event, prefix, probe_ids)
    if host_message is not None:
        return host_message
    if isinstance(event, LLMConvertible):
        message = event.to_llm_message()
        return message.model_copy(update={"content": f"{prefix}\n{message.content}"})
    return _verifier_summary_message(event, prefix)


def _bounded_summary_span(span: list[Event], *, max_chars: int, min_events: int) -> list[Event]:
    """Bound one summarizer request at event boundaries without splitting a tool pair."""

    selected: list[Event] = []
    used = 0
    for event in span:
        message = event.to_llm_message() if isinstance(event, LLMConvertible) else None
        size = len(message.content) + len(str(message.tool_calls or ())) if message else 0
        pair_open = bool(selected and isinstance(selected[-1], ActionEvent))
        if selected and used + size > max_chars and len(selected) >= min_events and not pair_open:
            break
        selected.append(event)
        used += size
    return selected


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
        summary_input_chars: int = 384_000,
        min_batch_tokens: int | None = None,
    ) -> None:
        if context_window is not None:
            derived_soft = int(context_window * soft_frac)
            derived_hard = int(context_window * hard_frac)
        else:
            derived_soft, derived_hard = budget_tokens, hard_budget_tokens
        self._max = max_tokens if max_tokens is not None else derived_soft
        self._hard = hard_max_tokens if hard_max_tokens is not None else derived_hard
        # A condensation waits until this much forgettable history has aged out of the
        # recent window, unless it ends hard pressure at once; otherwise a view whose fixed
        # part (system prompt, tool schemas, workspace snapshot, the kept turns) sits above
        # the line would pay one summarizer call per turn to forget two events. Derived
        # from the soft→hard band so it scales with the window: ~5.9 k tokens at 131 k,
        # ~360 at 8 k (small windows need frequent condensation), 0 when the band is 0.
        band = max(0, self._hard - self._max)
        self.min_batch_tokens = min_batch_tokens if min_batch_tokens is not None else int(band * 0.3)
        # Above this estimate a condensation is never deferred, whatever the batch: the
        # drift batching allows stops two thirds of a band above the hard line (0.90 of
        # the window at the default 0.65/0.80 fractions); no band → the hard line itself.
        self.ceiling_tokens = self._hard + int(band * 2 / 3)
        self._keep_head = keep_head
        self._keep_recent = keep_recent
        self._min_forget = min_forget
        self._summary_input_chars = max(1, summary_input_chars)

    @property
    def hard_max_tokens(self) -> int:
        """The estimate above which condensation is mandatory — when it can help."""
        return self._hard

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        if token_count is None:
            return None
        if token_count >= self._hard:
            return CondensationRequest(soft=False, reason="tokens")
        if token_count >= self._max:
            return CondensationRequest(soft=True, reason="tokens")
        return None

    def _forgettable_span(self, events: list[Event]) -> list[Event] | None:
        """The oldest forgettable span under the keep-head / keep-recent policy."""
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
        return _bounded_summary_span(
            span,
            max_chars=self._summary_input_chars,
            min_events=self._min_forget,
        )

    def forgettable_tokens(self, events: list[Event]) -> int:
        """Estimated tokens the next condensation would remove from the view (~4 chars/token)."""
        span = self._forgettable_span(events) or []
        chars = 0
        for event in span:
            message = event.to_llm_message() if isinstance(event, LLMConvertible) else None
            if message is not None:
                chars += len(message.content or "") + len(str(message.tool_calls or ()))
        return chars // 4

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None:
        span = self._forgettable_span(events)
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
