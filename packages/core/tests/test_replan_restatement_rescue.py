"""A restatement replan must not erase productive work the agent already did.

F1 failure 2026-07-27 (`p4_appkit_semantic_edit` seed 490007). The agent created
the app, retitled it via `app_update_content`, and passed `verify_appkit_app`
three times. A replan then landed that restated the SAME six steps, and the
execution-nudge cap terminalized the run STUCK/approve_plan_no_execution on a
build that was finished and verified.

`finish_intent_replan_after_prior_productive_work` is the guard meant to prevent
exactly that, and it could not fire: its "remaining steps are finish-intent only"
route reads `states`, which is populated ONLY by the model's own
`update_plan_progress` / `plan_step` calls. That run made **zero** progress
reports across 151 events, so every step read as remaining forever.

The second route uses evidence the host owns — the plans' own declarative
`done_condition` predicates. It stays conservative: a replan that introduces any
new predicate is new work and still fails, and a never-executed plan still fails
the prior-productive check.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.dod import FileExistsPredicate
from disco.core.loop import signals

_APPSPEC = FileExistsPredicate(kind="file_exists", path=".disco/appspec.json")
_DESIGNSPEC = FileExistsPredicate(kind="file_exists", path=".disco/designspec.json")


def _plan(seq: int, revision: int, predicates: list[FileExistsPredicate | None]) -> PlanEvent:
    return PlanEvent(
        seq=seq,
        source=EventSource.AGENT,
        revision=revision,
        summary=f"plan r{revision}",
        steps=[
            PlanStep(title=f"step {i}", detail=f"do {i}", done_condition=p)
            for i, p in enumerate(predicates, start=1)
        ],
    )


def _approved(seq: int) -> StatusEvent:
    return StatusEvent(
        seq=seq,
        source=EventSource.SYSTEM,
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
    )


def _productive(seq: int, tool: str = "app_update_content") -> list[ActionEvent | ObservationEvent]:
    action = ActionEvent(
        seq=seq,
        source=EventSource.AGENT,
        thought="",
        tool_call=ToolCall(tool_name=tool, arguments={}),
    )
    return [
        action,
        ObservationEvent(
            seq=seq + 1,
            source=EventSource.ENVIRONMENT,
            action_id=action.id,
            tool_result=ToolResult(call_id=action.id, tool_name=tool, success=True, content="ok"),
        ),
    ]


def _run(latest_predicates: list[FileExistsPredicate | None], *, productive: bool = True):
    """A run with two approvals, work between them, then a replan."""
    events: list = [_plan(1, 1, [_APPSPEC, _DESIGNSPEC]), _approved(2)]
    if productive:
        events += _productive(3)
    events += [
        _approved(5),
        *(_productive(6) if productive else []),
        _plan(8, 2, latest_predicates),
        _approved(9),
    ]
    return events


def test_a_restatement_replan_rescues_prior_productive_work():
    # Same predicate set, no new obligation -> the prior work still stands.
    events = _run([_APPSPEC, _DESIGNSPEC])
    assert signals.finish_intent_replan_after_prior_productive_work(events) is True


def test_reordering_and_rewording_do_not_matter_only_obligations_do():
    events = _run([_DESIGNSPEC, _APPSPEC, None])
    assert signals.finish_intent_replan_after_prior_productive_work(events) is True


def test_a_replan_that_drops_an_obligation_is_still_a_restatement():
    # A strict subset introduces nothing new.
    events = _run([_APPSPEC])
    assert signals.finish_intent_replan_after_prior_productive_work(events) is True


def test_a_replan_that_ADDS_an_obligation_is_genuinely_new_work():
    # The whole point of the conservatism: new predicate -> not a restatement.
    events = _run(
        [_APPSPEC, _DESIGNSPEC, FileExistsPredicate(kind="file_exists", path="dist/bundle.js")]
    )
    assert signals.finish_intent_replan_after_prior_productive_work(events) is False


def test_a_never_executed_plan_still_fails_and_lands_stuck():
    # No productive work in the prior segment -> the guard must not rescue it,
    # so approve_plan_no_execution still fires. This is the anti-false-finish
    # invariant the gate exists for.
    events = _run([_APPSPEC, _DESIGNSPEC], productive=False)
    assert signals.finish_intent_replan_after_prior_productive_work(events) is False


def test_a_predicate_free_replan_cannot_use_this_route():
    # With no predicates on either side there is no host-owned evidence of what
    # "done" means, so the route declines rather than guessing.
    events: list = [_plan(1, 1, [None, None]), _approved(2), *_productive(3), _approved(5)]
    events += [_plan(8, 2, [None, None]), _approved(9)]
    assert signals.finish_intent_replan_after_prior_productive_work(events) is False


def test_the_rescue_does_not_depend_on_the_model_reporting_progress():
    # The regression in one line: no update_plan_progress / plan_step anywhere.
    events = _run([_APPSPEC, _DESIGNSPEC])
    assert not any(
        isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name in {"update_plan_progress", "plan_step"}
        for e in events
    )
    assert signals.finish_intent_replan_after_prior_productive_work(events) is True
