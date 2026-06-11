# RP-04 — Wide research pipeline, phase 1

Parent plan: docs/next-fix-set-plan.md §2 RP-04 (locked).
Restructure the deep-research engine into a producer/consumer pipeline to allow concurrent retrieval.

## Work items

0. **E3 Root-cause: `packages/core/src/perpleximanus/core/llm/openai_provider.py`**
   - Investigate and fix the stray `</parameter>` malformed tool-call root cause.
   - The mitigation in `_tool_calls` (`{"_raw":...}` fallback, ~line 335) confirms the problem exists; fix it in the stream parsing/extraction machinery if appropriate.

1. **Pipeline: `packages/retrieval/src/perpleximanus/retrieval/deep_research/engine.py` (~line 174)**
   - Restructure the `for subq in pending:` loop into a producer/consumer pipeline.
   - ALL retrieval work (search, fetch, extract, embed, rerank) for all N sub-questions should run concurrently.
   - LLM work (QUERY_REWRITER/RAG_ANSWERER synthesis) MUST stay a single-depth queue (one at a time).
   - Ideally: sub-question k+1's retrieval prefetches while k synthesizes.

2. **Race Fixes* in `engine.py`**
   - **`remaining` budget TOCTOU** (~line 173, 196): Partition the budget N-ways upfront instead of sharing a mutable counter concurrently.
   - **`section_id` collision** (~line 214): Derive id from sub-question hash, not list length, to ensure stability under concurrency.
   - **vector_store namespace** (~line 190): Ensure per-sub-question namespaces to avoid interleaving corruption.

3. **Resume Semantics** (~line 153-158)
   - Ensure resume still works correctly with the pipeline (checkpoint per completed section).

## Standing rules (Wave-2)

- Never modify an existing test to make new code pass; if a test contradicts your change, STOP and flag.
- DC-05 meta-tool withholding and valve semantics are ratified design — changes to `_tools_for_step`/valve behavior are out of scope for all four.
- Run the FULL suite for evidence; a green run of only your own test files hid 17 broken tests in wave 1.
- Flag every manifest deviation at the top of your report with justification.
- Do NOT commit, do NOT git add, do NOT start dev servers on :5173/:5174.

## Manifest

- packages/core/src/perpleximanus/core/llm/openai_provider.py
- packages/retrieval/src/perpleximanus/retrieval/deep_research/engine.py
- packages/retrieval/tests/test_pipeline_invariants.py
- packages/core/tests/test_toolcall_defense.py
- test-record/rp-04/units-core.log
- test-record/rp-04/units-retrieval.log
- agent-projects/gemini/rp-04-report.md

## Anti-scope

- No parallel LLM decode (llama.cpp bug); synthesis MUST be serial.
- No RP-04b profiling (SKIPPED).

## Evidence

- `timeout 600 uv run pytest packages/core -v` > test-record/rp-04/units-core.log
- `timeout 600 uv run pytest packages/retrieval -v` > test-record/rp-04/units-retrieval.log
- E3 regression test: a stream yielding a stray `</parameter>` tail parses to a clean tool call (add to test_toolcall_defense.py).
- Instrumented LLM-busy vs retrieval-wait ratio: REVIEWER-run rung.
- A/B wall-clock + killed-and-resumed mid-pipeline run: REVIEWER-run rungs.
- Event-order invariants (section_done per section, no interleaving corruption) asserted in test_pipeline_invariants.py (yours).
- Report: `agent-projects/gemini/rp-04-report.md`
