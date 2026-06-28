# Codex PLAN Review — PR CXT-5 (recoverable compression)

DESIGN/PLAN review (code not written yet). CXT-1..4 merged. Inspect packages/tools/src/disco/tools/builtin/
browser.py (console/network/stack truncation ~lines 351-412), system.py (shell HS-01 spill ~109-136),
events.py (snip_content ~268-298, _snip_args ~318-401), files.py (FileReadTool paging). Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE.

The central scoping claim to judge: a scout found that ALMOST ALL truncation in disco is ALREADY
recoverable (snip_content, snapshot head/tail, file_read paging, shell spill, F8/F9/W2 — each marker names
a recover path; _snip_args is destructive-looking but K1-guarded + recoverable from the event log). The
ONLY truly destructive site (no recover path) is browser.py console/network/stack truncation.

So CXT-5 proposes: (1) add observations.py with a structured recoverable_excerpt() + a
scan_for_destructive_elision() engine; (2) FIX only browser.py to spill full diagnostics to a file + name
it (mirror shell HS-01); (3) ship + unit-test the scan engine, defer the final-deliverable build scan to
P1 product-harness; (4) explicitly DO NOT rewrite the already-recoverable sites (no churn, preserve tested
behavior).

Judge: (a) Is "don't churn the already-recoverable sites, fix only the destructive browser site" correct,
or does the campaign's "remove destructive elision from source-like observations" REQUIRE retrofitting
files/shell/snip_content to the structured format too? (b) Is the browser spill-to-file fix the right
recoverable pattern? (c) Is deferring the final-file build scan to P1 acceptable while shipping the scan
engine now? (d) Any destructive site the scout MISSED that you can see? (e) test sufficiency.

## PR Plan
## PR CXT-5 — Recoverable compression (no destructive elision)

### Status
PLANNING → CODEX_REVIEW

### Dependencies
- CXT-2 (store/SUMMARY artifact patterns), CXT-3 (recover patterns). Independent of CXT-4 wiring.

### ELISION SITE MAP (scout — durable; the basis for scope)
ALREADY RECOVERABLE (marker names a recover path — DO NOT churn these, they're tested):
- events.py snip_content (obs truncation): "… [snipped N chars — re-run the tool or use file_read …] …"
- view_render.py snapshot file head/tail: "… [N more chars — file_read(path, offset, limit) …] …"
- files.py FileReadTool paging: header "[lines X-Y of N; read more with offset=Z]"
- system.py shell HS-01 spill: full stdout → .disco-spill-*.log + "full output at <path> — file_read/grep"
- dedup.py F8/F9/W2: all name a recover path / earlier result.
- events.py _snip_args ARG marker: destructive-looking BUT defended by K1 guard + K1 recovery from the
  event log (history placeholder, not final-file content). Leave as-is (already mitigated).

TRULY DESTRUCTIVE (the CXT-5 target — NO recover path):
- browser.py console/network/stack truncation: "... (console truncated to N lines)",
  "... (stack truncated to 6 lines)", "... (N more network failures)" — content lost; re-run is expensive.

### Plan (narrow + high-value; no broad rewrite of working recoverable sites)
1. NEW `packages/core/src/disco/core/observations.py` (campaign names it): a small pure
   `recoverable_excerpt(content, *, path, shown_start, shown_end, recover_tool, recover_args) -> dict`
   producing the campaign's structured form {kind:"file_excerpt", path, complete:bool, shown_ranges,
   omitted_ranges, sha256, recover:{tool,args}} + `DESTRUCTIVE_ELISION_MARKERS` (the forbidden strings)
   + `scan_for_destructive_elision(text) -> list[str]` (finds forbidden markers NOT accompanied by a
   recover hint). Pure, fully unit-tested. This is the canonical pattern + the scan engine.
2. FIX browser.py destructive truncation → recoverable: when console/network/stack exceed caps, write the
   FULL rendered diagnostics to a spill file (mirror shell HS-01: `.disco-spill-browser-*.log` via
   ctx.sandbox.write_file in run()) and change the markers to name it
   ("… N more — full diagnostics at <path>; file_read it"). The truncated head stays for at-a-glance;
   nothing is lost. structured payload carries the spill path.
3. HARNESS SCAN: a test (packages/core/tests/test_recoverable_excerpts.py) that
   scan_for_destructive_elision flags the forbidden strings, PASSES the already-recoverable markers
   (they name a path / "file_read" / "re-run"), and that the browser markers post-fix are recoverable.
   (Final-file scan over built deliverables is a product-harness concern → P1; here we ship + unit-test
   the scan engine and document the P1 hook.)

### Scope boundaries (for the gate)
- Do NOT rewrite the already-recoverable sites (snip_content/snapshot/file_read/shell-spill) — they
  already satisfy "recoverable" and are tested; churning them is risk without benefit. Flag this.
- The structured recoverable_excerpt format is introduced + available; retrofitting every tool to emit it
  is deferred (the existing prose markers already carry recovery). Browser is fixed because it's the only
  DESTRUCTIVE site.
- Final-deliverable elision scan (fail builds containing forbidden strings) = P1 product-harness; CXT-5
  ships the scan engine + unit tests + documents the hook.

### Tests (packages/core/tests/test_recoverable_excerpts.py + packages/tools/tests/test_browser*.py)
- recoverable_excerpt: roundtrips, sha256 stable, complete=False when omitted, recover ref present;
  shown/omitted ranges correct.
- scan_for_destructive_elision: flags "...(elided)...", "[trimmed]", "content omitted",
  "truncated for brevity"; does NOT flag a snip marker that names file_read/re-run/a path.
- browser fix: when diagnostics exceed caps, a spill file is written, markers name it, head retained,
  nothing lost; assert no destructive (no-recover) marker remains.

### Codex review
- verdict: (pending)


## Required response format
VERDICT: APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE
REASONS:
- ...
REQUIRED_REVISIONS:
- ...
