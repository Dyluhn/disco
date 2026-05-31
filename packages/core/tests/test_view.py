"""View + condensation tests — event-state-contract.md §8.3.

The View is the materialized "what the LLM sees", computed by applying
condensation tombstones to the raw log. Phase 0 ships the View and a no-op
condenser; the real LLMSummarizingCondenser (first-half/keep_first/
minimum_progress/hard-reset strategy) is a Phase 1 deliverable (BoD §22), so its
strategy-specific tests are deferred. Here we test the View's tombstone
*application* directly (which is the [CONTRACT] half) plus the no-op condenser.
"""

from __future__ import annotations

from conftest import (
    action,
    agent_msg,
    fatal,
    observation,
    status,
    tombstone,
    user_msg,
    with_seqs,
)
from perpleximanus.core import (
    ConversationStatus,
    NoOpCondenser,
    View,
)


def test_view_is_deterministic():
    events = with_seqs([user_msg(), action(), observation("evt_a")])
    assert View.of(events).messages == View.of(events).messages


def test_non_convertibles_never_appear():
    """StatusEvent / ErrorEvent / CondensationEvent are never in View.messages."""
    events = with_seqs(
        [
            user_msg("q"),
            status(ConversationStatus.RUNNING),
            agent_msg("working"),
            fatal(),
        ]
    )
    view = View.of(events)
    # Only the two LLMConvertible messages (user + agent) survive.
    assert [m.role for m in view.messages] == ["user", "assistant"]
    assert [m.content for m in view.messages] == ["q", "working"]


def test_tombstone_drops_span_and_inserts_summary_once():
    """A tombstone over a span hides those events and shows its summary exactly
    once in their place."""
    events = with_seqs(
        [
            user_msg("first"),  # seq 1
            agent_msg("early-1"),  # seq 2  (forgotten)
            agent_msg("early-2"),  # seq 3  (forgotten)
            agent_msg("recent"),  # seq 4  (kept)
            tombstone(2, 3, "[earlier discussion]"),  # seq 5  (the tombstone)
        ]
    )
    view = View.of(events)
    contents = [m.content for m in view.messages]
    # first (kept) -> summary (in place of 2,3) -> recent (kept).
    assert contents == ["first", "[earlier discussion]", "recent"]
    assert contents.count("[earlier discussion]") == 1  # summary emitted once
    assert view.forgotten_count == 2
    assert 2 not in view.visible_seqs and 3 not in view.visible_seqs


def test_forgotten_events_never_appear_even_with_overlapping_tombstones():
    events = with_seqs(
        [
            user_msg("first"),  # 1
            agent_msg("a"),  # 2
            agent_msg("b"),  # 3
            agent_msg("c"),  # 4
            tombstone(2, 3, "S1"),  # 5
            tombstone(3, 4, "S2"),  # 6  (overlaps on 3)
        ]
    )
    view = View.of(events)
    # 2,3,4 are all forgotten by the union of ranges; neither survives.
    assert all(s not in view.visible_seqs for s in (2, 3, 4))
    assert "a" not in [m.content for m in view.messages]


def test_back_half_is_preserved_byte_identical_after_condensation():
    """First-half-style condensation leaves the back half untouched (the
    property the real condenser relies on; tested here at the View level)."""
    raw = with_seqs([user_msg("u"), agent_msg("m1"), agent_msg("m2"), agent_msg("m3")])
    before = View.of(raw)
    back_half_before = [m.content for m in before.messages if m.content in ("m2", "m3")]

    condensed = raw + with_seqs([tombstone(1, 2, "[summary of u, m1]")], start=5)
    after = View.of(condensed)
    back_half_after = [m.content for m in after.messages if m.content in ("m2", "m3")]

    assert back_half_before == back_half_after == ["m2", "m3"]


# ---- the no-op condenser (real one deferred to Phase 1) ---------------------


def test_noop_condenser_never_condenses():
    events = with_seqs([user_msg()] + [agent_msg(f"m{i}") for i in range(300)])
    view = View.of(events)
    cond = NoOpCondenser()
    assert cond.should_condense(view, token_count=10_000_000) is None
    assert cond.condense(events, view, summarizer=_FakeSummarizer()) is None


class _FakeSummarizer:
    """Stand-in Summarizer (no real LLM) — the Phase 1 condenser tests will use
    a fake like this to assert the tombstone summary equals its output (§8.3)."""

    def summarize(self, messages):
        return "[fake summary]"
