# Codex CODE Review — PR CXT-5 (recoverable compression) — IMPLEMENTED

Review the ACTUAL CODE vs plan + your round-1 required revisions. Inspect:
NEW:
- packages/core/src/disco/core/observations.py (recoverable_excerpt, DESTRUCTIVE_ELISION_MARKERS,
  RECOVER_CUES, scan_for_destructive_elision)
- packages/core/tests/test_recoverable_excerpts.py, packages/tools/tests/test_browser_spill.py
CHANGED:
- packages/tools/src/disco/tools/builtin/browser.py (run(): spill full console+network to
  .disco-spill-browser-*.json + prose pointer + structured diagnostics_spill_path when over caps)
- packages/core/src/disco/core/loop/bootstrap.py (truncation marker now names file_read + manifest)

Your round-1 required revisions — verify each in CODE:
1. bootstrap.py truncation made recoverable (names file_read + manifest). DONE.
2. scan semantics: DESTRUCTIVE_ELISION_MARKERS exact set + RECOVER_CUES exception (a marker is flagged
   only if no cue on the same line); bare ellipsis NOT in the set. Verify it does NOT false-positive on
   in-tree recoverable markers (test_scan_does_not_flag_recoverable_markers covers snip/shell/file_read/
   bootstrap/browser).
3. browser fix exposes a stable recover ref like shell-spill: structured["diagnostics_spill_path"] + prose
   pointer + full diagnostics persisted (not marker text alone). DONE.
4. pos/neg scan unit-test pair present.
5. P1 hook named in observations.py docstring (OutputTruthOracle imports the markers + scan, rule set v1).

Test status: 16 passed (incl browser regression); basedpyright strict 0 errors.

Judge correctness, no destructive-elision left in browser path, no false-positives in the scan, no behavior
regression. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
