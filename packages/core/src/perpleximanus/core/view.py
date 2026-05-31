"""Memory: View + Condensation — event-state-contract.md §5.

The tension: an append-only log grows without bound, but the LLM context window
is finite. Resolution (BoD §7.3): never delete; to "forget", append a
`CondensationEvent` tombstone recording a span to drop and a summary to insert.
The `View` computes, at read time, "what the LLM currently sees" by applying
those tombstones. The agent loop and LLM router consume `View.messages` — never
the raw log.

The View's behavior and tombstone semantics are [CONTRACT]. The *condensation
strategy* (when/how much to summarize) is a swappable [INTERIOR] `Condenser`;
Phase 0 ships only the no-op condenser (the real LLMSummarizingCondenser is a
Phase 1 deliverable, BoD §22).
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

from .events import CondensationEvent, Event, LLMConvertible, LLMMessage


class View(BaseModel):
    """The materialized "what the LLM sees right now", computed from the raw
    event log by applying condensation tombstones.

    [CONTRACT] View.of(events) is pure: same events -> same messages, every
    time. Computing it has no side effects and never appends.
    """

    messages: list[LLMMessage]
    # seqs of events currently visible (post-condensation), for diagnostics.
    visible_seqs: list[int]
    total_events: int
    forgotten_count: int

    @classmethod
    def of(cls, events: list[Event]) -> View:
        # 1. Collect forgotten seq ranges + a map of where each summary belongs.
        #    NOTE: we intentionally diverge from the contract's *illustrative*
        #    body, which emits the summary when it reaches the CondensationEvent
        #    in append order. Because a tombstone is appended *after* the span it
        #    forgets, that would place an old summary after recent messages. The
        #    [CONTRACT] behavior (§7.3) is "replace the first half with a single
        #    summary, leave the back half untouched" — i.e. the summary takes the
        #    *position of the forgotten span*. So we key summaries by
        #    forgotten_start_seq and emit them in chronological place.
        forgotten: list[tuple[int, int]] = []
        summary_at_start: dict[int, CondensationEvent] = {}
        for e in events:
            if isinstance(e, CondensationEvent):
                forgotten.append((e.forgotten_start_seq, e.forgotten_end_seq))
                # First tombstone for a given start_seq wins its slot.
                summary_at_start.setdefault(e.forgotten_start_seq, e)

        def is_forgotten(seq: int | None) -> bool:
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        # 2. Walk events in seq order. When we reach the start of a forgotten
        #    span, emit its summary (once) in place; drop forgotten and
        #    non-LLMConvertible events; keep the rest.
        msgs: list[LLMMessage] = []
        visible: list[int] = []
        emitted_summary_for: set[int] = set()
        for e in events:
            # Emit a span's summary at the chronological position it replaces.
            if e.seq is not None and e.seq in summary_at_start:
                key = e.seq
                if key not in emitted_summary_for:
                    tomb = summary_at_start[key]
                    msgs.append(LLMMessage(role=tomb.summary_role, content=tomb.summary))
                    emitted_summary_for.add(key)

            if isinstance(e, CondensationEvent):
                continue  # bookkeeping; its summary is emitted in-place above
            if not isinstance(e, LLMConvertible):
                continue  # status/error: never shown to the LLM
            if is_forgotten(e.seq):
                continue  # forgotten by a tombstone
            msgs.append(e.to_llm_message())
            if e.seq is not None:
                visible.append(e.seq)

        forgotten_n = sum(b - a + 1 for a, b in forgotten)
        return cls(
            messages=msgs,
            visible_seqs=visible,
            total_events=len(events),
            forgotten_count=forgotten_n,
        )


# ---- Condenser / Summarizer seams (§5.2) ------------------------------------


class CondensationRequest(BaseModel):
    soft: bool  # soft = maintain a bound; hard = must condense now
    reason: Literal["tokens", "events", "request", "hard_reset"]


class Summarizer(Protocol):
    """Produces summary text for a span of messages. [CONTRACT at the LLM
    boundary] — implemented by the LLM router using a CHEAP model, separate
    from the agent model (BoD §7.3, §15)."""

    def summarize(self, messages: list[LLMMessage]) -> str: ...


class Condenser(Protocol):
    """Decides whether/how to condense. The loop calls should_condense() then
    condense(); the strategy is [INTERIOR] and swappable. condense() returns at
    most one CondensationEvent to append, or None (no-op). No event is ever
    deleted."""

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None: ...

    def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None: ...


class NoOpCondenser:
    """Phase 0 placeholder satisfying the `Condenser` protocol: never condenses.

    This lets the View/loop wire up against the real interface now; the
    `LLMSummarizingCondenser` (first-half summarize, keep_first, minimum_progress
    guard, soft/hard triggers — §5.2) lands in Phase 1.
    """

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        return None

    def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None:
        return None
