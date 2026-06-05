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

    async def summarize(self, messages: list[LLMMessage]) -> str: ...


class Condenser(Protocol):
    """Decides whether/how to condense. The loop calls should_condense() (sync,
    pure over the View) then awaits condense() (async); the strategy is
    [INTERIOR] and swappable. condense() returns at most one CondensationEvent to
    append, or None (no-op). No event is ever deleted.

    Async rationale (event-state-contract v1.2 §5.2): condense() must be async
    because the only correct summarizer makes an async router call, invoked from
    the loop's running asyncio loop. should_condense stays sync (pure)."""

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None: ...

    async def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None: ...


class NoOpCondenser:
    """Phase 0 placeholder satisfying the `Condenser` protocol: never condenses.

    This lets the View/loop wire up against the real interface now; the
    `LLMSummarizingCondenser` is the real strategy used on the Agent surface.
    """

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        return None

    async def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None:
        return None


class LLMSummarizingCondenser:
    """[INTERIOR] The real condenser (§5.2). When the View grows past a token bound,
    summarize the OLDEST forgettable span — keeping an anchoring HEAD (the initial
    instruction) and a RECENT TAIL untouched — into ONE `CondensationEvent` tombstone.
    `View.of` places the summary at the span's chronological position, so the loop
    keeps going coherently instead of overflowing the context window.

    - Triggers: SOFT at `max_tokens` (maintain the bound), HARD at `hard_max_tokens`.
    - Guards: `keep_head` + `keep_recent` events are never forgotten; `min_forget`
      ensures each condensation makes real PROGRESS (no churn on tiny spans).

    `should_condense` is sync/pure over the View's token estimate; `condense` awaits the
    SUMMARIZER-role model (a cheap, separate model — BoD §7.3/§15) and never deletes an
    event (it appends a tombstone). It also self-computes its span, so the loop's
    hard-reset path (`_hard_reset`, which calls `condense` directly) works too.
    """

    def __init__(
        self,
        *,
        max_tokens: int = 24_000,
        hard_max_tokens: int = 32_000,
        keep_head: int = 1,
        keep_recent: int = 6,
        min_forget: int = 2,
    ) -> None:
        self._max = max_tokens
        self._hard = hard_max_tokens
        self._keep_head = keep_head
        self._keep_recent = keep_recent
        self._min_forget = min_forget

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None:
        if token_count is None:
            return None
        if token_count >= self._hard:
            return CondensationRequest(soft=False, reason="tokens")  # must condense now
        if token_count >= self._max:
            return CondensationRequest(soft=True, reason="tokens")  # maintain the bound
        return None

    async def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None:
        forgotten = [
            (e.forgotten_start_seq, e.forgotten_end_seq)
            for e in events
            if isinstance(e, CondensationEvent)
        ]

        def is_forgotten(seq: int | None) -> bool:
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        # The still-live, LLM-visible events in seq order (already-forgotten dropped).
        live = [
            e
            for e in events
            if isinstance(e, LLMConvertible) and e.seq is not None and not is_forgotten(e.seq)
        ]
        # Need an anchoring head + a recent tail AND at least `min_forget` in between —
        # else there's nothing worth forgetting yet (the minimum-progress guard).
        if len(live) < self._keep_head + self._keep_recent + self._min_forget:
            return None
        span = live[self._keep_head : len(live) - self._keep_recent]
        if len(span) < self._min_forget:
            return None

        start_seq, end_seq = span[0].seq, span[-1].seq
        if start_seq is None or end_seq is None:  # filtered above; assertion for the type
            return None
        summary = await summarizer.summarize([e.to_llm_message() for e in span])
        if not summary.strip():
            return None  # an empty summary would forget context for nothing
        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="tokens",
        )
