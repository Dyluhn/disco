# DC-05a Takeover Report

**Completed by:** Claude Sonnet 4.6 (takeover from Gemini Pro, killed mid-order by quota failure)  
**Date:** 2026-06-10  
**Branch:** build-surface-recovery-ux

---

## Situation at takeover

The prior worker (Gemini Pro) left a substantial partial implementation in `engine.py` (~211 lines changed) and a broken test file `test_dc05_loop.py` (232 lines). The engine compiled (`ast.parse` passes). The test file failed immediately on the first test with a Pydantic validation error (`self_assessed_risk: Input should be 'UNKNOWN', 'LOW', 'MEDIUM' or 'HIGH'`). The prior worker's session ended abruptly with `TerminalQuotaError: You have exhausted your capacity on this model`.

---

## Audit: what the prior worker had done (point by point)

### 1. Actionless-step breaker ✅ correct, kept

`_ACTIONLESS_BREAK_CAP: int = 3` added as an instance attribute in `__init__` (line 729). The check in the noop path emits the correct ENVIRONMENT diagnostic and `StatusEvent(PAUSED, detail="actionless")`. An `actual tool call resets the counter` behavior is implicit via `_consecutive_noops` (counts trailing AGENT MessageEvents, broken by any ActionEvent). Correct.

### 2. Valve taxonomy ✅ correct, kept

`_actions_since_last_resume(events)` static helper added (mirrors `_plan_is_incomplete` style as specified). Both landing paths (auto-continue cap and noop-limit) correctly check `actions_since == 0` to decide PAUSED vs FINISHED. The ⚠ ENVIRONMENT message content is correct.

### 3. Knowledge dedup ✅ present but had one engine bug — fixed

sha256-based dedup with `(scope, hash(normalized))` key, seen-set built from existing KnowledgeEvents. Logic correct.

**Bug:** The `ToolResult` for the duplicate observation was constructed without the required `call_id` field — `ToolResult.call_id` is a non-optional `str` field. This caused a `ValidationError` at runtime on any duplicate remember call.

**Fix applied (1 line in engine.py):**
```python
# BEFORE (broken — missing call_id)
res = ToolResult(tool_name="remember", success=True, content="Already recorded — not stored again.")

# AFTER (fixed)
res = ToolResult(call_id=step.tool_call.call_id, tool_name="remember", success=True, content="Already recorded — not stored again.")
```

### 4. DEFECT-5: LLMTransientError retry ✅ correct, kept

`_sleep = asyncio.sleep` module-level indirection present (patchable in tests). `_DRIVER_RETRY_BACKOFFS_S = (10.0, 30.0, 90.0)` present. Inner retry loop correctly catches `LLMTransientError` before the broad `except LLMError`. After 3 retries exhausted → ENVIRONMENT message + `StatusEvent(PAUSED, detail="driver-unavailable")`. `LLMContextWindowExceeded` is re-raised first (correct priority). Non-transient `LLMError` behavior unchanged.

### 5. Null-payload deliverable guard ✅ correct, kept

`if not step.tool_call.arguments:` at the serve intercept site, with a `_LOG.debug` line. Correct.

---

## What I fixed

### A. `ToolResult` missing `call_id` (engine.py, 1 line)

See point 3 above.

### B. `test_dc05_loop.py` — full rewrite

The prior worker's test file had four categories of defects that required a complete rewrite:

**Defect 1 — `noop_step` passed `self_assessed_risk=None`**  
`AgentStep.self_assessed_risk` is a `SecurityRisk` enum with default `SecurityRisk.UNKNOWN`. Passing `None` fails Pydantic validation. Fix: remove the argument (use default).

**Defect 2 — `state.status_detail` does not exist on `ConversationState`**  
`ConversationState.reconstruct` does not project StatusEvent `detail` into a named field. Asserting `state.status_detail == "..."` raises `AttributeError`. Fix: added `_last_status_detail(events)` helper that reads the `detail` of the last `StatusEvent` from the event log.

**Defect 3 — Valve tests were structurally blocked before the auto-continue path**  
Both valve tests used `_planning_tools = frozenset(["file_read"])` and `loop.approve_plan()`. With `_planning_tools` configured, the PLAN-MODE EXECUTION GATE (engine line ~1934) blocks any `finish_step()` call when `_productive_action_since_approval(events)` is False — which is always True for the zero-actions case. The auto-continue path was therefore unreachable in those tests. Fix: redesigned both valve tests to use a pre-seeded `SqliteEventStore` (matching the established `test_finished_with_incomplete_plan_auto_continues_then_lands_finished` pattern in `test_loop_step.py`) without `_planning_tools`, so the execution gate is inert.

**Defect 4 — Transient error test used callables that ScriptedAgent only invoked once**  
The test put `raising_step` (a closure raising on the first 2 calls) at index 0 of `[raising_step, finish_step()]`. Since `ScriptedAgent.calls` increments on every `step()` invocation, the first retry (i=1) returned `finish_step()` instead of invoking `raising_step` again — only 1 transient error, not 2. Fix: replaced callables with `LLMTransientError("unavailable")` instances directly in the script list; `ScriptedAgent` raises these via its `isinstance(item, BaseException)` check. For the persistent case, a single-element list causes `min(i, 0) == 0` on every retry, so all 4 calls use the same exception instance. Also split the test into two separate functions for clarity.

---

## Deviations from the locked design

1. **`_ACTIONLESS_BREAK_CAP` is an instance attribute, not a class constant.** Brief specifies a class constant. Prior worker placed it in `__init__`. Functionally identical. Left as-is per anti-scope rule.

2. **`import hashlib` is inside the loop body** (at the remember intercept site). Python caches imports so there is no runtime cost. Left as-is.

---

## Test output (verbatim)

```
============================= test session starts ==============================
platform linux -- Python 3.13.13, pytest-9.0.3, pluggy-1.6.0
rootdir: /var/home/dylan/projects/perpleximanus build
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.13.0, hypothesis-6.155.2
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 34 items

packages/core/tests/test_dc05_loop.py .........                          [ 26%]
packages/core/tests/test_loop_step.py .......................            [ 94%]
packages/core/tests/test_loop_integration.py ..                          [100%]

============================== 34 passed in 0.12s ==============================
```

---

## Manifest compliance

Files touched (all within the `dc-05a` manifest):

| File | Action |
|---|---|
| `packages/core/src/perpleximanus/core/loop/engine.py` | 1-line fix: `call_id=step.tool_call.call_id` in ToolResult constructor |
| `packages/core/tests/test_dc05_loop.py` | Full rewrite: fixed fixtures, status-detail assertions, valve tests, transient-error tests |
| `test-record/dc-05/units-core.log` | Created (directory + file) |
| `agent-projects/gemini/dc-05a-report.md` | Created (this file, overwrote Gemini quota-error stub) |

`packages/agent-server` — **not touched** (dc-05b scope).  
No commits made. No git writes beyond the four manifest files.
