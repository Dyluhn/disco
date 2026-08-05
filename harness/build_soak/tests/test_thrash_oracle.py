from __future__ import annotations

import pytest

from harness.build_soak import failure_codes as fc
from harness.build_soak.oracles.thrash import ThrashOracle
from harness.build_soak.tests._eventlog import action, agent_error, msg, observation, status


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


def _approved_plan_scope(seq: int, revision: int, fingerprints: list[str]) -> dict:
    event = status(seq, "RUNNING", "plan_approved")
    event["plan_verification_transition"] = {
        "new_authority": "plan",
        "new_plan_event_id": f"plan-{revision}",
        "new_plan_revision": revision,
        "new_predicate_fingerprints": fingerprints,
        "reason": "approved_initial_plan" if revision == 1 else "approved_plan_revision",
    }
    return event


def test_identical_tool_call_streak_fails() -> None:
    events = []
    for seq in (1, 3, 5):
        action_id = f"a{seq}"
        a = action(seq, "file_read", action_id=action_id, args={"path": "same.txt"})
        events += [a, observation(seq + 1, action_id, tool="file_read", success=True)]
    result = ThrashOracle().check(events, scenario=_scenario())[0]
    assert result.code == fc.TOOL_CALL_THRASH_IDENTICAL_STREAK
    assert result.facts["action_seqs"] == [1, 3, 5]


def _successful_read(seq: int, path: str = ".disco/appspec.json") -> list[dict]:
    action_id = f"read{seq}"
    return [
        action(seq, "file_read", action_id=action_id, args={"path": path}),
        observation(seq + 1, action_id, tool="file_read", success=True),
    ]


def test_identical_reads_are_fresh_after_typed_plan_recovery_progress() -> None:
    obligation = msg(4, "environment", "retain the dropped predicate", role="user")
    obligation["meta"] = {"blocking": "plan_predicate_weakening"}
    events = (
        [_approved_plan_scope(1, 1, ["sha256:old"])]
        + _successful_read(2)
        + [obligation]
        + _successful_read(5)
        + [_approved_plan_scope(7, 2, ["sha256:old", "sha256:new"])]
        + _successful_read(8)
    )

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["longest_identical_action_streak"] == 1


def test_same_predicate_reapproval_cannot_reset_identical_action_streak() -> None:
    events = (
        [_approved_plan_scope(1, 1, ["sha256:same"])]
        + _successful_read(2)
        + [_approved_plan_scope(4, 2, ["sha256:same"])]
        + _successful_read(5)
        + [_approved_plan_scope(7, 3, ["sha256:same"])]
        + _successful_read(8)
    )

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_IDENTICAL_STREAK
    assert result.facts["action_seqs"] == [2, 5, 8]


def test_plain_environment_prose_cannot_reset_identical_action_streak() -> None:
    reminder = msg(3, "environment", "<system-reminder>keep going</system-reminder>", role="user")
    events = _successful_read(1) + [reminder] + _successful_read(4) + _successful_read(6)

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_IDENTICAL_STREAK
    assert result.facts["action_seqs"] == [1, 4, 6]


def test_typed_read_escape_boundary_keeps_third_refused_proposal_in_recovery_epoch() -> None:
    obligation = msg(
        5,
        "environment",
        "<system-reminder>take a different action</system-reminder>",
        role="user",
    )
    obligation["meta"] = {"blocking": "stuck_escape:repeated_unchanged_file_read"}
    third = action(8, "file_read", action_id="read8", args={"path": "same.txt"})
    events = (
        _successful_read(1, "same.txt")
        + _successful_read(3, "same.txt")
        + [
            obligation,
            status(6, "RUNNING", "stuck_escape_block:file_read"),
            status(7, "RUNNING", "stuck_escape"),
            third,
            agent_error(
                9,
                "read8",
                error="stuck_escape_tool_quarantine:file_read",
            ),
        ]
    )

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["longest_identical_action_streak"] == 2


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

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [1, 3, 5]
    assert result.facts["fingerprint"] == '["python","/workspace/inventory.py",[]]'


def test_h469_output_materialization_is_not_a_third_pure_script_verification() -> None:
    commands = (
        "python3 primes.py",
        r"python3 primes.py | grep -oP '\d+' | tail -1",
        "python3 primes.py | tee /tmp/primes_output.txt",
    )
    events = []
    for seq, command in zip((9, 11, 24), commands, strict=True):
        action_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 2


