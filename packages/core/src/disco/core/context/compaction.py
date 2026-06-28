"""CompactionPolicy + the agent-driven deferred-snip functions (CXT-3).

CompactionPolicy is the pure policy data (caps) shared by ContextPack.from_ledger
and later assemblers. CXT-3 adds the agent-intent layer: ``context_mark_resolved``
(a deferred snip mark), ``context_write_summary`` (the durable-summary record that
unlocks compaction), and ``context_compact_if_needed`` (converts eligible marks
into ordinary CondensationEvent tombstones — REUSING the engine's existing View.of
filtering + recover_span, so there is no second way to forget). All functions are
pure over event lists (return events to append); the loop wiring is CXT-7.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from ..events import (
    CondensationEvent,
    ContextResolvedEvent,
    ContextSummaryEvent,
    Event,
)
from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef
from .ledger import ResolvedContextRange
from .source_priority import SourceKind

# Kinds that anchor the run and must never be dropped to make room.
_NEVER_COMPACT: tuple[SourceKind, ...] = (
    SourceKind.GOAL,
    SourceKind.CONTRACT,
    SourceKind.VERIFIER_FAILURE,
    SourceKind.DIRECT_EDIT,
)


class CompactionPolicy(BaseModel):
    """Thresholds for omitting omittable context. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_history_chars: int = 20_000
    max_retained_refs: int = 50
    max_resource_refs: int = 30
    max_recoverable_refs: int = 30
    max_comments: int = 30
    never_compact: tuple[SourceKind, ...] = _NEVER_COMPACT

    @classmethod
    def default(cls) -> CompactionPolicy:
        return cls()

    def limit_for(self, kind: SourceKind) -> int | None:
        """The max number of items to retain for ``kind``.

        Returns ``None`` (unbounded) for never-compact kinds and for kinds that
        are not count-capped here; otherwise the configured cap.
        """
        if kind in self.never_compact:
            return None
        return {
            SourceKind.RESOURCE: self.max_resource_refs,
            SourceKind.RECOVERABLE_REF: self.max_recoverable_refs,
            SourceKind.COMMENT: self.max_comments,
        }.get(kind)


# --- CXT-3: agent-driven deferred snip ----------------------------------------


def context_mark_resolved(
    start_seq: int, end_seq: int, reason: str = "resolved", *, range_id: str | None = None
) -> ContextResolvedEvent:
    """The agent's DEFERRED snip mark over event seq range [start, end]. On its own
    it does NOT change the model view — it only records intent. Compaction is
    executed later by ``context_compact_if_needed`` once a durable summary exists."""
    if range_id is None:
        return ContextResolvedEvent(
            forgotten_start_seq=start_seq, forgotten_end_seq=end_seq, reason=reason
        )
    return ContextResolvedEvent(
        range_id=range_id, forgotten_start_seq=start_seq, forgotten_end_seq=end_seq, reason=reason
    )


def context_write_summary(range_id: str, rel_path: str, summary: str) -> ContextSummaryEvent:
    """Record that a durable summary was written for ``range_id``. The non-empty
    ``summary`` is the content that will replace the forgotten span, so it is the
    durability proof that unlocks compaction of that range."""
    return ContextSummaryEvent(range_id=range_id, rel_path=rel_path, summary=summary)


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def context_compact_if_needed(
    events: Sequence[Event],
    policy: CompactionPolicy,
    *,
    protected_seqs: frozenset[int] = frozenset(),
    pressure_chars: int | None = None,
) -> list[CondensationEvent]:
    """Convert eligible deferred snip marks into CondensationEvent tombstones.

    A pending ``ContextResolvedEvent`` is compacted ONLY IF all hold:
      1. a ``ContextSummaryEvent`` with NON-EMPTY summary exists for its range_id
         (the "never compact until durable state exists elsewhere" guard — the
         summary is the replacement content inlined into the tombstone);
      2. its [start, end] does not overlap any ``protected_seqs`` (unresolved
         failures are never compacted away);
      3. its range is not already covered by an existing CondensationEvent nor by
         a range emitted earlier in this pass (idempotent, no double-forget);
      4. pressure warrants it: ``pressure_chars`` is None (caller forces) OR
         exceeds ``policy.max_history_chars``.

    Returns the CondensationEvents to append (reason="request"); re-running after
    they are appended returns ``[]`` (they now count as already-forgotten).
    """
    if pressure_chars is not None and pressure_chars <= policy.max_history_chars:
        return []

    already: list[tuple[int, int]] = [
        (e.forgotten_start_seq, e.forgotten_end_seq)
        for e in events
        if isinstance(e, CondensationEvent)
    ]
    summaries: dict[str, str] = {
        e.range_id: e.summary
        for e in events
        if isinstance(e, ContextSummaryEvent) and e.summary.strip()
    }
    pending = [e for e in events if isinstance(e, ContextResolvedEvent)]

    out: list[CondensationEvent] = []
    emitted: list[tuple[int, int]] = []
    for ev in sorted(pending, key=lambda e: (e.forgotten_start_seq, e.range_id)):
        s, t = ev.forgotten_start_seq, ev.forgotten_end_seq
        summary = summaries.get(ev.range_id)
        if not summary:  # (1) durability precondition
            continue
        if any(s <= ps <= t for ps in protected_seqs):  # (2) protected seqs
            continue
        if any(_overlaps(s, t, a, b) for a, b in already):  # (3a) already forgotten
            continue
        if any(_overlaps(s, t, a, b) for a, b in emitted):  # (3b) overlap within this pass
            continue
        out.append(
            CondensationEvent(
                forgotten_start_seq=s, forgotten_end_seq=t, summary=summary, reason="request"
            )
        )
        emitted.append((s, t))
    return out


def resolved_ranges_from_events(events: Sequence[Event]) -> tuple[ResolvedContextRange, ...]:
    """Project ContextResolvedEvents into CXT-1 ResolvedContextRange metadata,
    attaching a SUMMARY ArtifactMemoryRef when a durable summary exists. Read-only;
    the canonical truth remains the events themselves."""
    summaries: dict[str, ContextSummaryEvent] = {
        e.range_id: e for e in events if isinstance(e, ContextSummaryEvent)
    }
    out: list[ResolvedContextRange] = []
    for e in events:
        if isinstance(e, ContextResolvedEvent):
            se = summaries.get(e.range_id)
            sref = (
                ArtifactMemoryRef(kind=ArtifactMemoryKind.SUMMARY, rel_path=se.rel_path)
                if se is not None
                else None
            )
            out.append(
                ResolvedContextRange(range_id=e.range_id, reason=e.reason, summary_ref=sref)
            )
    return tuple(out)
