# FINDINGS - feature_flags primitive

## Scope

- Implemented only the `feature_flags` primitive from `SPEC-primitives-batch.md`.
- Did not touch analytics/blog would-be files.
- Did not modify anything under `current/sec-work-remaining/`.

## Decisions

- Added `feature_flags` as a fillable add-on primitive, not a base scaffold.
- V1 folds only into lead_gen-shaped apps and refuses other hosts with guidance.
- Persisted flags in AppSpec through one stable `feature_flags` admin section marker so regeneration stays AppSpec-driven and `app_add_primitive` provenance still lands at `.disco/primitives/feature_flags.json`.
- Lowering appends to the lead-gen outputs only when flags are present:
  - `_flags` D1 table plus deterministic seed rows in `schema.sql`.
  - Drizzle `featureFlags` table in `src/db/schema.ts`.
  - Public `GET /api/_flags` returning only enabled flags.
  - Owner-gated `POST /api/_flags/toggle`, reusing the existing `ADMIN_TOKEN` / `isAuthorized` fail-closed convention.
  - `src/hooks/useFlag.ts` exporting `useFlag(key)` and `flags.isEnabled(key)`.
  - A small SPA admin section for toggling flags with an owner token.
- Added a real WO-A3-style verify hook that checks the seeded table by executing `schema.sql` in sqlite and structurally checks the public route, owner-gated toggle route, and admin section.

## Verification

All commands used the main repo venv with this worktree on `PYTHONPATH`.

```sh
PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/python3 -m pytest \
  current/packages/core/tests/test_appkit_generator.py \
  current/packages/core/tests/test_appkit_directory.py \
  current/packages/core/tests/test_f31_form_primitive.py \
  current/packages/core/tests/test_f52_collection_primitive.py \
  current/packages/core/tests/test_f53_seo_primitive.py \
  current/packages/core/tests/test_f62_feature_flags_primitive.py \
  current/packages/core/tests/test_wo_a0_primitive_framework.py \
  current/packages/core/tests/test_wo_a3_primitive_verify.py \
  current/packages/tools/tests/test_f31_add_form_primitive.py \
  current/packages/tools/tests/test_f52_add_collection_primitive.py \
  current/packages/tools/tests/test_f53_add_seo_primitive.py \
  current/packages/tools/tests/test_f62_add_feature_flags_primitive.py \
  current/packages/tools/tests/test_wo_a1_add_primitive.py \
  current/packages/tools/tests/test_wo_a3_verify_dispatch.py
```

Tail: `233 passed, 1 xfailed in 3.19s`.

```sh
ln -s /var/home/dylan/projects/disclaude/.venv .venv
PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/basedpyright
rm .venv
```

Tail: `0 errors, 0 warnings, 0 notes`.

```sh
PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/python3 development/scripts/check_arch_budget.py
```

Tail: `ARCH BUDGET OK - no class > 800 / function > 200 LOC outside the 5 capped-coordinator + 17 dispatcher allowances.`

```sh
PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/python3 -m ruff check \
  current/packages/core/src/disco/core/appkit/feature_flags_primitive.py \
  current/packages/core/tests/test_f62_feature_flags_primitive.py \
  current/packages/tools/tests/test_f62_add_feature_flags_primitive.py
```

Tail: `All checks passed!`

Notes:

- A first basedpyright attempt without `.venv` failed because `pyproject.toml` pins `venv = ".venv"`; the temporary symlink was removed after the passing run.
- A broad ruff run over the already-touched `generator.py` still reports pre-existing long generated-string lines unrelated to this lane. The new feature flag module/tests pass ruff.
