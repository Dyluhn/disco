"""EventChainOracle unit tests (guidelines §11.1–11.3, PR S2)."""

from __future__ import annotations

from _eventlog import (
    action,
    agent_error,
    awaiting,
    clean_smoke_log,
    msg,
    observation,
    plan,
    status,
)

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.event_chain import EventChainOracle

_SCN_PLAN = {
    "id": "s",
    "assertions": {"event_chain": {"require_plan_before_execution": True}},
}


def _run(events, scenario=None):
    return EventChainOracle().check(normalize_events(events), scenario=scenario)


def test_clean_log_passes():
    results = _run(clean_smoke_log(), _SCN_PLAN)
    assert all(r.passed for r in results), [r.to_dict() for r in results]


def test_no_user_event_fails():
    results = _run([status(1, "RUNNING"), plan(2)])
    assert results[0].failed
    assert results[0].code == "NO_USER_EVENT_AFTER_SUBMIT"


def test_missing_plan_when_required_fails():
    events = [msg(1, "user", "build"), status(2, "RUNNING")]
    results = _run(events, _SCN_PLAN)
    assert results[0].code == "NO_PLAN_AFTER_USER_TURN"
    assert results[0].first_broken_link == "user_event -> plan_event"


def test_action_without_observation_fails():
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="act5"),
        # no observation for act5
        status(6, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "ACTION_NO_OBSERVATION"
    assert fail.facts["action_id"] == "act5"


def test_agent_error_counts_as_valid_pairing():
    # A rejected action answered by an AgentErrorEvent IS visible feedback — a
    # valid pairing, not a missing observation.
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="act5"),
        agent_error(6, "act5", error="rejected by policy"),
        action(7, "shell", action_id="act7"),
        observation(8, "act7"),
        status(9, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    assert all(r.passed for r in results), [r.to_dict() for r in results]


def test_observation_without_action_fails():
    # A real action+obs pair (so approval -> execution passes), plus a DANGLING
    # observation referencing no action.
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="act5"),
        observation(6, "act5"),
        observation(7, "ghost-action"),
        status(8, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "OBSERVATION_WITHOUT_ACTION"


def test_approved_plan_finished_with_no_action_fails():
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        status(5, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "APPROVE_PLAN_NO_EXECUTION"
    assert fail.first_broken_link == "approval_status -> execution_action"


def test_plan_approved_without_preceding_awaiting_fails():
    # A forged/skipped human gate: RUNNING/plan_approved with NO preceding
    # AWAITING_PLAN_APPROVAL in an interactive run.
    events = [
        msg(1, "user", "build"),
        plan(2),
        status(3, "RUNNING", "plan_approved"),  # no awaiting before it
        action(4, "shell", action_id="act4"),
        observation(5, "act4"),
        status(6, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "PLAN_APPROVED_STATUS_MISSING"
    assert fail.first_broken_link == "plan_event -> awaiting_plan_approval"


def test_autonomous_inline_approval_without_awaiting_passes():
    # An autonomous build auto-approves inline (no AWAITING) — a LEGITIMATE chain.
    events = [
        msg(1, "user", "build"),
        plan(2),
        status(3, "RUNNING", "plan_approved"),  # autonomous inline approve
        action(4, "shell", action_id="act4"),
        observation(5, "act4"),
        status(6, "FINISHED"),
    ]
    scenario = {**_SCN_PLAN, "autonomous": True}
    results = _run(events, scenario)
    assert all(r.passed for r in results), [r.to_dict() for r in results]


def test_observation_before_its_action_fails():
    # An observation that PRECEDES the action it references is an invalid pairing.
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        observation(5, "act6"),  # observation BEFORE its action
        action(6, "shell", action_id="act6"),
        status(7, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    # act6 has no LATER response -> ACTION_NO_OBSERVATION (the earlier obs doesn't pair)
    assert fail.code in {"ACTION_NO_OBSERVATION", "OBSERVATION_WITHOUT_ACTION"}
