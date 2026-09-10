# Finish-Gate Fix Findings

## AppKit Signal

- Keyed on the existing strict AppKit tool-surface signal: `verify_appkit_app` in `executor.available_tools()`.
- I did not add a new appkit flag. I also avoided using the broader `appkit_phase` executor attribute for these fixes because custom-build widening can retain phase state while restoring the raw action space.

## Fixes

- Dictated-content gate: `dictated_content_gate_passed()` now scopes out strict AppKit builds, so quoted tool-argument literals such as `'editorial-ledger'` are not checked against static-web deliverable paths that do not exist in the Vite/AppKit tree.
- Finish verify probe: `finish_verify_passed()` now pass-throughs under strict AppKit scope instead of emitting a `shell` probe. The shared `run_finish_verify_gates()` path still drives/consumes `verify_appkit_app`, so failing AppKit primitive verification still refuses finish.
- Execution nudge: added `finish_intent_replan_after_prior_productive_work()` and wired it into `gate_execution_nudge()`. A replan whose remaining steps are finish-only can finish if productive work occurred in the prior approved segment. A genuinely never-executed finish-only plan still hits `approve_plan_no_execution`.

## Tests Added

- `current/packages/core/tests/test_verify_appkit_gate.py::test_appkit_deadlock_sequence_replan_to_finish_terminates_cleanly`
- `current/packages/core/tests/test_verify_appkit_gate.py::test_appkit_finish_verify_arg_defers_to_appkit_verifier_without_shell`
- `current/packages/core/tests/test_verify_appkit_gate.py::test_appkit_failing_primitive_verify_refuses_finish`
- `current/packages/core/tests/test_dictated_content_finish_gate.py::test_non_appkit_dictated_content_literal_miss_still_refuses_then_cap_releases`
- `current/packages/core/tests/test_w5_execution_nudge_cap.py::test_genuinely_unexecuted_finish_only_plan_still_stucks`

## Test Runs

Environment:

```bash
WT=/var/home/dylan/projects/disclaude-wt-finishfix
PY=/var/home/dylan/projects/disclaude/.venv/bin/python3
PYTHONPATH="$WT/packages/core/src:$WT/packages/retrieval/src:$WT/packages/tools/src:$WT/packages/agent-server/src:$WT/packages/app-server/src"
```

Focused finish-gate tests:

```text
$PY -m pytest $WT/packages/core/tests/test_verify_appkit_gate.py \
  $WT/packages/core/tests/test_dictated_content_finish_gate.py \
  $WT/packages/core/tests/test_w5_execution_nudge_cap.py -q
....................                                                     [100%]
```

Core suite as specified was attempted. It hangs in existing command-predicate tests unrelated to this finish-gate change:

```text
$PY -m pytest $WT/packages/core/tests -m "not integration" -q -o faulthandler_timeout=180
... [15%]
Timeout (0:03:00)! active async test around:
current/packages/core/tests/test_c18_plan_step_done_condition.py::test_c18_sandboxless_command_falls_back_to_subprocess

$PY -m pytest $WT/packages/core/tests/test_c1c_dod_gate_wiring.py -vv -o faulthandler_timeout=60
...
current/packages/core/tests/test_c1c_dod_gate_wiring.py::test_command_predicate_arms_in_v2
Timeout (0:01:00)!
```

Core suite excluding those two pre-existing hang nodes:

```text
$PY -m pytest $WT/packages/core/tests -m "not integration" \
  -k "not test_c18_sandboxless_command_falls_back_to_subprocess and not test_command_predicate_arms_in_v2" \
  -q -o faulthandler_timeout=180
........................................................................ [ 98%]
...........................................                              [100%]
```

Tools suite as specified was attempted but is not runnable end-to-end in this restricted sandbox:

```text
$PY -m pytest $WT/packages/tools/tests -m "not integration" -vv --maxfail=2
current/packages/tools/tests/test_agent_tools.py::test_code_exec_python_state_persists_across_cells FAILED
current/packages/tools/tests/test_agent_tools.py::test_code_exec_erroring_cell_keeps_prior_state FAILED
PermissionError: [Errno 1] Operation not permitted
```

Additional tools hangs observed after excluding the socket-dependent code-exec tests:

```text
current/packages/tools/tests/test_bp_g10_filtered_default.py::test_gvisor_filtered_path_unchanged_no_regression
Timeout (0:01:00)!

current/packages/tools/tests/test_cluster4_filesystem.py::test_file_read_whole_file_unchanged
Timeout (0:02:00)!
```

Targeted tool-layer AppKit tests:

```text
$PY -m pytest $WT/packages/tools/tests/test_appkit_scope_enforcement.py \
  $WT/packages/tools/tests/test_wo_a3_verify_dispatch.py -q
....................                                                     [100%]
```

Agent-server suite as specified was attempted but hangs in existing Starlette `TestClient` route tests:

```text
$PY -m pytest $WT/packages/agent-server/tests -m "not integration" -q -o faulthandler_timeout=180
...................
Timeout (0:03:00)!
current/packages/agent-server/tests/test_activity.py::test_activity_lists_running_tasks_and_count

$PY -m pytest $WT/packages/agent-server/tests -m "not integration" \
  --ignore=$WT/packages/agent-server/tests/test_activity.py -q -o faulthandler_timeout=180
...
Timeout (0:03:00)!
current/packages/agent-server/tests/test_artifact_mode.py::test_artifact_mode_flag_round_trips_from_body
```

Targeted agent-server AppKit/build wiring tests:

```text
$PY -m pytest $WT/packages/agent-server/tests/test_appkit_mode_wiring.py \
  $WT/packages/agent-server/tests/test_build_contract_activation.py -q
.................                                                        [100%]
```

Syntax check:

```text
$PY -m py_compile current/packages/core/src/disco/core/loop/finish/common.py \
  current/packages/core/src/disco/core/loop/finish/content_gates.py \
  current/packages/core/src/disco/core/loop/finish/verify_gates.py \
  current/packages/core/src/disco/core/loop/signals.py
# exit 0
```

## Commit Attempt

The requested commit command was attempted from branch `wt-finishfix`:

```text
command git add -A && command git commit --no-verify -m "Fix AppKit finish gate deadlock"
fatal: Unable to create '/var/home/dylan/projects/disclaude/.git/worktrees/disclaude-wt-finishfix/index.lock': Read-only file system
```

No files were staged because the worktree Git metadata lives outside this writable sandbox.
