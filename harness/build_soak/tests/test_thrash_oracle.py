from __future__ import annotations

from harness.build_soak import failure_codes as fc
from harness.build_soak.oracles.thrash import ThrashOracle
from harness.build_soak.tests._eventlog import action, agent_error, observation, status


def _scenario(**overrides: int) -> dict:
    limits = {
        "max_identical_action_repeats": 2,
        "max_same_tool_error_repeats": 2,
        "max_actionless_pauses": 0,
        "max_same_model_repair_repeats": 1,
        "max_total_model_repairs": 3,
    }
    limits.update(overrides)
    return {"id": "thrash", "assertions": {"thrash": limits}}


def test_identical_tool_call_streak_fails() -> None:
    events = []
    for seq in (1, 3, 5):
        action_id = f"a{seq}"
        a = action(seq, "file_read", action_id=action_id, args={"path": "same.txt"})
        events += [a, observation(seq + 1, action_id, tool="file_read", success=True)]
    result = ThrashOracle().check(events, scenario=_scenario())[0]
    assert result.code == fc.TOOL_CALL_THRASH
    assert result.facts["action_seqs"] == [1, 3, 5]


def test_repeated_tool_schema_error_fails_even_with_other_calls_between() -> None:
    events = []
    for seq in (1, 5, 9):
        bad_id = f"bad{seq}"
        other_id = f"other{seq}"
        bad = action(seq, "exact_replace", action_id=bad_id)
        other = action(seq + 2, "file_list", action_id=other_id)
        events += [
            bad,
            agent_error(
                seq + 1,
                bad_id,
                error="missing required field old_text at item 123",
            ),
            other,
            observation(seq + 3, other_id, tool="file_list", success=True),
        ]
    result = ThrashOracle().check(events, scenario=_scenario())[0]
    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["tool"] == "exact_replace"
    assert result.facts["count"] == 3


def test_actionless_pause_is_a_failure_even_if_run_later_finishes() -> None:
    result = ThrashOracle().check(
        [status(1, "PAUSED", detail="actionless"), status(2, "FINISHED")],
        scenario=_scenario(),
    )[0]
    assert result.code == fc.ACTIONLESS_THRASH


def test_product_stuck_marker_is_classified_as_thrash() -> None:
    result = ThrashOracle().check(
        [status(1, "STUCK", detail="repeated_action_observation")],
        scenario=_scenario(),
    )[0]
    assert result.code == fc.TOOL_CALL_THRASH


def test_repeated_hidden_provider_repair_fails_from_inspect_trace() -> None:
    trace = {
        "spans": [
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": "provider_rejected_request",
                "attempt": attempt,
            }
            for attempt in (1, 2)
        ]
    }
    result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]
    assert result.code == fc.MODEL_REPAIR_THRASH
    assert result.facts["repair_kind"] == "provider_rejected_request"
    assert result.facts["count"] == 2


def test_excessive_mixed_hidden_repairs_fail() -> None:
    trace = {
        "spans": [
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": kind,
                "attempt": 1,
            }
            for kind in (
                "provider_rejected_request",
                "unknown_tool",
                "empty_reasoning",
                "prose_without_action",
            )
        ]
    }
    result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]
    assert result.code == fc.MODEL_REPAIR_THRASH
    assert result.facts["count"] == 4


def test_one_hidden_repair_is_reported_but_allowed() -> None:
    trace = {
        "spans": [
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": "unknown_tool",
                "attempt": 1,
                "tool_name": "filewrite",
            }
        ]
    }
    result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]
    assert result.passed
    assert result.facts["model_repair_count"] == 1


def test_legacy_scenario_without_policy_skips() -> None:
    result = ThrashOracle().check([], scenario={"id": "legacy", "assertions": {}})[0]
    assert result.skipped
