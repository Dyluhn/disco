"""No-fluke replay policy (guidelines §17, §2). A failed run that passes on exact
replay is FAIL/INTERMITTENT_<code>, never PASS; "fluke" never yields a pass."""

from __future__ import annotations

import pytest
from _eventlog import action, awaiting, msg, observation, plan, status
from harness.build_soak.classify import classify, intermittent_classification


def _no_replan_failure():
    events = [
        msg(1, "user", "build"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),
        observation(6, "a5"),
        status(7, "FINISHED"),
        msg(8, "user", "also add a page"),
        action(9, "file_write", args={"path": "c.html", "content": "x"}, action_id="a9"),
        observation(10, "a9"),
        status(11, "FINISHED"),
    ]
    c = classify(events, run_id="orig")
    assert c["status"] == "FAIL"
    assert c["code"] == "NO_REPLAN_AFTER_REVISION"
    return c


def test_replay_pass_becomes_intermittent_not_pass():
    original = _no_replan_failure()
    updated = intermittent_classification(original, replay_status="PASS", replay_run_id="replay1")
    assert updated["status"] == "FAIL"  # NEVER PASS
    assert updated["code"] == "INTERMITTENT_NO_REPLAN_AFTER_REVISION"
    assert updated["severity"] == "P0"  # same severity as the original code
    assert updated["replay"] == {"attempted": True, "result": "passed", "run_id": "replay1"}
    # input is not mutated
    assert original["code"] == "NO_REPLAN_AFTER_REVISION"


def test_replay_same_failure_is_deterministic():
    original = _no_replan_failure()
    updated = intermittent_classification(
        original,
        replay_status="FAIL",
        replay_code="NO_REPLAN_AFTER_REVISION",
        replay_run_id="replay1",
    )
    assert updated["code"] == "NO_REPLAN_AFTER_REVISION"
    assert updated["replay"]["result"] == "same_failure"


def test_replay_different_failure_is_recorded():
    original = _no_replan_failure()
    updated = intermittent_classification(
        original, replay_status="FAIL", replay_code="ACTION_NO_OBSERVATION"
    )
    assert updated["replay"]["result"] == "different_failure"
    assert updated["code"] == "NO_REPLAN_AFTER_REVISION"


def test_policy_rejects_non_failed_original():
    passing = {"status": "PASS", "code": None}
    with pytest.raises(ValueError):
        intermittent_classification(passing, replay_status="PASS")


def test_fluke_word_has_no_pass_path():
    # No combination of inputs yields a PASS from an originally-failed run.
    original = _no_replan_failure()
    for replay_status in ("PASS", "FAIL", "INVALID_RUN", "INFRA_FAILURE"):
        updated = intermittent_classification(original, replay_status=replay_status)
        assert updated["status"] == "FAIL"
