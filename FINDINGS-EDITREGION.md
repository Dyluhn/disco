# FINDINGS-EDITREGION

## Reproduction

- Baseline command passed before implementation:
  `PYTHONPATH=/home/dylan/projects/disclaude-wt-editregion/packages/agent-server/src:/home/dylan/projects/disclaude-wt-editregion/packages/app-server/src:/home/dylan/projects/disclaude-wt-editregion/packages/core/src:/home/dylan/projects/disclaude-wt-editregion/packages/retrieval/src:/home/dylan/projects/disclaude-wt-editregion/packages/tools/src /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests packages/core/tests -q`
- Final verification with the same command passed after implementation. An earlier full-suite run had three transient integration/environment failures; rerunning those failures passed, and the subsequent full-suite rerun passed.

## Deviations / Code Reality

- No required behavior was intentionally skipped.
- Success grounding is limited to the numbered content actually returned to the model. If the 64KB cap truncates an UPDATED-REGION view, only the line numbers that survive the cap are recorded as sha-grounded. This keeps the fresh-edit coverage guard honest.
- Existing refusal-delivered reads keep their prior behavior: they can ground corrected targeted edits but still do not set `read_since_write` for blind `file_write`. The spec covered successful mutators, so refusal behavior was left intact.
- `safe_write_file` was not changed because the spec names `file_write`, `file_append`, `file_edit`, `file_str_replace`, `exact_replace`, `file_replace_lines`, and `file_insert_lines` only.
