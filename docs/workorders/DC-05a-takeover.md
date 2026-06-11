# DC-05a TAKEOVER — complete a partial implementation

**Read `docs/workorders/DC-05a-loop-breakers.md` FIRST — it is the
authoritative order (locked design, acceptance ladder, manifest). This file
only describes the takeover situation.**

## Situation

A prior worker (Gemini Pro) was killed by a provider quota failure mid-order.
It left a PARTIAL implementation in the working tree — do `git diff
packages/core/src/perpleximanus/core/loop/engine.py` to see exactly what it
did (~211 lines changed; the file COMPILES — `ast.parse` passes).

State observed at takeover:
- `engine.py`: substantial implementation of the brief's 5 design points
  exists (constants, LLMTransientError retry with `_DRIVER_RETRY_BACKOFFS_S`
  + `_sleep` indirection, PAUSED detail="driver-unavailable" landing are
  visibly in place around line ~1726-1743). UNVERIFIED whether all 5 points
  (actionless breaker, valve taxonomy, knowledge dedup, driver retry,
  null-deliverable guard) are complete and correct — AUDIT the diff against
  the locked design section of the brief point by point. Complete or correct
  whatever is missing/wrong. Do NOT rewrite working parts.
- `packages/core/tests/test_dc05_loop.py` (232 lines): exists but BROKEN —
  first test fails with `AgentStep ... self_assessed_risk Input should be
  'UNKNOWN', 'LOW', 'MEDIUM' or 'HIGH'` (fixture passes None into an enum
  field). Check fixtures against the REAL model shapes (read
  `packages/core/tests/test_loop_step.py` for the established fixture style
  — the brief mandates following it). Fix or rewrite the tests so the full
  required matrix from the brief's acceptance ladder is honestly covered.
- `test-record/dc-05/units-core.log`: stale (captured a mid-edit syntax
  error). Overwrite it with your final run.

## Your job

1. Audit the partial diff vs the brief's locked design (all 5 points + the
   anti-scope rules). Complete/fix; keep what is correct.
2. Make the acceptance ladder pass for real: run ONLY
   `uv run pytest packages/core/tests/test_dc05_loop.py
   packages/core/tests/test_loop_step.py
   packages/core/tests/test_loop_integration.py -x -q`
   → log to `test-record/dc-05/units-core.log`.
3. Report → `agent-projects/gemini/dc-05a-report.md` (KEEP this exact path —
   it is in the order manifest). State honestly what the prior worker had
   done, what you fixed/added, verbatim test output, deviations.
4. Same manifest as the original brief — touch nothing outside it. Do NOT
   touch packages/agent-server (dc-05b owns it). No commits; no git writes.
