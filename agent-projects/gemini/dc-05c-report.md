# DC-05c Report: Resume-time Condensation of Degenerate Trailing Segments

**Date:** 2026-06-10
**Status:** Completed
**Scope:** `packages/agent-server` (with a surgical fix in `packages/core/view.py`)

## Summary of Changes

Implemented a deterministic (no-model) trailing degeneracy detector in `ConversationRuntime` that triggers during `resume_conversation`. This repair ensures that if a conversation was paused in a degenerate state (repetition, knowledge spam, looping without work), the resumed session starts with a "clean" context, preventing the model from self-conditioning on its own prior failures.

### 1. `ConversationRuntime._condense_trailing_degeneracy`
- **Detection:** Scans the event log backwards from the tail, stopping at the first "real" `ActionEvent` (non-bookkeeping) or a human `MessageEvent` (`source="user"`).
- **Thresholds:** Detects degeneracy if the segment contains ≥6 events, zero real actions, and either ≥3 assistant prose messages or any duplicated knowledge fact repeated ≥3 times.
- **Pinning:** Respects pinning by ensuring the FIRST instance of any duplicated knowledge fact stays outside the condensation span. Interleaved repeats are handled by `View.of` pinning (see below).
- **Tombstone:** Emits a `CondensationEvent` with a factual summary counting the dropped turns, duplicate knowledge entries, and plan revisions.

### 2. `resume_conversation` Integration
- Wired the detector to run immediately after loading events.
- If degeneracy is detected, the tombstone is appended to the store *before* reconstruction and the `RUNNING` status flip, ensuring the reconstructed View reflects the condensation.

### 3. `View.of` Pinning Restoration (`packages/core/view.py`)
- **Discovery:** Found that `_pinned_seqs` in `view.py` was pinning ALL `KnowledgeEvent`s regardless of duplication, contradicting the "Existing machinery" description in the workorder ("KnowledgeEvent spam is exempted from pinning protection ONLY when the events are exact duplicates").
- **Fix:** Surgically updated `_pinned_seqs` to only pin the FIRST instance of a (scope, snippet) pair. This was necessary to fulfill the "Acceptance ladder" requirement that the DEFECT-4 replay drop >100 messages (pinned events are never dropped by `View.of`).

## Validation Results

- **Unit Tests:** `packages/agent-server/tests/test_resume_condensation.py` covers:
    - Healthy tails (no condensation).
    - Breaker-paused tails below threshold (no condensation).
    - Pure prose degeneracy (condensation triggered).
    - Interleaved duplicate knowledge (condensation triggered, first instances preserved).
    - **DEFECT-4 Replay:** Verified against the marathon fixture. Condensation triggered, dropping 142 redundant messages from the View (well above the >100 requirement).
- **Integration:** Verified that `resume_conversation` correctly appends the tombstone and reconstruction events in the proper order.
- **Regression:** Ran `test_resume_reconstruction.py`, `test_resume.py`, and `test_lifecycle.py`. All 49 tests passed.

**Log file:** `test-record/dc-05/units-c.log`

## Manifest

- `packages/agent-server/src/disco/agent_server/runtime.py`
- `packages/core/src/disco/core/view.py`
- `packages/agent-server/tests/test_resume_condensation.py`
- `test-record/dc-05/units-c.log`
- `agent-projects/gemini/dc-05c-report.md`

## Reviewer addendum (Claude, 2026-06-10)

- **Defect fixed in review:** the detector's bookkeeping boundary used a fabricated
  tool name (`update_topic` — exists nowhere in the repo). Replaced with the
  engine's canonical `_BOOKKEEPING_TOOLS` ({submit_plan, propose_plan_update,
  plan_step, finish}) imported from `core.loop.engine` — single source of truth.
- **Lint fixed in review:** 6 unused-import/sort findings + 3 E501s in the new
  test file and runtime.py (worker's BaseEvent import).
- **view.py deviation ACCEPTED:** the brief's "Existing machinery" premise
  (dup-knowledge pinning exemption) did not exist in code; the worker's
  `_pinned_seqs` first-instance dedup implements exactly what the brief asserted,
  and the DEFECT-4 gate (>100 dropped) is unachievable without it.
- **Known limitation (follow-up, not blocking):** a degenerate tail containing
  duplicate-`remember` ActionEvents (the phase-b-rerun2 shape) terminates the
  backward scan at the remember — `remember` is a real tool, not bookkeeping —
  so that specific tail shape is not condensed. Per the locked brief boundary.
- **Reviewer ladder:** 49 agent-server resume tests + 426 core tests (1 skip)
  pass; ruff clean on changed files except the pre-existing runtime.py:1475
  baseline E501.
