# Migration: a PAUSED / non-finished build is NOT a PASS

**Date:** 2026-06-24
**Tracked files changed (§19):** `development/harness/build_soak/oracles/output_truth.py`,
`development/harness/build_soak/failure_codes.py`, `development/harness/build_soak/tests/test_output_truth_oracle.py`

## What assertion changed
`OutputTruthOracle` previously SKIPPED (→ contributed PASS) when a run had NOT reached a
finished terminal, on the reasoning "an unfinished run can't be a false finish." Now, when the
scenario REQUIRES output (`assertions.workspace.files` / `assertions.preview.required`) and/or
declares `assertions.terminal_status_in`, a run that did NOT reach a finished/required terminal
FAILS with the new code **`BUILD_DID_NOT_FINISH`** (P1).

## Why the previous assertion was wrong
It was a fail-closed hole: a bare-Build run that PAUSED (the no-progress / actionless valve), timed
out, or ended at an unhandled gate scored **PASS**, because the approval chain is judged only for
FINISHED/STUCK and output-truth was skipped for any non-finished terminal — so the required
workspace/preview deliverable was never verified. Live-surfaced (surfaced-bugs **Bug 8**):
`must_plan_before_tool` over `conv_c0ff8684...` actually PAUSED "actionless" yet classified PASS.
A build that does not complete is not a pass even if some files happen to exist on disk.

## Which product behavior replaces it
None — this is a HARNESS adjudicator (fail-closed) fix, not a product change. A run that the
scenario expects to FINISH-and-deliver but that only ever PAUSES/TIMES-OUT now classifies
`BUILD_DID_NOT_FINISH` (P1), never PASS. A scenario that asserts no output (`wants_output` False)
still SKIPs (unchanged) — the gate only fires where a finish was actually required.

## Which tests prove the new behavior
- `development/tests/test_output_truth_oracle.py::test_not_finished_with_required_output_fails_closed`
  (was `test_not_finished_skips_output_truth`) — a required-output run with no FINISHED status →
  `BUILD_DID_NOT_FINISH`.
- `development/tests/test_classifier.py::test_paused_incomplete_required_output_is_build_did_not_finish` —
  end-to-end classify of a PAUSED-incomplete plan-gated run → FAIL/`BUILD_DID_NOT_FINISH`/P1.
- `development/tests/test_api_runner.py::test_paused_then_finished_resumes_to_terminal` /
  `test_paused_forever_is_bounded_then_build_did_not_finish` — the runner's bounded PAUSED-resume.
