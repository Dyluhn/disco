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


def test_background_server_recovery_is_not_foreground_verification_thrash() -> None:
    """H320 exact r14 shape: three starts with visible cleanup get one credit."""

    events = [
        action(
            22,
            "shell_exec",
            action_id="start22",
            args={"command": "python3 /workspace/server.py &", "session": "server"},
        ),
        observation(23, "start22", tool="shell_exec", success=True),
        action(26, "shell_kill_process", action_id="kill26", args={"session": "server"}),
        observation(27, "kill26", tool="shell_kill_process", success=True),
        action(
            28,
            "shell",
            action_id="start28",
            args={"command": "kill 88 2>/dev/null; sleep 1; python3 /workspace/server.py &"},
        ),
        observation(29, "start28", tool="shell", success=True),
        action(
            34,
            "shell",
            action_id="cleanup34",
            args={"command": "fuser -k 8000/tcp 2>/dev/null; sleep 1"},
        ),
        observation(35, "cleanup34", tool="shell", success=True),
        action(
            36,
            "shell_exec",
            action_id="start36",
            args={"command": "python3 /workspace/server.py&", "session": "server"},
        ),
        observation(37, "start36", tool="shell_exec", success=True),
    ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 0
    assert result.facts["largest_background_script_restart_group"] == 3
    assert result.facts["background_script_cleanup_credit"] == 1


def test_three_background_script_starts_without_cleanup_fail() -> None:
    events = []
    for seq in (1, 3, 5):
        action_id = f"start{seq}"
        events += [
            action(
                seq,
                "shell_exec",
                action_id=action_id,
                args={"command": "python3 server.py &", "session": f"server{seq}"},
            ),
            observation(seq + 1, action_id, tool="shell_exec", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.first_broken_link == "tool_call -> repeated_background_script_restart"
    assert result.facts["action_seqs"] == [1, 3, 5]
    assert result.facts["cleanup_credit"] == 0


def test_repeated_cleanup_cannot_hide_four_background_restarts() -> None:
    events = []
    for index, seq in enumerate((1, 7, 13, 19), start=1):
        start_id = f"start{seq}"
        events += [
            action(
                seq,
                "shell",
                action_id=start_id,
                args={"command": f"kill {80 + index} 2>/dev/null; python3 server.py &"},
            ),
            observation(seq + 1, start_id, tool="shell", success=True),
            action(
                seq + 2,
                "shell",
                action_id=f"probe{seq}",
                args={"command": f"lsof -i :{8000 + index}"},
            ),
            observation(seq + 3, f"probe{seq}", tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.first_broken_link == "tool_call -> repeated_background_script_restart"
    assert result.facts["count"] == 4
    assert result.facts["allowed"] == 3
    assert result.facts["cleanup_credit"] == 1


def test_cleanup_after_final_background_start_grants_no_credit() -> None:
    events = []
    for seq in (1, 3, 5):
        action_id = f"start{seq}"
        events += [
            action(
                seq,
                "shell_exec",
                action_id=action_id,
                args={"command": "python3 server.py &", "session": f"server{seq}"},
            ),
            observation(seq + 1, action_id, tool="shell_exec", success=True),
        ]
    events += [
        action(7, "shell", action_id="late-kill", args={"command": "pkill -f server.py"}),
        observation(8, "late-kill", tool="shell", success=True),
    ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.facts["cleanup_credit"] == 0


def test_stderr_redirection_is_not_background_lifecycle() -> None:
    events = []
    commands = (
        "python3 inventory.py 2>&1",
        "cd /workspace && python3 inventory.py 2>&1",
        "echo verify; python3 inventory.py 2>&1",
    )
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"run{seq}"
        events += [
            action(
                seq,
                "shell",
                action_id=action_id,
                args={"command": command},
            ),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"


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


def test_distinct_shell_commands_with_generic_exit_one_are_not_same_error_thrash() -> None:
    commands = (
        "kill -9 90 2>/dev/null; sleep 0.5; lsof -ti:8000",
        'curl -s http://localhost:5173/ | grep "Live Server Up"',
    )
    events = []
    for seq, command in zip((15, 25), commands, strict=True):
        action_id = f"shell{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            agent_error(seq + 1, action_id, error="command exited 1"),
        ]

    result = ThrashOracle().check(
        events,
        scenario=_scenario(max_same_tool_error_repeats=1),
    )[0]

    assert result.passed
    assert result.facts["largest_same_tool_error_group"] == 1


def test_same_exact_failed_shell_command_remains_error_thrash() -> None:
    command = 'curl -s http://localhost:5173/ | grep "Live Server Up"'
    events = [
        action(1, "shell", action_id="shell1", args={"command": command}),
        agent_error(2, "shell1", error="command exited 1"),
        action(3, "file_read", action_id="read", args={"path": "server.py"}),
        observation(4, "read", tool="file_read", success=True),
        action(5, "shell", action_id="shell5", args={"command": command}),
        agent_error(6, "shell5", error="command exited 1"),
    ]

    result = ThrashOracle().check(
        events,
        scenario=_scenario(max_same_tool_error_repeats=1),
    )[0]

    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["action_seqs"] == [1, 5]
    assert result.facts["action_signature"]


def test_distinctive_shell_error_stays_grouped_across_changed_commands() -> None:
    error = "session 'server' is busy running 'bash'"
    events = []
    for seq, command in ((1, "python3 server.py &"), (3, "python3 other.py &")):
        action_id = f"shell{seq}"
        events += [
            action(seq, "shell_exec", action_id=action_id, args={"command": command}),
            agent_error(seq + 1, action_id, error=error),
        ]

    result = ThrashOracle().check(
        events,
        scenario=_scenario(max_same_tool_error_repeats=1),
    )[0]

    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["action_seqs"] == [1, 3]
    assert result.facts["action_signature"] is None


def test_distinct_recovery_details_under_same_tool_error_are_not_thrash() -> None:
    """H314: a corrected AppKit retry must not be collapsed by its coarse code."""

    events = [
        action(34, "app_set_design", action_id="design34", args={"recipe_id": "field-notes"}),
        agent_error(
            35,
            "design34",
            error="app_set_design_refused",
            detail="provide exactly one of recipe_id (P0) or design_spec (P1).",
        ),
        action(36, "app_set_design", action_id="design36", args={"design_spec": {}}),
        agent_error(
            37,
            "design36",
            error="app_set_design_refused",
            detail="invalid design_spec: typography.heading_font must contain one font family",
        ),
    ]

    result = ThrashOracle().check(
        events,
        scenario=_scenario(max_same_tool_error_repeats=1),
    )[0]

    assert result.passed
    assert result.facts["largest_same_tool_error_group"] == 1


def test_same_recovery_detail_under_same_tool_error_remains_thrash_without_leaking_detail() -> None:
    events = []
    for seq, item in ((1, 123), (3, 456)):
        action_id = f"design{seq}"
        events += [
            action(seq, "app_set_design", action_id=action_id, args={"design_spec": {"v": item}}),
            agent_error(
                seq + 1,
                action_id,
                error="app_set_design_refused",
                detail=f"invalid design_spec: secret-marker item {item} has an invalid font stack",
            ),
        ]

    result = ThrashOracle().check(
        events,
        scenario=_scenario(max_same_tool_error_repeats=1),
    )[0]

    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["action_seqs"] == [1, 3]
    assert result.facts["detail_signature"].startswith("sha256:")
    assert "secret-marker" not in str(result.facts)


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
