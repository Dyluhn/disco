# SPEC: anchor-miss refusals carry the live region — one-turn recovery

## The live failure (flags attempt 5, iteration 2)

A `file_edit` whose `old` anchor no longer matched disk (the anchor contained
`═══` box-drawing comment dividers; the region had drifted) returned the agent
literally the string `old_text_not_found` — no path echo, no current content,
no cause, no recipe. With nothing to correct against, the model retried the
IDENTICAL edit until the repeated-action detector STUCKed the run.

The system already knows how to carry content in refusals: the read-before-edit
guards in `packages/tools/src/disco/tools/builtin/files.py` embed the current
file (whole when <= `_REFUSAL_READ_FULL_MAX_BYTES` = 64KB, else a targeted
window) so recovery is one turn. The anchor-miss path predates that pattern.

## Required behavior

Find every mutator that can fail with `old_text_not_found` (file_edit /
str-replace family — grep the literal). Replace the bare error with a
DIAGNOSIS + RECIPE refusal that includes:

1. WHAT: "file_edit refused: your `old` text was not found in {path} (the file
   has changed since you composed it, or the anchor differs in whitespace /
   unicode — e.g. decorative characters often drift)."
2. THE LIVE CONTENT to anchor on:
   - locate the BEST FUZZY MATCH region for the attempted `old` in the current
     file (line-window scoring is fine — e.g. difflib.SequenceMatcher over a
     sliding window, or match on the first/last non-blank line of `old`);
     show that region with line numbers, ±10 lines of context;
   - if no plausible match OR the file is <= the existing 64KB refusal cap,
     include the WHOLE current file (reuse the existing refusal-content
     machinery/caps — do NOT invent new limits).
3. RECIPE: "Anchor your next edit on the CURRENT text shown above (copy it
   exactly), or use file_replace_lines with the line numbers shown."
4. Keep the machine-readable error code (`old_text_not_found`) in
   ToolOutcome.error — tests/telemetry key off it; only the CONTENT gets rich.

Idempotent-target special case (worth a distinct message): if the file already
CONTAINS the `new` text (the edit evidently already applied — common on blind
retries), say exactly that: "the replacement text is already present at lines
N-M — the edit already applied; do not re-issue it."

## Tests (extend the existing file-tool test suites' idiom)

1. anchor miss on a small file → refusal contains the whole current file +
   the recipe; ToolOutcome.error == "old_text_not_found".
2. anchor miss on a >64KB file → refusal contains the best-match window with
   line numbers, not the whole file.
3. `new` already present → the already-applied message with its line range.
4. matching anchor → edit applies exactly as today (no behavior change).
5. unicode/whitespace drift case: `old` differing only by a decorative char
   from the live text → the fuzzy window FINDS the near-miss region and shows it.

## Verification
- PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests packages/core/tests -q

## Constraints
- Smallest coherent diff; reuse the existing refusal-content helpers/caps.
- Do NOT touch running dev servers or the main checkout.
- Commit on THIS branch (wt-editmiss) with --no-verify; FINDINGS-EDITMISS.md.
