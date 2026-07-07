# RP-04 Wide Research Pipeline Report

## Status
- **E3 Root-cause Fix**: Completed. `openai_provider.py` now strips XML-like tags (e.g., `</parameter>`) in `_repair_json`.
- **Pipeline Restructuring**: Completed. `DeepResearchRun.run` now uses an `asyncio.create_task` producer/consumer model.
- **Race Fixes**: Completed. 
  - Budget is partitioned N-ways upfront.
  - `section_id` and `vector_store` namespaces are derived from sub-question title hashes for stability and isolation.
- **Verification**:
  - `packages/core/tests/test_toolcall_defense.py`: New regression test `test_tool_call_xml_leak_defense` passed.
  - `packages/retrieval/tests/test_pipeline_invariants.py`: New tests for concurrency, serial synthesis, budget partitioning, and resume semantics passed.
  - Full suite run: `packages/retrieval` (74/74 passed), `packages/core` (445/448 passed).

## Manifest Deviations
- None. All requested files were modified or created as specified.

## Pre-existing Test Failures in `packages/core`
The following 3 tests failed in `packages/core`, but are unrelated to the RP-04 changes:
1. `test_auto_approved_not_stamped_when_base_would_not_gate` (No ActionEvent found)
2. `test_provider_error_reaches_the_user_with_real_content` (Call count mismatch: expected 1, got 3)
3. `test_small_old_observation_is_full` (String format mismatch: 'Output: small' vs 'small')

## Evidence
- Logs captured in `test-record/rp-04/units-core.log` and `test-record/rp-04/units-retrieval.log`.
- Pipeline concurrency verified via `test_pipeline_invariants.py` using `time.monotonic()` overlap checks.

## Implementation Details
The `pending` sub-questions are now processed as follows:
1. **Producer Phase**: All `gather_for_subquestion` calls are wrapped in `asyncio.create_task` and started concurrently.
2. **Consumer Phase**: The results are awaited in the original order. As each `gather` task completes, `synthesize_section` is called. This ensures that while synthesis for section *k* is happening, retrieval for section *k+1* and beyond is already making progress in the background.
3. **Budgeting**: The `max_sources` budget is divided by the number of pending questions at the start. Leftovers are distributed to the first few questions.
4. **IDs**: `section_id` is now `f"s{hash[:8]}"` instead of `f"s{index}"`. This ensures that if a run is stopped and resumed, the IDs remain stable even if the plan order changes or some sections are already done.
5. **Namespacing**: Each sub-question gets its own vector store namespace: `{base_namespace}/{hash[:8]}`.
