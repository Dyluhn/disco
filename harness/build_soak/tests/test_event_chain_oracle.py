"""EventChainOracle unit tests (guidelines §11.1–11.3, PR S2)."""

from __future__ import annotations

from _eventlog import (
    action,
    agent_error,
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
        status(3, "RUNNING", "plan_approved"),
        action(4, "shell", action_id="act4"),
        # no observation for act4
        status(5, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "ACTION_NO_OBSERVATION"
    assert fail.facts["action_id"] == "act4"


def test_agent_error_counts_as_valid_pairing():
    # A rejected action answered by an AgentErrorEvent IS visible feedback — a
    # valid pairing, not a missing observation.
    events = [
        msg(1, "user", "build"),
        plan(2),
        status(3, "RUNNING", "plan_approved"),
        action(4, "shell", action_id="act4"),
        agent_error(5, "act4", error="rejected by policy"),
        action(6, "shell", action_id="act6"),
        observation(7, "act6"),
        status(8, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    assert all(r.passed for r in results), [r.to_dict() for r in results]


def test_observation_without_action_fails():
    # A real action+obs pair (so approval -> execution passes), plus a DANGLING
    # observation referencing no action.
    events = [
        msg(1, "user", "build"),
        plan(2),
        status(3, "RUNNING", "plan_approved"),
        action(4, "shell", action_id="act4"),
        observation(5, "act4"),
        observation(6, "ghost-action"),
        status(7, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "OBSERVATION_WITHOUT_ACTION"


def test_approved_plan_finished_with_no_action_fails():
    events = [
        msg(1, "user", "build"),
        plan(2),
        status(3, "RUNNING", "plan_approved"),
        status(4, "FINISHED"),
    ]
    results = _run(events, _SCN_PLAN)
    fail = next(r for r in results if r.failed)
    assert fail.code == "APPROVE_PLAN_NO_EXECUTION"
    assert fail.first_broken_link == "approval_status -> execution_action"
