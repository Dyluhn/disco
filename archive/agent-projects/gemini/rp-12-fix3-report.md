# RP-12 FIX 3 REPORT — Requery Hang Resolution

## Hang Mechanism
The hang in `test_dc05_loop.py` (specifically `test_actionless_breaker_halts`) was caused by a combination of the new requery logic and the behavior of `ScriptedAgent`.

1. **Tool Identification Gap:** `submit_plan` and `plan_step` were missing from the `virtual_names` set in `engine.py`. Since they also don't exist in the executor's tool registry, they were treated as "unknown tools" by the requery logic.
2. **Script Consumption:** The requery logic calls `await self.agent.step(...)` to attempt a correction. `ScriptedAgent` increments its call counter on every `step()` call. Requerying a scripted step effectively "swallows" that step and consumes the next one in the script.
3. **Plan Gate Bypass:** When Turn 0's `submit_plan` was requeried, `ScriptedAgent` returned Turn 1's `noop_step`. The loop then processed Turn 1 as prose. Since no `PlanEvent` was ever emitted, the loop stayed in `PLANNING` mode.
4. **Infinite Loop in Planning:** The `PLANNING` mode path for tool-less prose lacked an actionless valve check. It simply emitted a nudge message and `continue`d the outer loop. Because `ScriptedAgent` repeats its last step when exhausted, it kept returning `noop_step` or `finish_step` (which also has no tool call), leading to an infinite busy-loop.

## Fixes Applied
1. **Comprehensive Known Tools:** Updated `all_known_names` in `engine.py` to include `self._plan_tool` ("submit_plan"), `plan_step`, and all `self._planning_tools`. This ensures valid planning/bookkeeping tools never trigger a requery.
2. **Planning Mode Valve:** Added the `_actionless_valve` check to the planning mode's tool-less prose path. This ensures that even if an agent gets stuck in a "talking without planning" loop, the actionless breaker will eventually fire and halt the loop.
3. **Bounded Requery:** Confirmed that the requery logic is already bounded by `requery_count < 2`. The addition of `plan_step` to the known list prevents the specific "script exhaustion" hang during requery for that tool.

## Test Results
Ran the requested test suite with `timeout 60`:
`uv run pytest packages/core/tests/test_dc05_loop.py packages/core/tests/test_toolcall_defense.py packages/tools/tests/test_kernel_session.py -v`

```
packages/core/tests/test_dc05_loop.py ......................             [ 61%]
packages/core/tests/test_toolcall_defense.py ......                      [ 77%]
packages/tools/tests/test_kernel_session.py ........                     [100%]

============================== 36 passed in 7.49s ==============================
```

All 36 tests passed, including the new regression test `test_exhausted_scripted_agent_during_requery` in `test_toolcall_defense.py`.
