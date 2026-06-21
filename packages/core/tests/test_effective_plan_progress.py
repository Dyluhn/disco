"""effective_plan_progress — the UNIFIED plan-completion reader (plan-progress-source-
unify). Merges incremental `plan_step` marks + declarative `update_plan_progress`
snapshots, latest-event-wins, re-plan reset, partial-snapshot retain, out-of-range
bound, seqless list-position fallback. The fix for the live build that PAUSED on the
actionless valve because a capable model reported progress only via update_plan_progress.
"""

from disco.core import PlanStep
from disco.core.events import ActionEvent, PlanEvent, ToolCall
from disco.core.loop.signals import plan_is_incomplete
from disco.core.view import effective_plan_progress


def _plan(n: int, *, revision: int = 1, seq: int | None = None) -> PlanEvent:
    return PlanEvent(
        summary="p",
        steps=[PlanStep(title=str(i)) for i in range(1, n + 1)],
        revision=revision,
        seq=seq,
    )


def _act(tool: str, args: dict, *, seq: int | None = None) -> ActionEvent:
    return ActionEvent(
        thought="", tool_call=ToolCall(tool_name=tool, arguments=args, call_id="c"), seq=seq
    )


def _ps(idx: int, state: str, *, seq: int | None = None) -> ActionEvent:
    return _act("plan_step", {"index": idx, "state": state}, seq=seq)


def _upp(steps: list[dict], *, seq: int | None = None) -> ActionEvent:
    return _act("update_plan_progress", {"steps": steps}, seq=seq)


def test_update_plan_progress_all_done():
    """The core repro: all steps done via the declarative snapshot, no plan_step."""
    events = [_plan(3, seq=1), _upp([{"index": i, "state": "done"} for i in (1, 2, 3)], seq=2)]
    plan, states = effective_plan_progress(events)
    assert plan is not None
    assert states == {1: "done", 2: "done", 3: "done"}
    assert plan_is_incomplete(events) == (False, [])


def test_plan_step_only_unbroken():
    """The small-model incremental path is unchanged."""
    events = [_plan(2, seq=1), _ps(1, "done", seq=2)]
    assert plan_is_incomplete(events) == (True, [2])


def test_latest_wins_can_undo_done():
    """A later update_plan_progress moving a step done→active wins (latest-state)."""
    events = [_plan(2, seq=1), _ps(1, "done", seq=2), _upp([{"index": 1, "state": "active"}], seq=3)]
    _, states = effective_plan_progress(events)
    assert states[1] == "active"
    assert plan_is_incomplete(events)[0] is True


def test_partial_snapshot_retains_omitted():
    """A snapshot listing only step 2 must NOT un-complete step 1 (omitted = retain)."""
    events = [_plan(2, seq=1), _ps(1, "done", seq=2), _upp([{"index": 2, "state": "done"}], seq=3)]
    _, states = effective_plan_progress(events)
    assert states == {1: "done", 2: "done"}
    assert plan_is_incomplete(events) == (False, [])


def test_replan_resets_progress():
    """A re-plan (higher revision) starts a fresh checklist — prior marks don't carry."""
    events = [
        _plan(2, revision=1, seq=1),
        _upp([{"index": 1, "state": "done"}, {"index": 2, "state": "done"}], seq=2),
        _plan(2, revision=2, seq=3),
    ]
    plan, states = effective_plan_progress(events)
    assert plan is not None and plan.revision == 2
    assert states == {}
    assert plan_is_incomplete(events) == (True, [1, 2])


def test_out_of_range_indices_ignored():
    """A stale/out-of-range index in a snapshot is bounded out (plan has 2 steps)."""
    events = [_plan(2, seq=1), _upp([{"index": 5, "state": "done"}, {"index": 1, "state": "done"}], seq=2)]
    _, states = effective_plan_progress(events)
    assert states == {1: "done"}


def test_seqless_events_order_by_list_position():
    """Seqless fixtures: list position after the plan scopes correctly (no pre-plan carry)."""
    events = [_plan(2), _upp([{"index": 1, "state": "done"}, {"index": 2, "state": "done"}])]
    assert plan_is_incomplete(events) == (False, [])


def test_no_plan_is_not_incomplete():
    events = [_upp([{"index": 1, "state": "done"}])]
    assert effective_plan_progress(events) == (None, {})
    assert plan_is_incomplete(events) == (False, [])
