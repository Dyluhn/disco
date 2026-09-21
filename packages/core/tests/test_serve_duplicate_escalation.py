"""A repeated duplicate-`serve` refusal must say something new — and must say
what the HOST already knows rather than asking the agent.

Counted-promotion failure 2026-07-27 (`p4_ff_static_continue` seed 600002,
ACTIONLESS_THRASH). The agent handed off, then called `serve` twice more and got
a BYTE-IDENTICAL reminder each time. `serve` is non-productive, so each attempt
burned an actionless turn and the third tripped the valve — on a run that went
on to FINISH successfully.

The first reminder was correct and actionable, so this is not a broken contract;
it is an information defect. Repeating a sentence a model has already mis-read
gives it nothing to act on differently, and never mentions that the next repeat
ends the run.

The actionless cap itself is untouched. A genuinely stuck agent still lands in
the same valve at the same count.

**2026-08-06z — the projection rule (GROUNDED FEEDBACK constraint 3).** This file
previously asserted, in `test_it_never_tells_the_agent_to_finish_unconditionally`,
that the escalation CONTAINS the clause *"if the plan and its verification are
complete"*. The owner's dated record names that exact two-branch sentence as the
canonical counter-example and requires its repair: a corrective message must name
ONE next move or state that none exists, and must never hand the
done-determination back to the agent.

The protection that assertion was written for is real and is KEPT, in a stronger
form. The old test could only check that a hedge was present. These check the
behaviour the hedge was standing in for: when a plan step is outstanding the
message must NOT tell the agent to finish, it must NAME the step; and only when
the record shows nothing outstanding may it say to finish. That is a property of
the run, not of the wording, so it cannot be satisfied by a sentence that merely
sounds cautious.
"""

from __future__ import annotations

from disco.core.events import (
    ActionEvent,
    EventSource,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    ToolCall,
    ToolResult,
)
from disco.core.loop.turn_control import (
    _serve_duplicate_guidance,
    _serve_next_move,
)


def _plan(*titles: str) -> PlanEvent:
    return PlanEvent(
        summary="build the thing",
        steps=[PlanStep(title=title) for title in titles],
        seq=1,
    )


def _step_done(index: int, seq: int) -> list[ActionEvent | ObservationEvent]:
    action = ActionEvent(
        thought="marking progress",
        tool_call=ToolCall(
            tool_name="plan_step",
            call_id=f"c{index}",
            arguments={"index": index, "state": "done"},
        ),
        seq=seq,
    )
    observation = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=action.id,
        tool_result=ToolResult(
            tool_name="plan_step", call_id=f"c{index}", success=True, content="ok"
        ),
        seq=seq + 1,
    )
    return [action, observation]


def _verify_passed(seq: int) -> list[ActionEvent | ObservationEvent]:
    action = ActionEvent(
        thought="verifying",
        tool_call=ToolCall(tool_name="verify_web_app", call_id="v1", arguments={}),
        seq=seq,
    )
    observation = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=action.id,
        tool_result=ToolResult(
            tool_name="verify_web_app",
            call_id="v1",
            success=True,
            content="VERIFY_WEB_APP: PASS",
            structured={"passed": True},
        ),
        seq=seq + 1,
    )
    return [action, observation]


def _work_outstanding() -> list[object]:
    return [_plan("Scaffold the page", "Wire the form")]


def _nothing_outstanding() -> list[object]:
    events: list[object] = [_plan("Scaffold the page")]
    events.extend(_step_done(1, 2))
    events.extend(_verify_passed(4))
    return events


def test_the_first_refusal_is_unchanged():
    first = _serve_duplicate_guidance(1, _work_outstanding())  # type: ignore[arg-type]
    assert "already handed off" in first
    assert "Do not serve it again" in first


def test_a_repeat_stops_repeating_itself():
    events = _work_outstanding()
    first = _serve_duplicate_guidance(1, events)  # type: ignore[arg-type]
    second = _serve_duplicate_guidance(2, events)  # type: ignore[arg-type]
    assert second != first


def test_a_repeat_names_the_cost_and_the_remaining_moves():
    second = _serve_duplicate_guidance(2, _work_outstanding())  # type: ignore[arg-type]
    assert "ENDS this run" in second
    assert "not counted as work" in second
    assert "Do not call `serve` again" in second


def test_the_count_is_stated_so_each_repeat_carries_new_information():
    events = _work_outstanding()
    assert "2 times" in _serve_duplicate_guidance(2, events)  # type: ignore[arg-type]
    assert "3 times" in _serve_duplicate_guidance(3, events)  # type: ignore[arg-type]
    assert _serve_duplicate_guidance(2, events) != _serve_duplicate_guidance(  # type: ignore[arg-type]
        3, events
    )


def test_every_variant_stays_an_ambient_system_reminder():
    # Never a user-tone scolding — same framing rule as the other loop nudges.
    for repeats in (1, 2, 3, 9):
        text = _serve_duplicate_guidance(repeats, _work_outstanding())  # type: ignore[arg-type]
        assert text.startswith("<system-reminder>")
        assert text.rstrip().endswith("</system-reminder>")


# --- the projection rule (constraint 3) -----------------------------------


def test_it_never_tells_the_agent_to_finish_unconditionally():
    """The protection the old hedge stood for, asserted against the RUN.

    SAME test id as before 2026-08-06z, deliberately: the guarantee is unchanged
    and its lineage should survive. What changed is how it is checked. The old
    body asserted that the text CONTAINS the clause "if the plan and its
    verification are complete" — i.e. it required the hedge the owner's record
    names as the canonical counter-example. This body asserts the property that
    hedge was standing in for: while a plan step is outstanding the message must
    not say to finish, it must name the step. That is a property of the run, not
    of the wording, so it cannot be satisfied by a sentence that merely sounds
    cautious.
    """
    for repeats in (1, 2, 3):
        text = _serve_duplicate_guidance(repeats, _work_outstanding())  # type: ignore[arg-type]
        assert "Next move: plan step 1 (Scaffold the page)" in text
        assert "call `finish`. Every plan step" not in text


def test_it_names_finish_only_when_the_record_shows_nothing_outstanding():
    for repeats in (1, 2, 3):
        text = _serve_duplicate_guidance(repeats, _nothing_outstanding())  # type: ignore[arg-type]
        assert "Next move: call `finish`." in text
        assert "Every plan step is marked done" in text


def test_the_message_never_delegates_the_done_determination():
    """The canonical counter-example, as a standing assertion.

    The owner's record (2026-08-06 ~21:35 CDT) names this sentence shape. It must
    not come back — in either branch, at any repeat count.
    """
    forks = (
        "if the plan and its verification are complete",
        "If verification and plan work are complete",
        "otherwise perform the remaining work",
        "There are exactly two moves left",
    )
    for events in (_work_outstanding(), _nothing_outstanding()):
        for repeats in (1, 2, 3, 9):
            text = _serve_duplicate_guidance(repeats, events)  # type: ignore[arg-type]
            for fork in forks:
                assert fork not in text, f"delegated fork returned: {fork!r}"


def test_the_next_move_is_a_projection_of_the_same_state_the_loop_grades_with():
    """Message and enforcement read ONE source, so they cannot drift."""
    outstanding = _serve_next_move(_work_outstanding())  # type: ignore[arg-type]
    settled = _serve_next_move(_nothing_outstanding())  # type: ignore[arg-type]
    assert outstanding != settled
    # Exactly one move named, never a menu.
    for text in (outstanding, settled):
        assert text.count("Next move:") == 1
