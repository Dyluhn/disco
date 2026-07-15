from __future__ import annotations

import pytest

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


def test_semantically_repeated_direct_script_verification_fails_across_shell_wrappers() -> None:
    commands = (
        "cd /workspace && python inventory.py",
        'cd /workspace && python inventory.py && echo "EXIT CODE: $?"',
        'cd /workspace && ls -la inventory.py && echo "---" && python inventory.py',
    )
    events = []
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"a{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [1, 3, 5]


def test_semantic_script_repeats_do_not_equate_changed_args_or_module_runners() -> None:
    commands = (
        "python inventory.py --format text",
        "python inventory.py --format json",
        "python -m pytest",
        "cd /workspace && python -m pytest",
        'echo "run tests" && python -m pytest',
        "deno run verify.ts",
        "cd /workspace && deno run verify.ts",
        'echo "verify" && deno run verify.ts',
    )
    events = []
    for index, command in enumerate(commands):
        seq = index * 2 + 1
        action_id = f"a{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 1


def test_successful_same_family_source_edit_starts_a_new_verification_generation() -> None:
    events = []
    for seq in (1, 7, 13):
        run_id = f"run{seq}"
        write_id = f"write{seq}"
        events += [
            action(
                seq,
                "file_write",
                action_id=write_id,
                args={"path": "/workspace/helpers.py", "content": f"# revision {seq}"},
            ),
            observation(seq + 1, write_id, tool="file_write", success=True),
            action(
                seq + 2,
                "shell",
                action_id=run_id,
                args={"command": "python inventory.py"},
            ),
            observation(seq + 3, run_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 1


def test_documentation_write_does_not_hide_repeated_script_verification() -> None:
    commands = (
        "python inventory.py",
        "cd /workspace && python inventory.py",
        'echo "verify" && python inventory.py',
    )
    events = []
    for seq, command in zip((1, 5, 9), commands, strict=True):
        run_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=run_id, args={"command": command}),
            observation(seq + 1, run_id, tool="shell", success=True),
        ]
        if seq < 9:
            write_id = f"report{seq}"
            events += [
                action(
                    seq + 2,
                    "file_write",
                    action_id=write_id,
                    args={"path": "/workspace/task-report.md", "content": f"report {seq}"},
                ),
                observation(seq + 3, write_id, tool="file_write", success=True),
            ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [1, 5, 9]


def test_failed_semantic_script_rewrite_does_not_hide_repeated_verification() -> None:
    commands = (
        "python inventory.py",
        "cd /workspace && python inventory.py",
        'echo "verify" && python inventory.py',
    )
    events = []
    for seq, command in zip((1, 5, 7), commands, strict=True):
        run_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=run_id, args={"command": command}),
            observation(seq + 1, run_id, tool="shell", success=True),
        ]
        if seq == 1:
            write_id = "failed-write"
            events += [
                action(
                    3,
                    "file_write",
                    action_id=write_id,
                    args={"path": "/workspace/inventory.py", "content": "# did not apply"},
                ),
                observation(4, write_id, tool="file_write", success=False),
            ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [1, 5, 7]


def test_generated_verify_probe_does_not_count_as_model_semantic_repeat() -> None:
    commands = (
        "python inventory.py",
        "cd /workspace && python inventory.py",
        "python inventory.py",
    )
    events = []
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"a{seq}"
        run = action(
            seq,
            "shell",
            action_id=action_id,
            args={"command": command},
        )
        if seq == 5:
            run["meta"] = {"verify_probe": True}
        events += [run, observation(seq + 1, action_id, tool="shell", success=True)]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 2


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


@pytest.mark.parametrize("detail", ["repeated_action_observation", "verifier_no_progress"])
def test_product_stuck_marker_is_classified_as_thrash(detail: str) -> None:
    result = ThrashOracle().check(
        [status(1, "STUCK", detail=detail)],
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
