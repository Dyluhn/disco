# Read-Churn Valve Findings

## What Changed

- Added an execution-only read-churn signal derived from the event log.
- Same-path `file_read` calls with explicit `limit <= 25` now form a small-read streak.
- Warnings are emitted once per streak at counts 5, 10, and 15 as environment `MessageEvent`s with `meta.diagnostic == "read_churn_nudge"` and `count`/`streak` metadata.
- From streak count 20 onward, the current event-derived streak contributes to `_invisible_steps`, so the existing actionless ladder pauses the run through its normal machinery.
- Reset coverage includes same-path whole-file reads, reads that reach the file remainder per the tool observation header, different-path reads, file edits/writes/line edits/appends, shell/run/browser/preview/design-lint/update-plan/submit/finish actions, user/agent messages, and plan boundaries.
- Updated the `file_read` prompt text to say to re-read the range being edited, not to broadly re-read before every line edit.

## Reproduction

- Added the failing scripted-provider loop test first:
  `test_same_path_small_file_reads_warn_then_feed_actionless_ladder`.
- Before the implementation it failed with no `read_churn_nudge` diagnostics after repeated small reads of `index.html`.

## Verification

- Focused read-churn tests:
  `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests/test_loop_integration.py -q -k 'read_churn or same_path_small_file_reads'`
  passed.
- Full requested command:
  `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q`
  passed with exit code 0. Pytest reported dependency/runtime warnings only.

## Deviations / Code Reality

- The >=20 ladder contribution is computed from the event-derived streak as `count - 19` and assigned into `_invisible_steps` with `max(...)`. This is behaviorally equivalent to incrementing on each post-20 read and survives loop recreation better than relying on in-memory accumulation.
- The warning reminder is persisted as the diagnostic `MessageEvent`; because read handling returns to the main loop before the next driver step, that persisted message is the next-step reminder. No extra same-step transient retry message is needed unlike the prose no-op path.
- The full-file-read negative test uses three repeated whole-file reads to stay below the pre-existing repeated-action stuck detector. The valve still treats no-limit reads as non-small and emits no read-churn warning.
