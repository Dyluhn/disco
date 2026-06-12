# DC-05b Report — resume-path context reconstruction (DEFECT-4 root cause)

**Date:** 2026-06-10  
**Branch:** build-surface-recovery-ux  
**Scope:** packages/agent-server ONLY (packages/core untouched)

---

## What changed

### `packages/agent-server/src/disco/agent_server/runtime.py`

**Import added:**
- `AgentErrorEvent` added to the `from disco.core import (...)` block — needed to detect resolved (error-paired) actions when scanning for dangling ones.

**New private method — `_reconstruct_resume_context(self, conversation_id, events) -> list`** (inserted immediately before `resume_conversation`):

Implements all three pieces from the decided design, in order:

1. **Dangling-action synthesis** — scans `events` for `ActionEvent`s whose `.id` has no matching `ObservationEvent.action_id` or `AgentErrorEvent.action_id` later in the log. For each (normally 0 or 1 at SIGTERM), emits an `ObservationEvent` with:
   - `action_id = action.id` (pairs with the ActionEvent in `View.of`)
   - `tool_result.call_id = action.tool_call.call_id` (pairs the tool-role message)
   - `tool_result.success = False`
   - `content = "<system-reminder>This action was interrupted by a server restart — its outcome is UNKNOWN. Re-verify its effect before assuming it completed.</system-reminder>"`

2. **Environment reality block** — one `ENVIRONMENT` `MessageEvent` (role=`user`) whose content begins with `"Resumed by user."` (preserving the existing substring the test_resume.py legality tests check for) and continues with:
   - The sandbox-reclaim notice
   - The file listing from `store.iter_workspace(cid)[:30]`, relative to the workspace root; falls back to `"No saved files — the workspace starts empty."` when the store is absent, status is not OK, or the workspace is missing
   - The sessions line from `await self.sessions_snapshot(cid)` (already degrades gracefully per DEFECT-1 fix)

3. **Plan restatement** — appended to the same reality-block message: walks `events` for the latest `PlanEvent` (by revision), then collects `plan_step(index, state="done")` `ActionEvent`s emitted after the plan's seq, and finds the first 1-indexed step not in that done set. Appends `"Next actionable step (N): '<title>'. Do not re-plan and do not summarize — execute this step now using tools."`. Omitted when no plan exists or all steps are marked done.

**Modified — `resume_conversation`:**
- Events are now fetched unconditionally after the early-return guards (was: only fetched for the IDLE branch). This makes the same slice available to both the legality check and `_reconstruct_resume_context`.
- The old single `await self._store.append(..., MessageEvent(content="Resumed by user."))` is replaced with:
  ```python
  new_events = await self._reconstruct_resume_context(conversation_id, events)
  for event in new_events:
      await self._store.append(conversation_id, event)
  ```
  followed by the unchanged cancel-flag clear and RUNNING flip.

### `packages/agent-server/tests/test_resume_reconstruction.py` (NEW)

12 tests covering the full acceptance ladder:

| Test | What it asserts |
|---|---|
| `test_dangling_action_gets_synthesized_observation` | 1 synthetic `ObservationEvent`, correctly paired by `action_id` + `call_id`, containing the "interrupted by a server restart" text; precedes the `MessageEvent` |
| `test_no_dangling_action_means_no_synthetic_observation` | Paired `ActionEvent`+`ObservationEvent` → no synthetic event added |
| `test_agent_error_resolves_dangling_action` | `AgentErrorEvent.action_id` also counts as resolved; no synthetic emitted |
| `test_reality_block_contains_sandbox_sentence` | "sandbox was reclaimed" and "Resumed by user." present |
| `test_reality_block_contains_restore_files` | Seeded `tmp_path` project store → `app.py` and `App.jsx` in content |
| `test_reality_block_no_snapshot_shows_empty_message` | No project store → "No saved files — the workspace starts empty." |
| `test_reality_block_sessions_line_no_sandbox` | No live session → "No shell sessions are running." |
| `test_plan_restatement_names_first_undone_step` | Steps 1–2 done of 4 → content contains "Step C" and "(3)" |
| `test_plan_restatement_absent_when_no_plan` | No `PlanEvent` → "Next actionable step" absent |
| `test_plan_restatement_absent_when_all_steps_done` | All steps done → restatement absent |
| `test_defect4_replay` | Archived real event log sliced to first PAUSED → synthesized obs for `evt_3403bf01550d49fcae08af46bc548aed`, reality block present, "(2)" + "Scaffold Vite + React frontend" in content |
| `test_reconstruction_events_land_before_running_flip` | Integration: `resume_conversation` → obs and reality msg appear in store before the `RUNNING` `StatusEvent` |

---

## Verbatim test output

```
============================= test session starts ==============================
platform linux -- Python 3.13.13, pytest-8.0.3, pluggy-1.6.0
rootdir: /var/home/dylan/projects/disco build
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.13.0, hypothesis-6.155.2
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 42 items

packages/agent-server/tests/test_resume_reconstruction.py ............   [ 28%]
packages/agent-server/tests/test_resume.py ............                  [ 57%]
packages/agent-server/tests/test_lifecycle.py ..................         [100%]

============================== 42 passed in 0.31s ==============================
```

Tests collected from `test_resume_reconstruction.py`:
```
test_dangling_action_gets_synthesized_observation
test_no_dangling_action_means_no_synthetic_observation
test_agent_error_resolves_dangling_action
test_reality_block_contains_sandbox_sentence
test_reality_block_contains_restore_files
test_reality_block_no_snapshot_shows_empty_message
test_reality_block_sessions_line_no_sandbox
test_plan_restatement_names_first_undone_step
test_plan_restatement_absent_when_no_plan
test_plan_restatement_absent_when_all_steps_done
test_defect4_replay
test_reconstruction_events_land_before_running_flip
```

42 passed · 0 failed · 0 skipped.

---

## Deviations declared

**None.**

The existing `test_resume.py::test_resume_appends_environment_message_exactly_once` checks for `"Resumed by user." in e.message.content`. The new reality-block content begins with `"Resumed by user. Current environment reality after interruption:"` — this substring match continues to hold, so the test required no modification and passes unchanged.

The `_reconstruct_resume_context` method is added before `resume_conversation` (within the `ConversationRuntime` class, inside the same file), consistent with the manifest constraint of touching only listed files.

The `Event` type alias was not added to the import block (no annotation in the runtime module required it — existing code uses untyped `list` for event lists). `AgentErrorEvent` was the only new addition to the core import.
