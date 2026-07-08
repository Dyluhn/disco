# FINDINGS-EDITMISS

## Reproduction

- Baseline command before edits: `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests packages/core/tests -q`
- Baseline result: passed.

## Implementation

- Routed all anchored old-text miss producers through the shared recovery refusal helper.
- `file_edit`, `file_str_replace`, and `exact_replace` miss refusals now include a diagnosis, live numbered content, and the retry recipe.
- Small files carry the full current file.
- Large files carry a best fuzzy-match window with line numbers and 10 lines of context when a plausible match exists.
- If the replacement text is already present, the refusal reports the existing line range and tells the caller not to re-issue the edit.

## Deviations

- The spec names `old_text_not_found` as the machine-readable error code. Code reality already has distinct public error codes for sibling tools: `old_str_not_found` for `file_str_replace` and `EXACT_REPLACE_NO_MATCH` for `exact_replace`. Those are preserved for compatibility; the rich refusal content is shared.
- The spec says to include the whole current file when no plausible match exists. For files over the existing 64KB refusal cap, this implementation preserves the cap and reports that no plausible fuzzy match was found instead of embedding the whole file.

## Verification

- Targeted: `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests/test_edit_guards.py packages/tools/tests/test_w3_w4_edit_safety.py packages/tools/tests/test_exact_replace.py packages/tools/tests/test_inclusive_editing.py packages/tools/tests/test_rel_rc_d_reground.py packages/tools/tests/test_builtin_extra.py -q`
- Targeted result: passed.
- Full: `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests packages/core/tests -q`
- Full result: passed.