def test_h523_approved_replacement_predicates_scope_revised_verification() -> None:
    commands_and_outputs = (
        (
            "cd /workspace && python primes.py",
            "2\n3\n5\n7\n11\n13\n17\n19\n23\n29\n31\n37\n41\n43\n47\n53\n59\n61\n67\n71\n",
        ),
        ("cd /workspace && python primes.py | wc -l", "20\n"),
        ("cd /workspace && python primes.py | tail -1", "71\n"),
    )
    events = [_approved_plan_scope(6, 1, ["sha256:old-malformed-predicate"])]
    for seq, (command, output) in zip((9, 39, 41), commands_and_outputs, strict=True):
        if seq == 39:
            events.append(_approved_plan_scope(37, 3, []))
        action_id = f"run{seq}"
        result = observation(seq + 1, action_id, tool="shell", success=True)
        result["tool_result"]["content"] = output
        result["tool_result"]["structured"] = {
            "exit_code": 0,
            "stdout": output,
            "stderr": "",
            "timed_out": False,
            "output_truncated": False,
        }
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            result,
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 2


def test_same_predicate_reapproval_cannot_reset_cosmetic_wrapper_thrash() -> None:
    commands_and_outputs = (
        ("python inventory.py; echo nonce1", "inventory\nnonce1\n"),
        ("cd /workspace && python inventory.py; echo nonce2", "inventory\nnonce2\n"),
        ("echo before; python inventory.py; echo nonce3", "before\ninventory\nnonce3\n"),
    )
    events = []
    for revision, seq, (command, output) in zip(
        (1, 2, 3), (1, 5, 9), commands_and_outputs, strict=True
    ):
        events.append(_approved_plan_scope(seq, revision, ["sha256:same-predicate"]))
        action_id = f"run{seq + 2}"
        result = observation(seq + 3, action_id, tool="shell", success=True)
        result["tool_result"]["content"] = output
        result["tool_result"]["structured"] = {
            "exit_code": 0,
            "stdout": output,
            "stderr": "",
            "timed_out": False,
            "output_truncated": False,
        }
        events += [
            action(seq + 2, "shell", action_id=action_id, args={"command": command}),
            result,
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [3, 7, 11]


def test_exact_command_retries_remain_bounded_when_successful_output_changes() -> None:
    events = []
    for seq, output in zip((1, 5, 9), ("first\n", "second\n", "third\n"), strict=True):
        action_id = f"run{seq}"
        result = observation(seq + 1, action_id, tool="shell", success=True)
        result["tool_result"]["content"] = output
        result["tool_result"]["structured"] = {
            "exit_code": 0,
            "stdout": output,
            "stderr": "",
            "timed_out": False,
            "output_truncated": False,
        }
        events += [
            action(
                seq,
                "shell",
                action_id=action_id,
                args={"command": "cd /workspace && python inventory.py"},
            ),
            result,
        ]
        if seq < 9:
            list_id = f"list{seq}"
            events += [
                action(seq + 2, "file_list", action_id=list_id, args={"path": "/workspace"}),
                observation(seq + 3, list_id, tool="file_list", success=True),
            ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [1, 5, 9]


def test_repeated_same_static_tee_sink_remains_bounded_across_append_and_cwd_forms() -> None:
    commands = (
        "python3 primes.py | tee /workspace/primes.out",
        "python3 primes.py | tee -a primes.out",
        "cd /workspace && python3 primes.py | tee --append ./primes.out",
    )
    events = []
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"
    assert result.facts["action_seqs"] == [1, 3, 5]
    assert result.facts["fingerprint"].endswith(',{"tee_sinks":["/workspace/primes.out"]}]')


def test_distinct_static_tee_artifacts_do_not_collapse_into_one_repeat_group() -> None:
    events = []
    for seq, sink in zip((1, 3, 5), ("first.out", "second.out", "/tmp/third.out"), strict=True):
        action_id = f"run{seq}"
        events += [
            action(
                seq,
                "shell",
                action_id=action_id,
                args={"command": f"python3 primes.py | tee {sink}"},
            ),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.passed
    assert result.facts["largest_semantic_shell_repeat_group"] == 1


@pytest.mark.parametrize(
    "tee_stage",
    (
        'tee "$OUTPUT_PATH"',
        "tee",
        "tee /dev/null",
        'tee "$(mktemp)"',
        "tee output.txt | tail -1",
        "tee output.txt; true",
        "tee /definitely-missing-h469-parent/out.txt && false || true",
        "tee /definitely-missing-h469-parent/out.txt\ntrue",
        "tee --output-error=warn output.txt",
    ),
)
def test_unproven_tee_sink_cannot_evade_pure_repeat_limit(tee_stage: str) -> None:
    commands = (
        "python3 primes.py",
        "cd /workspace && python3 primes.py",
        f"python3 primes.py | {tee_stage}",
    )
    events = []
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
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

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
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

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
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

    assert result.code == fc.TOOL_CALL_THRASH_BACKGROUND_RESTART
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

    assert result.code == fc.TOOL_CALL_THRASH_BACKGROUND_RESTART
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

    assert result.code == fc.TOOL_CALL_THRASH_BACKGROUND_RESTART
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

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"


def test_quoted_ampersand_argument_remains_foreground_verification() -> None:
    commands = (
        "python3 inventory.py '&'",
        'cd /workspace && python3 inventory.py "&"',
        r"echo verify; python3 inventory.py \&",
    )
    events = []
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
    assert result.first_broken_link == "tool_call -> repeated_semantic_shell_verification"


@pytest.mark.parametrize(
    "suffix",
    ("|& tee output.log", "# lifecycle note R&D"),
)
def test_pipeline_stderr_and_comment_ampersands_remain_foreground(suffix: str) -> None:
    commands = (
        f"python3 inventory.py {suffix}",
        f"cd /workspace && python3 inventory.py {suffix}",
        f"echo verify; python3 inventory.py {suffix}",
    )
    events = []
    for seq, command in zip((1, 3, 5), commands, strict=True):
        action_id = f"run{seq}"
        events += [
            action(seq, "shell", action_id=action_id, args={"command": command}),
            observation(seq + 1, action_id, tool="shell", success=True),
        ]

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_SEMANTIC_SHELL
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
    assert result.code == fc.TOOL_CALL_THRASH_STUCK_VALVE


def _superseded_blocked_status(seq: int, detail: str = "verifier_no_progress") -> dict:
    event = status(seq, "STUCK", detail=detail)
    event["meta"] = {
        "blocked_landing": True,
        "blocked_reason": detail,
        "legacy_detail": detail,
        "legacy_status": "STUCK",
        "superseded_by_landing": True,
    }
    return event


def _blocked_question_status(seq: int, detail: str = "verifier_no_progress") -> dict:
    event = status(seq, "AWAITING_USER_QUESTION", detail=f"evt_q_{seq}")
    event["meta"] = {
        "blocked_landing": True,
        "blocked_reason": detail,
        "legacy_detail": detail,
        "legacy_status": "STUCK",
    }
    return event


def test_superseded_blocked_marker_with_user_recovery_is_not_terminal_thrash() -> None:
    result = ThrashOracle().check(
        [
            _superseded_blocked_status(1),
            _blocked_question_status(2),
            msg(3, "user", "Use the conventional repair and continue."),
            status(4, "RUNNING"),
            status(5, "FINISHED"),
        ],
        scenario=_scenario(),
    )[0]

    assert result.passed
    assert result.facts["recovered_blocked_marker_seqs"] == [1]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda events: events[0]["meta"].update(superseded_by_landing=False),
        lambda events: events[0]["meta"].update(blocked_reason="different_reason"),
        lambda events: events[1]["meta"].update(blocked_reason="different_reason"),
        lambda events: events.insert(1, status(2, "RUNNING")),
        lambda events: events.__setitem__(2, msg(3, "environment", "not a user answer")),
        lambda events: events.pop(),
    ),
)
def test_incomplete_blocked_landing_lineage_remains_thrash(mutate) -> None:
    events = [
        _superseded_blocked_status(1),
        _blocked_question_status(2),
        msg(3, "user", "continue"),
        status(4, "FINISHED"),
    ]
    mutate(events)

    result = ThrashOracle().check(events, scenario=_scenario())[0]

    assert result.code == fc.TOOL_CALL_THRASH_STUCK_VALVE


def test_later_unsuperseded_stuck_marker_still_fails() -> None:
    result = ThrashOracle().check(
        [
            _superseded_blocked_status(1),
            _blocked_question_status(2),
            msg(3, "user", "continue"),
            status(4, "FINISHED"),
            status(5, "STUCK", detail="repeated_noop"),
        ],
        scenario=_scenario(),
    )[0]

    assert result.code == fc.TOOL_CALL_THRASH_STUCK_VALVE
    assert result.facts["terminal_markers"] == [{"seq": 5, "detail": "repeated_noop"}]


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


# ---- k6g F2: error accounting is scoped to progress epochs ------------------
#
# The 128k canary's ThrashOracle finding grouped two identical
# `stuck_escape_tool_quarantine:file_read` refusals at seqs 428 and 514 into
# `count 2 > allowed 1` although a trusted mutation receipt, an approved plan
# revision, verification, and a new finish-gate obligation lay between them.
# These controls replay that exact shape hermetically (no scenario constants).


def _refused_read(seq: int, path: str) -> list[dict]:
    action_id = f"a{seq}"
    return [
        action(seq, "file_read", action_id=action_id, args={"path": path}),
        agent_error(
            seq + 1,
            action_id,
            error="stuck_escape_tool_quarantine:file_read",
            detail="withheld during the current loop",
        ),
    ]


def _receipt_write(seq: int, path: str) -> list[dict]:
    action_id = f"a{seq}"
    write = action(seq, "file_edit", action_id=action_id, args={"path": path})
    result = observation(seq + 1, action_id, tool="file_edit", success=True)
    result["tool_result"]["structured"] = {"path": path, "sha256": "a" * 64}
    return [write, result]


def test_identical_errors_across_trusted_progress_are_not_one_repeated_behavior() -> None:
    """Positive no-false-thrash control: the exact seq-428/seq-514 history."""
    events = (
        _refused_read(1, "styles.css")
        + _receipt_write(3, "styles.css")
        + [_approved_plan_scope(5, 2, ["fp-1"])]
        + _receipt_write(6, "app.js")
        + _refused_read(8, "index.html")
    )
    results = ThrashOracle().check(events, scenario=_scenario(max_same_tool_error_repeats=1))
    assert [r.code for r in results if r.failed] == []
    passing = results[0]
    assert passing.facts["largest_same_tool_error_group"] == 1


def test_adjacent_identical_errors_with_no_progress_still_fail() -> None:
    """Negative control: a true consecutive repeat still stops spend."""
    events = _refused_read(1, "index.html") + _refused_read(3, "index.html")
    result = ThrashOracle().check(events, scenario=_scenario(max_same_tool_error_repeats=1))[0]
    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["count"] == 2
    assert result.facts["action_seqs"] == [1, 3]


def test_user_turn_and_blocking_obligation_split_progress_epochs() -> None:
    user_turn = {
        "id": "m5",
        "seq": 5,
        "kind": "message",
        "source": "user",
        "message": {"role": "user", "content": "proceed with sensible defaults"},
    }
    obligation = {
        "id": "m6",
        "seq": 6,
        "kind": "message",
        "source": "environment",
        "meta": {"blocking": "user_literal_missing"},
        "message": {"role": "user", "content": "a required literal is missing"},
    }
    for boundary in (user_turn, obligation):
        events = _refused_read(1, "index.html") + [boundary] + _refused_read(7, "index.html")
        results = ThrashOracle().check(events, scenario=_scenario(max_same_tool_error_repeats=1))
        assert [r.code for r in results if r.failed] == [], boundary["id"]


def test_plain_environment_prose_is_not_a_progress_boundary() -> None:
    reminder = {
        "id": "m5",
        "seq": 5,
        "kind": "message",
        "source": "environment",
        "message": {"role": "user", "content": "<system-reminder>keep going</system-reminder>"},
    }
    events = _refused_read(1, "index.html") + [reminder] + _refused_read(7, "index.html")
    result = ThrashOracle().check(events, scenario=_scenario(max_same_tool_error_repeats=1))[0]
    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["count"] == 2


def test_receiptless_success_is_not_a_progress_boundary() -> None:
    action_id = "a5"
    hollow = [
        action(5, "file_edit", action_id=action_id, args={"path": "styles.css"}),
        observation(6, action_id, tool="file_edit", success=True),
    ]
    events = _refused_read(1, "index.html") + hollow + _refused_read(7, "index.html")
    result = ThrashOracle().check(events, scenario=_scenario(max_same_tool_error_repeats=1))[0]
    assert result.code == fc.TOOL_ERROR_THRASH
    assert result.facts["count"] == 2


def test_v3_receipt_trust_matches_the_product_boundary() -> None:
    """Verifier V3: degenerate/unpaired/foreign-shape receipts are not epochs."""

    def check(events: list[dict]) -> object:
        return ThrashOracle().check(events, scenario=_scenario(max_same_tool_error_repeats=1))[0]

    def boundary_events(middle: list[dict]) -> list[dict]:
        return _refused_read(1, "index.html") + middle + _refused_read(9, "index.html")

    # applied=[""] is degenerate; applied on a non-run_project_script tool is a
    # foreign shape; path+sha256 on a non-file tool is a foreign shape; a
    # receipt on an UNPAIRED observation (no matching action event) is inert.
    hollow_applied = [
        action(5, "run_project_script", action_id="a5", args={"name": "build"}),
        observation(6, "a5", tool="run_project_script", success=True),
    ]
    hollow_applied[1]["tool_result"]["structured"] = {"applied": [""]}
    foreign_applied = [
        action(5, "shell", action_id="a5", args={"command": "make"}),
        observation(6, "a5", tool="shell", success=True),
    ]
    foreign_applied[1]["tool_result"]["structured"] = {"applied": ["step"]}
    foreign_sha = [
        action(5, "shell", action_id="a5", args={"command": "make"}),
        observation(6, "a5", tool="shell", success=True),
    ]
    foreign_sha[1]["tool_result"]["structured"] = {"path": "x", "sha256": "a" * 64}
    unpaired = [observation(6, "ghost-action", tool="file_edit", success=True)]
    unpaired[0]["tool_result"]["structured"] = {"path": "x", "sha256": "a" * 64}

    for middle in (hollow_applied, foreign_applied, foreign_sha, unpaired):
        result = check(boundary_events(middle))
        assert result.code == fc.TOOL_ERROR_THRASH, middle
        assert result.facts["count"] == 2

    # The genuine shapes still split epochs.
    real_applied = [
        action(5, "run_project_script", action_id="a5", args={"name": "build"}),
        observation(6, "a5", tool="run_project_script", success=True),
    ]
    real_applied[1]["tool_result"]["structured"] = {"applied": ["postprocess"]}
    results = ThrashOracle().check(
        boundary_events(real_applied), scenario=_scenario(max_same_tool_error_repeats=1)
    )
    assert [r.code for r in results if r.failed] == []


def _repair(kind: str, **extra: object) -> dict[str, object]:
    return {"span": "agent.repair", "event": "point", "repair_kind": kind, **extra}


def test_one_first_repair_per_drive_is_not_repeated_behaviour() -> None:
    """Counted idx 022: two drives, each spending its one legal repair, is not thrash.

    `attempt` is 1-based within ONE drive's budget (`drive_step` re-initialises the
    counters), so two `attempt: 1` records are two independent first recoveries
    separated by a user turn. Lifetime counting reported them as one repeated
    behaviour and failed a run in which the product did exactly what it was built
    to do.
    """

    trace = {"spans": [_repair("empty_reasoning", attempt=1) for _ in range(2)]}
    result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]

    assert result.passed
    assert result.facts["model_repair_count"] == 2


