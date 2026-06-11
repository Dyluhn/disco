# DC-04a — Session Diagnostics Report

## Summary

All three changes from the decided design are implemented. 15 tests pass.
No deviations from the spec.

---

## Changes Made

### 1. `packages/tools/src/perpleximanus/tools/executor.py`

**The relay fix (DEFECT-2 root cause).**

In `DefaultToolExecutor.execute()`, the final `ToolResult` construction changed
one field:

```python
# before
error=outcome.error,

# after
# DEFECT-2: tools report failure via content, not error; fall back so
# AgentErrorEvent receives the diagnosis instead of bare "tool failed".
error=outcome.error or (None if outcome.success else outcome.content),
```

Logic: when `outcome.success` is False and `outcome.error` is falsy, the
`content` field is used as the error. Success outcomes are unaffected
(their `error` is already None, and `None or None == None`). This fixes
opacity for **every** tool that reports failure via `content` rather than
`error`, not just shell tools.

---

### 2. `packages/tools/src/perpleximanus/tools/builtin/shell_sessions.py`

**Belt+braces: every `ToolOutcome(success=False, ...)` now also sets `error=`.**

All five tools (ShellExecTool, ShellViewTool, ShellWaitTool, ShellWriteTool,
ShellKillTool) had their failure returns updated to mirror `content` into
`error`. Examples:

```python
# before
return ToolOutcome(success=False, content=str(e))
return ToolOutcome(success=False, content=f"Error: {e}")
return ToolOutcome(success=False, content="Session manager not available.")

# after
return ToolOutcome(success=False, content=str(e), error=str(e))
return ToolOutcome(success=False, content=f"Error: {e}", error=f"Error: {e}")
return ToolOutcome(success=False, content="Session manager not available.", error="Session manager not available.")
```

11 `ToolOutcome` calls updated in total (across the five tools).

---

### 3. `packages/tools/src/perpleximanus/tools/sandbox/shell_sessions.py`

**Two changes to `ShellSessionManager.exec()`.**

#### 3a. Unreachable session — wraps `ensure()` failure

```python
try:
    await self.ensure(name, exec_dir)
except Exception as e:
    raise RuntimeError(
        f"session '{name}' could not be reached (sandbox shell unavailable or "
        f"recreated) — retry once; if it persists, use a new session name or "
        f"server_start. {e}"
    ) from e
```

When `ensure()` raises (e.g. the tmux server is gone, the sandbox ssh pipe
is broken), the raw tmux error is wrapped into a diagnostic RuntimeError
naming the session and the three recovery options. The original exception is
appended and chained (`from e`).

#### 3b. Busy session — queries the occupant command

```python
if await self.is_busy(name):
    full = self._full_name(name)
    rc, pane_out = await self._run_tmux_safe(
        f"list-panes -t {shlex.quote(full)} -F '#{{pane_current_command}}'"
    )
    cmd_name = (
        pane_out.strip().splitlines()[0].strip()
        if rc == 0 and pane_out.strip()
        else ""
    )
    if cmd_name:
        raise SessionBusy(
            f"session '{name}' is busy running '{cmd_name}' — "
            "wait for it (shell_wait), "
            "interact with it (shell_write_to_process), "
            "kill it (shell_kill_process), "
            "or use a different session name."
        )
    raise SessionBusy(
        f"Previous command not finished in session '{name}'. Wait for it (shell_wait), "
        "interact with it (shell_write_to_process), kill it (shell_kill_process), "
        "or use a different session name."
    )
```

`list-panes` is queried via `_run_tmux_safe` (no raise on failure). If it
returns a command name, the enriched message is used; otherwise the original
wording is the fallback. The existing `test_busy_detection` test in
`test_shell_sessions.py` passes unchanged because its `FakeInstance` returns
empty stdout for `list-panes` → `cmd_name = ""` → fallback message.

---

## Test File

`packages/tools/tests/test_session_diagnostics.py` — 9 new tests:

| # | Test name | What it covers |
|---|-----------|----------------|
| 1 | `test_busy_session_names_running_command` | sandbox: SessionBusy message contains session name, 'python3', all three ways out |
| 2 | `test_busy_session_fallback_when_list_panes_fails` | sandbox: fallback when list-panes fails — no crash |
| 3 | `test_unreachable_tmux_names_session_and_recovery` | sandbox: ensure() failure → RuntimeError names session + "retry once" + "server_start" |
| 4 | `test_busy_tooloutcome_carries_error_and_content` | builtin: SessionBusy → ToolOutcome.error == ToolOutcome.content, naming session/command/ways-out |
| 5 | `test_unreachable_tooloutcome_carries_error_and_content` | builtin: RuntimeError → ToolOutcome.error == ToolOutcome.content, naming session + recovery path |
| 6 | `test_executor_relay_error_falls_back_to_content` | **DEFECT-2 regression**: ToolOutcome(success=False, content="rich diagnosis", error=None) → ToolResult.error == "rich diagnosis" |
| 7 | `test_executor_relay_success_keeps_error_none` | Success outcomes: ToolResult.error is None |
| 8 | `test_defect_shape_busy_backend_no_bare_tool_failed` | Attempt-5 shape: busy backend → error non-empty, not "tool failed" |
| 9 | `test_defect_shape_unreachable_backend_no_bare_tool_failed` | Attempt-3 shape: dead backend → error non-empty, not "tool failed" |

---

## Test Output

```
============================= test session starts ==============================
collected 15 items

packages/tools/tests/test_session_diagnostics.py .........               [ 60%]
packages/tools/tests/test_shell_sessions.py ......                       [100%]

============================= 15 passed in 59.45s ==============================
```

15 tests pass: 9 new (`test_session_diagnostics.py`) + 6 pre-existing
(`test_shell_sessions.py`, including the integration test). The pre-existing
`test_busy_detection` test (which asserted the old `SessionBusy` wording
verbatim) continues to pass because the fallback path preserves that wording.

---

## Deviations

None. All three design items implemented as specified. No files outside the
manifest were touched.
