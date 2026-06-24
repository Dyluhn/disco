"""RevisionOracle unit tests (guidelines §11.4, PR S2)."""

from __future__ import annotations

from _eventlog import action, msg, observation, plan, status

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.revision import RevisionOracle


def _run(events, scenario=None):
    return RevisionOracle().check(normalize_events(events), scenario=scenario)


def _initial_build():
    # user -> plan rev1 -> approved -> action -> obs -> finished
    return [
        msg(1, "user", "build a page"),
        plan(2, revision=1),
        status(3, "RUNNING", "plan_approved"),
        action(4, "shell", action_id="act4"),
        observation(5, "act4"),
        status(6, "FINISHED"),
    ]


def test_clean_replan_passes():
    events = _initial_build() + [
        msg(7, "user", "revise the heading"),
        status(8, "RUNNING", "planning"),
        plan(9, revision=2),
        status(10, "RUNNING", "plan_approved"),
        action(11, "file_write", args={"path": "index.html", "content": "x"}, action_id="act11"),
        observation(12, "act11"),
        status(13, "FINISHED"),
    ]
    results = _run(events)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]


def test_no_replan_after_followup_with_freebuild_fails():
    # follow-up then a mutating action with NO new plan/planning entry -> stale plan.
    events = _initial_build() + [
        msg(7, "user", "also add a contact page"),
        action(8, "file_write", args={"path": "contact.html", "content": "x"}, action_id="act8"),
        observation(9, "act8"),
        status(10, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
    assert fail.facts["followup_user_event_seq"] == 7
    assert fail.facts["write_before_revised_plan_approval"] is True


def test_revision_not_incremented_fails():
    events = _initial_build() + [
        msg(7, "user", "revise"),
        status(8, "RUNNING", "planning"),
        plan(9, revision=1),  # BUG: revision did not increment
        status(10, "RUNNING", "plan_approved"),
        action(11, "shell", action_id="act11"),
        observation(12, "act11"),
        status(13, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "PLAN_REVISION_NOT_INCREMENTED"


def test_write_before_revision_approval_fails():
    # new plan IS proposed, but a write lands before it is approved.
    events = _initial_build() + [
        msg(7, "user", "revise"),
        status(8, "RUNNING", "planning"),
        action(9, "file_write", args={"path": "x", "content": "y"}, action_id="act9"),
        observation(10, "act9"),
        plan(11, revision=2),
        status(12, "RUNNING", "plan_approved"),
        status(13, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "WRITE_BEFORE_REVISION_APPROVAL"
    assert fail.facts["first_write_tool_after_followup_seq"] == 9


def test_no_followup_skips():
    results = _run(_initial_build())
    assert all(r.skipped for r in results)


def test_scenario_requires_revision_even_without_write():
    # A declared follow-up requiring a revision, but no new plan and no write.
    events = _initial_build() + [msg(7, "user", "thanks, also tweak it")]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