def test_escalating_repair_within_one_drive_still_fails() -> None:
    """Negative control: real escalation inside one budget must still stop the run."""

    trace = {"spans": [_repair("empty_reasoning", attempt=depth) for depth in (1, 2)]}
    result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]

    assert result.code == fc.MODEL_REPAIR_THRASH
    assert result.facts["repair_kind"] == "empty_reasoning"
    assert result.facts["count"] == 2


def test_repairs_without_an_attempt_are_counted_the_old_way() -> None:
    """Negative control: an unattributable repair must never be waved through.

    A span with no usable `attempt` cannot be tied to a drive's budget, so the
    oracle keeps counting occurrences for it. This fix narrows what counts as
    thrash; it must not open a hole for span shapes that predate the field.
    """

    for bad in ({}, {"attempt": 0}, {"attempt": "1"}, {"attempt": True}, {"attempt": None}):
        trace = {"spans": [_repair("unknown_tool", **bad) for _ in range(2)]}
        result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]

        assert result.code == fc.MODEL_REPAIR_THRASH, bad
        assert result.facts["count"] == 2, bad


def test_first_repairs_across_many_drives_still_hit_the_total_bound() -> None:
    """The lifetime bound is the backstop the per-drive rule leans on.

    Four honest first attempts are individually legal but collectively excessive;
    `max_total_model_repairs` must still catch that, or narrowing the same-kind
    rule would let a run repair forever one drive at a time.
    """

    trace = {"spans": [_repair("empty_reasoning", attempt=1) for _ in range(4)]}
    result = ThrashOracle().check([], scenario=_scenario(), inspect_trace=trace)[0]

    assert result.code == fc.MODEL_REPAIR_THRASH
    assert result.facts["count"] == 4
