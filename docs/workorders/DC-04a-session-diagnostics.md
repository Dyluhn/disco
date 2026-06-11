# DC-04a — Actionable session diagnostics (DEFECT-2, tools-package half)

**Read `README.md` first. De-complexity Wave 0 (docs/decomplexity-wave-plan.md DC-04).
Scope = packages/tools ONLY — the DEFECT-1 (/sessions retry) half is a separate order
because it touches files another live order owns. Do NOT touch packages/core or
packages/agent-server.**

## Why (read the evidence first: test-record/marathon/DEFECTS.md, DEFECT-2)

Twice in the bp-16 marathon (attempts 3 & 5), `shell_exec` into an UNAVAILABLE
session — dead (attempt 3) or occupied by a foreground `python3 server.py`
(attempt 5) — failed 4–7× in a row with the agent seeing only
`agent_error: "tool failed"`. No diagnostic, so the agent repeated the identical
action until the 5-failure circuit breaker forced a human click. The diagnostics
EXIST — they're dropped in the relay:

1. `builtin/shell_sessions.py` tool wrappers return
   `ToolOutcome(success=False, content=str(e))` — message in **content**,
   `error=None`.
2. `executor.py` (~line 117) maps `error=outcome.error` → None.
3. `core/loop/engine.py:1337` emits `AgentErrorEvent(error=result.error or
   "tool failed")` — the content never reaches the model. (Do NOT edit
   engine.py — fixing the executor seam is sufficient and stays in-package.)

## The decided design (locked)

### 1. `packages/tools/src/perpleximanus/tools/executor.py` — the relay fix

In the final `ToolResult` construction: when `outcome.success` is False and
`outcome.error` is falsy, fall back to `outcome.content` (and only then to
None). One expression; comment it with the DEFECT-2 reference. This fixes the
opacity for EVERY tool that reports failure via content, not just shell.

### 2. `packages/tools/src/perpleximanus/tools/builtin/shell_sessions.py` — belt+braces

Every `ToolOutcome(success=False, content=…)` in this module also sets
`error=` to the same text (the contract says error is populated iff not
success — honor it at the source too). The generic `except Exception as e:
… f"Error: {e}"` stays as the last resort.

### 3. `packages/tools/src/perpleximanus/tools/sandbox/shell_sessions.py` — name WHICH state

- **Busy**: `SessionBusy` currently says "Previous command not finished…".
  Enrich it to name the occupant: query
  `tmux list-panes -t {full} -F '#{pane_current_command}'` (via
  `_run_tmux_safe`; on failure fall back to the current wording) →
  `"session 'backend' is busy running 'python3' — wait for it (shell_wait),
  interact with it (shell_write_to_process), kill it (shell_kill_process), or
  use a different session name."`
- **Dead/lost**: `view()` already distinguishes `_lost_sessions`
  ("session lost: sandbox was recreated") from generic not-found. Extend the
  same WHICH-state discipline to `exec()`: when `ensure()` raises (tmux
  create/send failing — e.g. the sandbox's tmux server is down or the exec
  transport dropped), wrap into a RuntimeError whose message names the session
  and the likely cause and the way out:
  `"session 'backend' could not be reached (sandbox shell unavailable or
  recreated) — retry once; if it persists, use a new session name or
  server_start."` Keep the original tmux stderr appended for the trace.
- Do NOT change exec()'s ensure-creates-missing-sessions semantics, the
  marker/PS1 machinery, deadlines, or any happy-path behavior.

## Acceptance ladder

1. **Unit — `packages/tools/tests/test_session_diagnostics.py`** (NEW). Use the
   process backend / a stubbed `SandboxInstance` whose `exec_shell` you script
   (the existing tools tests have patterns for this — find and follow them).
   MUST cover, asserting on the EXACT user-visible strings:
   - busy session → ToolOutcome failure whose `error` AND `content` name the
     session, the running command, and the three ways out;
   - unreachable tmux (scripted exec_shell failure) → failure naming the
     session + "retry once / new session / server_start";
   - executor relay: a stub tool returning
     `ToolOutcome(success=False, content="rich diagnosis", error=None)` →
     `ToolResult.error == "rich diagnosis"` (the DEFECT-2 regression test
     proper); success outcomes keep `error=None`.
   - replay the two archived defect SHAPES (not the full logs): a
     `shell_exec {"session": "backend"}` against a busy session and against an
     unreachable one — assert neither produces a bare "tool failed"-style
     empty error.
2. Run ONLY the tools package tests you touched plus the existing shell-session
   test files (`uv run pytest packages/tools/tests/<files> -x -q`) — never the
   whole repo, never core/agent-server suites. Log →
   `test-record/dc-04a/units.log` (create the dir).
3. **Report** — `agent-projects/sonnet/dc-04a-report.md`: what changed, verbatim
   test output, any deviation declared honestly. Do NOT commit; do NOT run git
   commands other than read-only ones.

## Manifest (orders.yaml `dc-04a` — touch nothing outside it)

- packages/tools/src/perpleximanus/tools/executor.py
- packages/tools/src/perpleximanus/tools/builtin/shell_sessions.py
- packages/tools/src/perpleximanus/tools/sandbox/shell_sessions.py
- packages/tools/tests/test_session_diagnostics.py
- test-record/dc-04a/units.log
- agent-projects/sonnet/dc-04a-report.md
