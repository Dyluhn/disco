"""OracleResult schema round-trips + validates (guidelines §10, PR S1)."""

from __future__ import annotations

import pytest
from harness.build_soak.oracles.schema import (
    FAILED,
    OK,
    SKIPPED,
    OracleResult,
    failing,
    passing,
    skipping,
)


def test_pass_result_round_trips():
    r = passing("EventChainOracle", facts={"action_count": 2})
    d = r.to_dict()
    assert d["oracle"] == "EventChainOracle"
    assert d["status"] == OK
    assert OracleResult.from_dict(d) == r


def test_fail_result_round_trips_with_link():
    r = failing(
        "RevisionOracle",
        "NO_REPLAN_AFTER_REVISION",
        first_broken_link="followup_user_event -> revised_plan_event",
        facts={"followup_user_event_seq": 42},
    )
    d = r.to_dict()
    assert d["status"] == FAILED
    assert d["code"] == "NO_REPLAN_AFTER_REVISION"
    assert d["first_broken_link"] == "followup_user_event -> revised_plan_event"
    assert OracleResult.from_dict(d) == r


def test_skip_result_round_trips_with_reason():
    r = skipping("ToolScopeOracle", reason="needs captured tool scope")
    d = r.to_dict()
    assert d["status"] == SKIPPED
    assert d["reason"] == "needs captured tool scope"
    assert OracleResult.from_dict(d) == r


def test_fail_without_code_is_rejected():
    with pytest.raises(ValueError):
        OracleResult(oracle="X", status=FAILED, code=None)


def test_bad_status_is_rejected():
    with pytest.raises(ValueError):
        OracleResult(oracle="X", status="MAYBE")


def test_from_dict_requires_keys():
    with pytest.raises(ValueError):
        OracleResult.from_dict({"status": "PASS"})
