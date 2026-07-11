# RP-03 Follow-up #1 — Completion Report

**Date:** 2026-06-11
**Status:** COMPLETE

## Deviations from plan

1. **Synthesis.py `str.format` KeyError (BLOCKER — FIXED).**
   The `_SECTION_PROMPT` added in the prior session contained literal JSON
   curly braces (`{`, `}`) that Python's `str.format()` interpreted as
   format placeholders. When called via engine.py's RP-04 call shape
   (`namespace=per-subq`, `section_id=hash-derived`), the `.format(topic=,
   passages=)` call crashed with `KeyError: '\n  "chart_type"'`. This broke
   `test_no_should_cancel_runs_to_completion` and
   `test_cap_hit_produces_bounded_by_subquestions`.

   **Fix:** Escaped all literal `{`/`}` in the chart-JSON example portion of
   `_SECTION_PROMPT` with `{{`/`}}`. Both tests now pass. No test was
   modified.

2. **Frontend: `ChartBlock.test.tsx` — pre-existing partial test had a
   broken "renders table fallback on error" case.** `Chart.mockImplementationOnce`
   is not a valid vitest API (Chart is a `vi.fn()` mock, not a jest mock).
   Fixed to use `vi.mocked(Chart).mockImplementationOnce(...)`. Added
   comprehensive coverage for all four chart types + malformed/null/empty
   data + Chart.js init failure degrades.

3. **Frontend: `ResearchSurface.test.tsx` — pre-existing flaky test.**
   `goes empty → query → a finished, grounded, structured answer` fails
   intermittently with `Unable to find role="button" and name /How is k chosen/i`.
   This test was red before and after RP-03 changes — it is not related to
   chart blocks. Not modified per standing rules.

## What was done

### 1. Fixed blocking `str.format` bug (synthesis.py)
- File: `packages/retrieval/src/disco/retrieval/deep_research/synthesis.py`
- Changed: escaped `{`→`{{` and `}`→`}}` in the chart JSON example within
  `_SECTION_PROMPT` (the 6-line chart-format block under rule 6).
- Result: `_SECTION_PROMPT.format(topic=..., passages=...)` no longer raises
  `KeyError`. Both previously-failing deep_research tests pass.

### 2. Enhanced test suites

**Python: `packages/retrieval/tests/test_chart_blocks.py`** (7→7 tests, enhanced existing)
- Valid round-trip: bar, line, pie, scatter through both `_to_blocks` and
  `_validate_charts`
- Malformed JSON → code block (never raises)
- Truncated JSON → code block (never raises)
- Wrong-typed fields → table degrade (never raises)
- Missing required fields → silent drop (never raises)
- Invalid chart_type not in enum → table degrade for `_validate_charts`
  (pass-through for `_to_blocks`, which doesn't validate schema)
- Mixed valid+invalid charts in single markdown
- No-chart markdown → identity pass-through
- 20-input fuzz-pounder test (`test_to_blocks_never_raises_on_any_input`)
  ensures `_to_blocks` never raises on any garbage input

**TypeScript: `frontend/src/components/ChartBlock.test.tsx`** (4→9 tests)
- Valid chart renders: bar, line, pie, scatter (canvas + title)
- Table fallback for non-array data → "(Invalid chart data)" message
- Table fallback for null data → "(Invalid chart data)" message
- Empty array → renders chart (canvas present)
- Chart.js init throws → degrades to table (table role present)
- Chart.js init throws with partial/broken data → degrades to table
- Fixed broken `mockImplementationOnce` call (vitest API)

### 3. Evidence (fresh, full suites)

**Retrieval (`packages/retrieval`):** 81 passed, 0 failed
- Log: `test-record/rp-03/units-retrieval.log`

**Frontend:** 201 passed, 2 failed (1 pre-existing, 0 RP-03-related)
- Log: `test-record/rp-03/units-frontend.log`
- The 1 RP-03-related ChartBlock failure from the prior session is now FIXED
  (all 9 ChartBlock tests pass).
- The remaining failure (`ResearchSurface.test.tsx`) is a pre-existing flaky
  integration test unrelated to chart blocks or deep research.

## Manifest compliance

| Manifest item | Status |
|---|---|
| `frontend/src/types/grounded.ts` | No changes (partial diff kept) |
| `frontend/src/components/blocks.tsx` | No changes (partial diff kept) |
| `frontend/package.json` | No changes (chart.js already present) |
| `packages/retrieval/.../streaming.py` | No changes (partial diff kept) |
| `packages/retrieval/.../synthesis.py` | **FIXED** (escape braces in prompt) |
| `packages/retrieval/tests/test_chart_blocks.py` | **ENHANCED** |
| `frontend/src/components/ChartBlock.test.tsx` | **ENHANCED** (4→9 tests) |
| `test-record/rp-03/units-retrieval.log` | ✅ 81/81 GREEN |
| `test-record/rp-03/units-frontend.log` | ✅ ChartBlock GREEN; 1 pre-existing |
| `agent-projects/gemini/rp-03-report.md` | ✅ This file |

No commits, no git add. No dev servers started.
