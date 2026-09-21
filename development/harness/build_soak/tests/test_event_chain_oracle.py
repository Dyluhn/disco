"""EventChainOracle unit tests (guidelines §11.1–11.3, PR S2)."""

from __future__ import annotations

from copy import deepcopy

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


def _verifier_repair_events():
    repaired_fingerprints = ["sha256:corrected-predicate"]
    repair_approval = status(9, "RUNNING", "plan_approved")
    repair_approval["plan_verification_transition"] = {
        "old_plan_revision": 1,
        "old_plan_event_id": "evt_2",
        "old_predicate_fingerprints": ["sha256:broken-predicate"],
        "new_plan_revision": 2,
        "new_plan_event_id": "evt_7",
        "new_predicate_fingerprints": repaired_fingerprints,
        "old_authority": "plan",
        "new_authority": "plan",
        "external_authority": "external",
        "external_predicate_fingerprints": [],
        "reason": "approved_plan_verifier_repair",
    }
    verifier_pass = status(10, "RUNNING", "plan_verification_passed")
    verifier_pass["plan_verifier_pass"] = {
        "plan_revision": 2,
        "plan_event_id": "evt_7",
        "predicate_fingerprints": repaired_fingerprints,
        "spec_fingerprint": "sha256:corrected-plan-spec",
        "authority": "plan",
    }
    return [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="act5"),
        observation(6, "act5"),
        plan(7, revision=2),
        awaiting(8, 7),
        repair_approval,
        verifier_pass,
        status(11, "FINISHED"),
    ]


def test_verifier_only_repair_with_exact_host_pass_receipt_satisfies_execution_edge():
    """Correct product bytes need no no-op mutation after a verifier-only replan.

    The alternative C->D edge is nevertheless strict: a host pass receipt must bind
    the latest approved repair plan and its exact predicate set.
    """
    events = _verifier_repair_events()
    results = _run(events, _SCN_PLAN)
    assert all(result.passed for result in results), [result.to_dict() for result in results]


def test_verifier_repair_receipt_cannot_bypass_execution_edge_when_mutated():
    mutations = {
        "absent receipt": lambda events: events.pop(9),
        "wrong plan id": lambda events: events[9]["plan_verifier_pass"].update(
            plan_event_id="evt_wrong"
        ),
        "wrong revision": lambda events: events[9]["plan_verifier_pass"].update(plan_revision=3),
        "wrong predicates": lambda events: events[9]["plan_verifier_pass"].update(
            predicate_fingerprints=["sha256:wrong"]
        ),
        "receipt before approval": lambda events: events[9].update(seq=8),
        "ordinary replan": lambda events: events[8]["plan_verification_transition"].update(
            reason="approved_plan_revision"
        ),
        "receipt after terminal": lambda events: (
            events[9].update(seq=11),
            events[10].update(seq=10),
            events.append(status(12, "FINISHED")),
        ),
    }
    for label, mutate in mutations.items():
        events = deepcopy(_verifier_repair_events())
        mutate(events)
        results = _run(events, _SCN_PLAN)
        failure = next((result for result in results if result.failed), None)
        assert failure is not None, label
        assert failure.code == "APPROVE_PLAN_NO_EXECUTION", label


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
