"""CXT-3 tests: agent-driven deferred snip — events, guards, and the reuse of the
engine's CondensationEvent tombstone (View.of omission + recover_span audit)."""

from __future__ import annotations

from disco.core import EventAdapter
from disco.core.context import (
    ArtifactMemoryKind,
    CompactionPolicy,
    context_compact_if_needed,
    context_mark_resolved,
    context_write_summary,
    resolved_ranges_from_events,
)
from disco.core.events import (
    CondensationEvent,
    ContextResolvedEvent,
    ContextSummaryEvent,
    Event,
    LLMConvertible,
)
from disco.core.view import View
from event_fakes import user_msg, with_seqs


def _msgs(view: View) -> list[str]:
    return [m.content for m in view.messages]


def _base() -> list[Event]:
    # seqs 1..4 with distinct content
    return with_seqs([user_msg("A"), user_msg("B"), user_msg("C"), user_msg("D")])


# --- union integrity + audit-only ---------------------------------------------
def test_new_events_roundtrip_and_are_not_llm_convertible() -> None:
    for e in (
        ContextResolvedEvent(forgotten_start_seq=2, forgotten_end_seq=3),
        ContextSummaryEvent(range_id="r1", rel_path=".disco/context/summary/r1.md", summary="s"),
    ):
        assert EventAdapter.validate_python(e.model_dump(mode="json")) == e
        assert not isinstance(e, LLMConvertible)


def test_resolved_event_default_range_id_prefix() -> None:
    assert context_mark_resolved(1, 2).range_id.startswith("cxr_")


# --- deferred mark alone changes nothing --------------------------------------
def test_mark_alone_does_not_change_view() -> None:
    events = _base()
    before = _msgs(View.of(events))
    mark = context_mark_resolved(2, 3, range_id="r1")
    after = _msgs(View.of([*events, mark]))
    assert before == after  # the mark is deferred + not LLMConvertible


# --- never compact without a durable summary ----------------------------------
def test_no_compaction_without_summary() -> None:
    events = [*_base(), context_mark_resolved(2, 3, range_id="r1")]
    out = context_compact_if_needed(events, CompactionPolicy.default())
    assert out == []


def test_no_compaction_with_empty_summary() -> None:
    events = [
        *_base(),
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "   "),  # blank
    ]
    assert context_compact_if_needed(events, CompactionPolicy.default()) == []


# --- compaction executes via CondensationEvent, omitting from view ------------
def test_compaction_omits_from_view_but_keeps_audit() -> None:
    events = [
        *_base(),
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "explored B/C — dead end"),
    ]
    out = context_compact_if_needed(events, CompactionPolicy.default())
    assert len(out) == 1
    cond = out[0]
    assert isinstance(cond, CondensationEvent)
    assert (cond.forgotten_start_seq, cond.forgotten_end_seq) == (2, 3)

    # append the tombstone (give it a seq as the store would) and re-derive the view
    all_events = [*events, cond.model_copy(update={"seq": 5})]
    contents = _msgs(View.of(all_events))
    assert "B" not in contents and "C" not in contents  # omitted from model view
    assert "A" in contents and "D" in contents  # neighbours kept
    assert "explored B/C — dead end" in contents  # summary emitted in place

    # audit log complete: the original events are recoverable
    recovered = View.recover_span(all_events, cond)
    assert {e.seq for e in recovered} == {2, 3}


# --- guards -------------------------------------------------------------------
def test_protected_seqs_block_compaction() -> None:
    events = [
        *_base(),
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "summary"),
    ]
    out = context_compact_if_needed(
        events, CompactionPolicy.default(), protected_seqs=frozenset({3})
    )
    assert out == []  # unresolved-failure seq inside the range → never compacted


def test_idempotent_after_execution() -> None:
    events = [
        *_base(),
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "summary"),
    ]
    first = context_compact_if_needed(events, CompactionPolicy.default())
    assert len(first) == 1
    # after the tombstone exists, a second pass is a no-op
    again = context_compact_if_needed([*events, first[0]], CompactionPolicy.default())
    assert again == []


def test_overlap_with_existing_condensation_skipped() -> None:
    existing = CondensationEvent(
        forgotten_start_seq=2, forgotten_end_seq=3, summary="old", reason="tokens"
    )
    events = [
        *_base(),
        existing,
        context_mark_resolved(2, 4, range_id="r1"),  # overlaps [2,3]
        context_write_summary("r1", ".disco/context/summary/r1.md", "summary"),
    ]
    assert context_compact_if_needed(events, CompactionPolicy.default()) == []


def test_pressure_gate_below_threshold_skips() -> None:
    events = [
        *_base(),
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "summary"),
    ]
    pol = CompactionPolicy.default()
    # pressure under the cap → defer
    assert context_compact_if_needed(events, pol, pressure_chars=pol.max_history_chars - 1) == []
    # pressure over the cap → execute
    assert (
        len(context_compact_if_needed(events, pol, pressure_chars=pol.max_history_chars + 1)) == 1
    )


# --- projection into CXT-1 metadata -------------------------------------------
def test_resolved_ranges_from_events_attaches_summary_ref() -> None:
    events = [
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "s"),
        context_mark_resolved(5, 6, range_id="r2"),  # no summary yet
    ]
    ranges = resolved_ranges_from_events(events)
    by_id = {r.range_id: r for r in ranges}
    assert by_id["r1"].summary_ref is not None
    assert by_id["r1"].summary_ref.kind is ArtifactMemoryKind.SUMMARY
    assert by_id["r1"].summary_ref.rel_path.endswith("r1.md")
    assert by_id["r2"].summary_ref is None
