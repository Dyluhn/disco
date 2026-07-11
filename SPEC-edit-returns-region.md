# SPEC F1: mutating file tools return the updated region and grant grounding

## Why (measured)

Across 5 passing build runs: 46% of edits (77 of 167) were immediately followed
by a re-read of the SAME file — a full LLM round-trip each (~13 min of ceremony
per 5 runs) — plus 4 FRESH_READ_REQUIRED and 2 STALE_FILE_CONTEXT refusals.
The agent re-reads because (a) our own descriptions tell it to and (b) the
freshness gates refuse un-grounded edits. The edit's observation is the
freshest possible truth about the file — return it and count it as grounding.

## Required behavior

For file_edit, file_str_replace, exact_replace, file_replace_lines,
file_insert_lines, file_append, and file_write (on success):

1. The success ToolOutcome content includes an UPDATED-REGION view:
   the changed lines ±10 lines of context, with 1-based line numbers as
   file_read renders them, plus a one-line header
   "applied — lines N-M now read:". For file_write (whole file) show the
   first 40 lines + total count instead. Cap the embedded view with the
   existing refusal-content cap machinery (64KB) — never a monster payload.
2. GROUNDING: a successful mutation updates the same freshness state a
   file_read would set for that path (the read-since-write bit AND the
   fresh-edit sha grounding — find them in files.py: the F1/CD-TOOLS-1
   machinery). The next targeted edit to that path must NOT be refused for
   staleness when it anchors on the returned view. The gates stay intact for
   paths the model has neither read nor just mutated.
3. DESCRIPTION updates in lockstep:
   - file_replace_lines: replace "re-read the file IMMEDIATELY before each
     call" with "your previous edit's observation shows the CURRENT numbering
     for that region — use it; re-read only if you edited elsewhere since".
   - file_edit large-file hint: same adjustment.

## Tests (extend the existing edit-guard suites' idiom)
1. edit → observation contains the ±10-line numbered region.
2. edit → immediately edit the SAME file again anchored on the returned view
   → NOT refused for freshness (no intervening file_read needed).
3. edit file A → edit file B (never read) → B still refused (gates intact).
4. file_write success → first-40-lines view + grounding for that path.
5. The 64KB cap holds for a large-file region view.

## Verification
- PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests packages/core/tests -q

## Constraints
- Smallest coherent diff; reuse existing content-view helpers.
- Do NOT touch running servers or the main checkout.
- Commit on wt-editregion with --no-verify; FINDINGS-EDITREGION.md.
