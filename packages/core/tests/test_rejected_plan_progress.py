"""Rejected progress is not plan completion, including after revision/reload."""

import pytest
from disco.core.events import AgentErrorEvent, EventAdapter, ObservationEvent, ToolResult
from disco.core.loop.signals import plan_is_incomplete
from disco.core.view import effective_plan_progress
from test_effective_plan_progress import _plan, _ps, _upp


def _rejection(action, kind):
    if kind == "agent_error":
        return AgentErrorEvent(action_id=action.id, error="invalid progress")
    return ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=False,
            content="rejected",
        ),
    )


def test_g41_malformed_snapshot_does_not_complete_revised_plan():
    rows = [{"index": i, "state": "done"} for i in range(1, 9)]
    rows += [{"index": 9, "state": "active"}]
    rows += [{"index": 10, "state": "pending"}] * 4
    rows += [{"index": 10, "table": "pending"}]
    bad = _upp(rows, seq=416)
    events = [
        _plan(7, revision=1, seq=11),
        _upp([{"index": i, "state": "done"} for i in range(1, 8)], seq=323),
        _plan(6, revision=2, seq=407),
        bad,
    ]
    assert effective_plan_progress(events)[1] == {}
    events.append(_rejection(bad, "agent_error"))
    reloaded = [EventAdapter.validate_json(event.model_dump_json()) for event in events]
    assert effective_plan_progress(reloaded)[1] == {}
    assert plan_is_incomplete(reloaded) == (True, [1, 2, 3, 4, 5, 6])


@pytest.mark.parametrize("kind", ["agent_error", "observation"])
@pytest.mark.parametrize("tool", ["snapshot", "incremental"])
def test_failed_progress_keeps_genuine_prior_state_and_later_correction(kind, tool):
    bad = _upp([{"index": 2, "state": "done"}]) if tool == "snapshot" else _ps(2, "done")
    events = [_plan(2), _ps(1, "done"), bad, _rejection(bad, kind)]
    assert effective_plan_progress(events)[1] == {1: "done"}
    assert plan_is_incomplete(events) == (True, [2])
    events.append(_ps(2, "active"))
    reloaded = [EventAdapter.validate_json(event.model_dump_json()) for event in events]
    assert effective_plan_progress(reloaded)[1] == {1: "done", 2: "active"}


@pytest.mark.parametrize("rows", [None, {}, [None], [{"index": 2}], [{"index": 2, "state": "bad"}]])
def test_malformed_snapshot_does_not_override_valid_marks(rows):
    events = [_plan(2), _ps(1, "active"), _upp(rows)]
    assert effective_plan_progress(events)[1] == {1: "active"}
