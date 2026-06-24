"""RevisionOracle unit tests (guidelines §11.4, PR S2)."""

from __future__ import annotations

from _eventlog import action, awaiting, msg, observation, plan, status

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.revision import RevisionOracle


def _run(events, scenario=None):
    return RevisionOracle().check(normalize_events(events), scenario=scenario)


def _initial_build():
    # user -> plan rev1 -> awaiting -> approved -> action -> obs -> finished
    return [
        msg(1, "user", "build a page"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="act5"),
        observation(6, "act5"),
        status(7, "FINISHED"),
    ]


def test_clean_replan_passes():
    # full revised chain: followup < revised plan(rev2) < AWAITING < approved < action
    events = _initial_build() + [
        msg(8, "user", "revise the heading"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        awaiting(11, 10),
        status(12, "RUNNING", "plan_approved"),
        action(13, "file_write", args={"path": "index.html", "content": "x"}, action_id="act13"),
        observation(14, "act13"),
        status(15, "FINISHED"),
    ]
    results = _run(events)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]


def test_revised_approval_before_revised_plan_fails():
    # The out-of-order revised-approval false-pass: plan_approved appears BEFORE the
    # revised PlanEvent — a stale approval, not the revised one.
    events = _initial_build() + [
        msg(8, "user", "revise it"),
        status(9, "RUNNING", "plan_approved"),  # approval BEFORE the revised plan
        plan(10, revision=2),
        action(11, "file_write", args={"path": "x"}, action_id="act11"),
        observation(12, "act11"),
        status(13, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "STALE_PLAN_USED_AFTER_FOLLOWUP"
    assert fail.first_broken_link == "followup_user_event -> revised_plan_approval"


def test_revised_plan_finished_without_approval_is_stale():
    # A revised plan that reached FINISHED but was never approved AFTER it.
    events = _initial_build() + [
        msg(8, "user", "revise it"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        awaiting(11, 10),
        status(12, "FINISHED"),  # never approved the revised plan
    ]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "STALE_PLAN_USED_AFTER_FOLLOWUP"


def test_revised_approval_without_awaiting_interactive_fails():
    # Revised plan -> plan_approved with NO AWAITING gate between them (interactive).
    events = _initial_build() + [
        msg(8, "user", "revise"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        status(11, "RUNNING", "plan_approved"),  # no awaiting before it
        action(12, "file_write", args={"path": "x"}, action_id="act12"),
        observation(13, "act12"),
        status(14, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "PLAN_APPROVED_STATUS_MISSING"
    assert fail.first_broken_link == "revised_plan_event -> awaiting_plan_approval"


def test_autonomous_revised_chain_passes():
    # Autonomous revised build: inline approval, no AWAITING gate — legitimate.
    events = _initial_build() + [
        msg(8, "user", "revise"),
        plan(9, revision=2),
        status(10, "RUNNING", "plan_approved"),  # autonomous inline approve
        action(11, "file_write", args={"path": "x"}, action_id="act11"),
        observation(12, "act11"),
        status(13, "FINISHED"),
    ]
    scenario = {"id": "s", "autonomous": True, "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]


def test_no_replan_after_followup_with_freebuild_fails():
    # follow-up then a mutating action with NO new plan/planning entry -> stale plan.
    events = _initial_build() + [
        msg(8, "user", "also add a contact page"),
        action(9, "file_write", args={"path": "contact.html", "content": "x"}, action_id="act9"),
        observation(10, "act9"),
        status(11, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
    assert fail.facts["followup_user_event_seq"] == 8
    assert fail.facts["write_before_revised_plan_approval"] is True


def test_revision_not_incremented_fails():
    events = _initial_build() + [
        msg(8, "user", "revise"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=1),  # BUG: revision did not increment
        awaiting(11, 10),
        status(12, "RUNNING", "plan_approved"),
        action(13, "shell", action_id="act13"),
        observation(14, "act13"),
        status(15, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "PLAN_REVISION_NOT_INCREMENTED"


def test_write_before_revision_approval_fails():
    # new plan IS proposed, but a write lands before it is approved.
    events = _initial_build() + [
        msg(8, "user", "revise"),
        status(9, "RUNNING", "planning"),
        action(10, "file_write", args={"path": "x", "content": "y"}, action_id="act10"),
        observation(11, "act10"),
        plan(12, revision=2),
        awaiting(13, 12),
        status(14, "RUNNING", "plan_approved"),
        status(15, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "WRITE_BEFORE_REVISION_APPROVAL"
    assert fail.facts["first_write_tool_after_followup_seq"] == 10


def test_no_followup_skips():
    results = _run(_initial_build())
    assert all(r.skipped for r in results)


def test_scenario_requires_revision_even_without_write():
    # A declared follow-up requiring a revision, but no new plan and no write.
    events = _initial_build() + [msg(8, "user", "thanks, also tweak it")]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
