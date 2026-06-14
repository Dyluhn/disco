"""C11 evidence script #2 — still-omitted.txt output."""
from disco.core import (
    ActionEvent, CondensationEvent, MessageEvent, ObservationEvent,
    ToolCall, ToolResult, View,
)
from disco.core.view import microcompact, recover_span
from conftest import action, agent_msg, observation, tombstone, user_msg, with_seqs


def main():
    print("=== Default View still omits the tombstoned span ===")
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
    # Materialize the View ONCE.
    view1 = View.of(events)
    # Call recovery — the on-demand accessor.
    recovered = recover_span(events, t)
    # Re-materialize the View. Recovery has no side effects on the View.
    view2 = View.of(events)
    print(f"    View.messages: {[m.content for m in view1.messages]}")
    print(
        f"    'early-1' in any message: "
        f"{'early-1' in [m.content for m in view1.messages]}"
    )
    print(
        f"    'early-2' in any message: "
        f"{'early-2' in [m.content for m in view1.messages]}"
    )
    print(f"    visible_seqs: {view1.visible_seqs}")
    print(f"    forgotten_count: {view1.forgotten_count}")
    print(
        f"    view1 == view2 (post-recovery re-materialize): "
        f"{[m.content for m in view1.messages] == [m.content for m in view2.messages]}"
    )
    print(f"    fingerprint byte-identical: {view1.fingerprint() == view2.fingerprint()}")

    print()
    print("=== Recovery is ON-DEMAND: caller must invoke the accessor ===")
    print(f"    recovered = recover_span(events, tombstone)  # {len(recovered)} events")
    print(f"    # `recovered` is a separate list the caller can use however")
    print(f"    # they like. The View never sees it.")

    print()
    print("=== Live context is NOT bloated ===")
    print(
        f"    The View's message count is {len(view1.messages)} "
        f"(user, summary, recent). The recovered {len(recovered)} events are"
    )
    print(f"    NOT counted toward the View's messages.")

    print()
    print("=== Microcompact case: failed turn still dropped, originals recoverable ===")
    a1 = action(tool="shell", args={"command": "pip install x"})
    o1 = observation(
        action_id=a1.id, tool="shell", success=False,
        content="network error\n" + "x" * 200,
    )
    a2 = action(tool="shell", args={"command": "pip install x"})
    o2 = observation(action_id=a2.id, tool="shell", success=True, content="installed")
    base = with_seqs([user_msg("go"), a1, o1, a2, o2])
    tombs = microcompact(base)
    view_micro = View.of(base + tombs)
    fp_micro_before = view_micro.fingerprint()
    rec_micro = recover_span(base, tombs[0])
    view_micro2 = View.of(base + tombs)
    fp_micro_after = view_micro2.fingerprint()
    print(f"    View.messages: {[m.content for m in view_micro.messages]}")
    print(
        f"    'network error' in any message: "
        f"{any('network error' in m.content for m in view_micro.messages)}"
    )
    print(
        f"    recovered from microcompact: {len(rec_micro)} events "
        f"({type(rec_micro[0]).__name__}, {type(rec_micro[1]).__name__})"
    )
    print(
        f"    fingerprint byte-identical before/after recovery: "
        f"{fp_micro_before == fp_micro_after}"
    )


if __name__ == "__main__":
    main()
