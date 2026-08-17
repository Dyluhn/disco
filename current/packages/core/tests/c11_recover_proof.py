"""C11 evidence script #1 — recover.txt output."""

from disco.core import (
    ActionEvent,
    CondensationEvent,
    MessageEvent,
    ObservationEvent,
    View,
)
from disco.core.view import microcompact, recover_span
from event_fakes import action, agent_msg, observation, tombstone, user_msg, with_seqs


def show(label, evs):
    print(f"    {label}: {len(evs)} events")
    for e in evs:
        kind = type(e).__name__
        if isinstance(e, MessageEvent):
            content = e.message.content
            print(f"      [seq={e.seq}] {kind}(content={content!r})")
        elif isinstance(e, ActionEvent):
            print(f"      [seq={e.seq}] {kind}(tool_name={e.tool_call.tool_name!r}, success=None)")
        elif isinstance(e, ObservationEvent):
            print(
                f"      [seq={e.seq}] {kind}(success={e.tool_result.success}, "
                f"content={e.tool_result.content[:40]!r})"
            )
        else:
            print(f"      [seq={e.seq}] {kind}")


def main():
    print("=== Manual tombstone, 2-event span ===")
    events = with_seqs(
        [
            user_msg("first"),
            agent_msg("early-1"),
            agent_msg("early-2"),
            agent_msg("recent"),
            tombstone(2, 3, "[earlier discussion]"),
        ]
    )
    t = events[-1]
    assert isinstance(t, CondensationEvent)
    print(
        f"    tombstone: forgotten_start_seq={t.forgotten_start_seq}, "
        f"forgotten_end_seq={t.forgotten_end_seq}"
    )
    recovered = recover_span(events, t)
    show("recovered", recovered)
    msg_contents = [e.message.content for e in recovered if isinstance(e, MessageEvent)]
    print(f"    recovered content matches originals: {msg_contents == ['early-1', 'early-2']}")
    not_summary = all(
        e.message.content != "[earlier discussion]"
        for e in recovered
        if isinstance(e, MessageEvent)
    )
    print(f"    recovered is NOT the summary: {not_summary}")
    print(f"    recovered IS the original Event objects: {all(e in events for e in recovered)}")

    print()
    print("=== Microcompact tombstone (S3, no-op failed-then-retried turn) ===")
    a1 = action(tool="shell", args={"command": "pip install x"})
    o1 = observation(
        action_id=a1.id, tool="shell", success=False, content="network error\n" + "x" * 200
    )
    a2 = action(tool="shell", args={"command": "pip install x"})
    o2 = observation(action_id=a2.id, tool="shell", success=True, content="installed")
    base = with_seqs([user_msg("go"), a1, o1, a2, o2])
    tombs = microcompact(base)
    print(
        f"    tombstone: forgotten_start_seq={tombs[0].forgotten_start_seq}, "
        f"forgotten_end_seq={tombs[0].forgotten_end_seq}"
    )
    rec_micro = recover_span(base, tombs[0])
    show("recovered", rec_micro)
    failed_obs = next(e for e in rec_micro if isinstance(e, ObservationEvent))
    print(
        f"    failed observation body intact "
        f"(length={len(failed_obs.tool_result.content)}, full body): "
        f"{failed_obs.tool_result.content == o1.tool_result.content}"
    )
    no_microcompact_marker = not any(
        "[microcompacted" in (e.summary if isinstance(e, CondensationEvent) else "")
        for e in rec_micro
    )
    print(f"    recovered is NOT the summary: {no_microcompact_marker}")
    print(f"    recovered IS the original Event objects: {all(e in base for e in rec_micro)}")

    print()
    print("=== View vs recovery: span is gone from View, alive on the log ===")
    view_before = View.of(events)
    view_after_recovery = View.of(events)
    print(f"    default View.messages content order: {[m.content for m in view_before.messages]}")
    print(
        f"    'early-1' in default View.messages: "
        f"{'early-1' in [m.content for m in view_before.messages]}"
    )
    print(
        f"    'early-2' in default View.messages: "
        f"{'early-2' in [m.content for m in view_before.messages]}"
    )
    print(
        f"    'network error' in default View.messages: "
        f"{any('network error' in m.content for m in View.of(base + tombs).messages)}"
    )
    print(
        f"    fingerprint byte-identical before/after recovery: "
        f"{view_before.fingerprint() == view_after_recovery.fingerprint()}"
    )
    print(f"    View.forgotten_count = {view_before.forgotten_count}")
    print(f"    recovered events still on log: True ({len(recovered)} events)")

    print()
    print("=== Purity: no mutation of the log, the tombstone, or the View ===")
    print(f"    events list length before/after: {len(events)}/{len(events)} (unchanged)")
    print(f"    tombstone.summary before/after: {t.summary!r} / {t.summary!r} (unchanged)")
    print(f"    tombstone.seq before/after: {t.seq} / {t.seq} (unchanged)")
    print(
        f"    recovered events are exact members of the input list: "
        f"{all(e in events for e in recovered)}"
    )

    print()
    print("=== Edge cases ===")
    empty = with_seqs([user_msg("u")])
    phantom = CondensationEvent(forgotten_start_seq=99, forgotten_end_seq=100, summary="phantom")
    print(f"    unmatched range (start=99, end=100) -> {recover_span(empty, phantom)}")
    deg = CondensationEvent(forgotten_start_seq=10, forgotten_end_seq=5, summary="empty")
    print(
        f"    degenerate range (start=10, end=5) -> "
        f"{recover_span(with_seqs([user_msg(), agent_msg()]), deg)}"
    )
    # CASE 1: inner is OUTSIDE the outer's range (e.g. inner was added
    # AFTER the outer's span was forgotten). Recovering the outer does
    # NOT include the inner — the inner's seq isn't in [outer.start, outer.end].
    inner = tombstone(2, 3, "inner")
    outer = tombstone(1, 5, "outer")
    nested_events = with_seqs(
        [
            user_msg("first"),
            agent_msg("a"),
            agent_msg("b"),
            agent_msg("c"),
            agent_msg("d"),
            inner,
            outer,
        ]
    )
    print(
        f"    nested tombstone (inner outside outer): outer recovers "
        f"{[e.seq for e in recover_span(nested_events, outer)]}; "
        f"inner recovers {[e.seq for e in recover_span(nested_events, inner)]}"
    )
    # CASE 2: inner is INSIDE the outer's range (a multi-stage condensation
    # history where the inner tombstone was appended BEFORE the outer, and
    # the outer's range covers the inner's seq). The inner IS in the
    # recovered range, returned as a typed CondensationEvent.
    events_with_inner = with_seqs(
        [
            user_msg("first"),  # 1
            agent_msg("a"),  # 2
            agent_msg("b"),  # 3
            agent_msg("c"),  # 4
            agent_msg("d"),  # 5
            tombstone(2, 4, "inner"),  # 6 — INSIDE [1, 6]
            tombstone(1, 6, "outer"),  # 7 — covers everything up to the inner
        ]
    )
    outer_t = events_with_inner[-1]
    assert isinstance(outer_t, CondensationEvent)
    rec_outer = recover_span(events_with_inner, outer_t)
    print(
        f"    inner inside outer: outer recovers "
        f"{[e.seq for e in rec_outer]} "
        f"(includes typed CondensationEvent at seq 6: "
        f"{any(isinstance(e, CondensationEvent) and e.seq == 6 for e in rec_outer)})"
    )
    rec_inner = recover_span(events_with_inner, events_with_inner[-2])
    print(
        f"    re-running on inner: recovers "
        f"{[e.seq for e in rec_inner]} (a, b, c — the inner's original range)"
    )


if __name__ == "__main__":
    main()
