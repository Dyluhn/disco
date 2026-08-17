# RP-03 — Chart blocks

Parent plan: docs/next-fix-set-plan.md § RP-03 (locked).
Implementation of a new `chart` answer block type for both standard and deep research.

## Work items

1. **Schema: `frontend/src/types/grounded.ts` (~line 48)**
   - Add `chart` member to the `AnswerBlock` union.
   - Define `ChartBlock` with narrow per-type JSON schemas (bar, line, pie, scatter).
   - Use the GPT-Vis/DeerFlow pattern for the schema.

2. **Frontend: `frontend/src/components/blocks.tsx` (~line 171)**
   - Implement `ChartBlock` component using **Chart.js 4**.
   - Add `chart.js` to `frontend/package.json`.
   - Handle the narrow per-type JSON schema; unknown/invalid chart payloads render the table fallback, never a crash.
   - Include a degrade-to-table path if rendering fails.

3. **Backend: `packages/retrieval/src/disco/retrieval/streaming.py` (~line 109)**
   - Update `_to_blocks()` to detect markdown-encoded data suitable for charts.
   - Emit `chart` blocks when appropriate.

4. **Deep Research: `packages/retrieval/src/disco/retrieval/deep_research/synthesis.py` (~line 95)**
   - Update the synthesis prompt to encourage the model to propose charts as typed blocks.
   - Implement backend validation of proposed chart JSON using `jsonschema`.
   - Implement **one retry-with-error-trace** on validation failure.
   - Degrade to `table`block if validation fails after retry.

5. **Schema-fuzz suite (plan-required gate)**
   - `packages/retrieval/tests/test_chart_blocks.py` (new): malformed/truncated/wrong-typed chart JSON -> degrades to table block, never raises; valid bar/line/pie/scatter round-trip.
   - `frontend/src/components/ChartBlock.test.tsx` (new): renders a valid chart; invalid payload renders table fallback.

## Standing rules (Wave-2)

- Never modify an existing test to make new code pass; if a test contradicts your change, STOP and flag.
- DC-05 meta-tool withholding and valve semantics are ratified design — changes to `_tools_for_step`/valve behavior are out of scope for all four.
- Run the FULL suite for evidence; a green run of only your own test files hid 17 broken tests in wave 1.
- Flag every manifest deviation at the top of your report with justification.
- Do NOT commit, Do NOT git add, Do NOT start dev servers on :5173/:5174.

## Manifest

- frontend/src/types/grounded.ts
- frontend/src/components/blocks.tsx
- frontend/package.json
- packages/retrieval/src/disco/retrieval/streaming.py
- packages/retrieval/src/disco/retrieval/deep_research/synthesis.py
- packages/retrieval/tests/test_chart_blocks.py
- frontend/src/components/ChartBlock.test.tsx
- test-record/rp-03/units-retrieval.log
- test-record/rp-03/units-frontend.log
- agent-projects/gemini/rp-03-report.md

## Anti-scope

- No matplotlib or any image-based charts in the sandbox.
- No raw Vega-Lite grammar.
- No changes to `engine.py` (RP-04 owns it).

## Evidence

- `cd frontend && timeout 300 npx vitest run` > test-record/rp-03/units-frontend.log
- `timeout 300 uv run pytest packages/retrieval -v` > test-record/rp-03/units-retrieval.log
- Behavioral run + screenshot of a real chart in the UI: REVIEWER-run rung, not yours.
- Report: `agent-projects/gemini/rp-03-report.md`
