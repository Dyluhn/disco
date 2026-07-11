# FINDINGS: form edges

## What changed

- E1 generalized the `form` primitive to fold into `records` apps as well as
  `lead_gen`-shaped apps. Records-hosted forms use `POST /api/forms/<form_id>`
  while record CRUD continues to own `POST /api/<table>`, keeping each primitive's
  route contract independently verifiable.
- Directory apps remain refused because they are static: no D1 binding, no
  `schema.sql` data plane, and no Worker submission route. Shipping a partial fold
  would build a tree that cannot honestly verify or deploy. Hello remains refused
  because it is the mount-proof primitive.
- E2 wires form `success_message` through the folded `AppSpec`, generated
  `content.ts`, and `app_update_content`; regenerating after an edit updates the
  component output.
- E3 adds pure verify hooks for `form`, `seo`, and `collection`, registers them in
  their `PrimitiveDefinition`s, and exercises the form hook through the applied
  primitive dispatch path from `.disco/primitives/*.json`.
- E4 changes the deploy canonical-worker gate to reconstruct `worker/index.ts` by
  calling the registered primitive generator over the frozen staged `AppSpec`.
  The gate still uses byte equality after the existing normalization, still fails
  closed on missing/unloadable specs or generator failures, and does not consume
  staged design input.

## Code reality deviations

- The collection generator centralizes collection item payloads in
  `src/generated/content.ts`; the TSX components render through that generated
  content rather than embedding every literal directly in the component files.
  `collection_verify` therefore checks that collection sections render on their
  target pages and that the folded item literals are present in `content.ts`.

## Focused verification

- `packages/core/tests/test_f31_form_primitive.py`
- `packages/core/tests/test_f52_collection_primitive.py`
- `packages/core/tests/test_f53_seo_primitive.py`
- `packages/core/tests/test_wo_a3_primitive_verify.py`
- `packages/tools/tests/test_f31_add_form_primitive.py`
- `packages/tools/tests/test_app_kit_tools.py`
- `packages/tools/tests/test_wo_a3_verify_dispatch.py`
- `packages/agent-server/tests/test_appkit_cloudflare.py`

Tail:

```text
476 passed in 6.15s
```

## Directed-suite status

- Core full suite was attempted with the required worktree `PYTHONPATH`; it hung in
  `packages/core/tests/test_c18_plan_step_done_condition.py::test_c18_sandboxless_command_falls_back_to_subprocess`
  and also timed out when run as a single test with `timeout 60`.
- Tools full suite was attempted with the required worktree `PYTHONPATH`; the first
  failures were `packages/tools/tests/test_agent_tools.py::test_code_exec_python_state_persists_across_cells`
  and `test_code_exec_erroring_cell_keeps_prior_state`, both failing because the
  sandbox cannot create Jupyter sockets: `PermissionError: [Errno 1] Operation not
  permitted`.
- Agent-server full suite was attempted with the required worktree `PYTHONPATH`;
  it did not complete in this sandbox after several minutes. The deploy canonical
  gate tests touched by E4 are included in the focused passing set above.

## Static checks

```text
ruff check touched files: All checks passed
basedpyright: 0 errors, 0 warnings, 0 notes
scripts/check_arch_budget.py: ARCH BUDGET OK
git diff --check: clean
```
