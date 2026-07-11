# Edit Surface 2B Findings

Implemented SPEC-edit-surface-2b:

- Removed `file_str_replace`, `find_and_edit`, `safe_write_file`, and `plan_step` from `AGENT_TOOLS`.
- Kept legacy tool classes registered for non-agent scopes, direct tests, workflows, and replay/back-compat paths.
- Kept `exact_replace` in agent scope and gated its advertisement through `ModelExecutionPolicy.anchored_edit`.
- Folded the safe-write guards into `file_write`: `.disco/` governed guard, elision rejection, >50% shrink refusal with `allow_shrink=true`, and atomic commit through the existing sandbox atomic-write helper.
- Removed prompt affordances for retired tools and updated prompt text to describe guarded `file_write`.

Code-reality deviations:

- `safe_write_file` was also removed from `ARTIFACT_TOOLS` because that scope is explicitly tested and documented as a strict subset of `AGENT_TOOLS`; `file_write` now carries the relevant guards there.
- `ModelExecutionPolicy.withheld_tools` no longer lists tools that are absent from `AGENT_TOOLS`; it only withholds currently scoped tools (`exact_replace`, `update_plan_progress`).
- Existing `file_write` syntax-gate behavior is preserved: syntax-introducing writes are still rejected through the existing gate, with atomic writes/reverts where the backend supports them.

Verification:

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/tools/tests packages/core/tests packages/agent-server/tests -q` passed.
