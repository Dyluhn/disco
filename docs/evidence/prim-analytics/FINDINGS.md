# FINDINGS - analytics primitive lane

## Decisions

- Implemented only the `analytics` primitive; did not touch feature_flags/blog files.
- Added `AppSpec.analytics` as an unset-hidden `AnalyticsMeta`, matching the SEO additive pattern so existing specs/trees remain unchanged until the primitive is applied.
- Scoped `analytics` to lead_gen-shaped apps because this slice reuses the lead-gen D1 binding and `ADMIN_TOKEN` Bearer owner-auth convention.
- Lowered `_hits` with direct parameterized D1 `env.DB.prepare(...).bind(...)` calls so the existing lead Drizzle schema contract stays unchanged.
- Added a generated `/analytics` SPA page/section, public `/api/_hits` beacon route, owner-gated `/api/_hits/summary` aggregate route, and route-change beacon helper in `src/main.tsx`.
- Added a real WO-A3 verify hook: sqlite schema proof for `_hits`, Worker route/auth checks, dashboard component check, and SPA beacon helper check.

## Verification

- PASS: `python3 -m py_compile ...` for touched core modules.
- PASS: `ruff check` on new analytics files plus touched exports/spec/primitives.
- PASS: `pytest packages/core/tests/test_e61_analytics_primitive.py packages/tools/tests/test_e61_add_analytics_primitive.py -q`
  - Tail: `......... [100%]`
- PASS: adjacent primitive/add-primitive suites:
  - `test_f31_form_primitive.py`, `test_f52_collection_primitive.py`, `test_f53_seo_primitive.py`, `test_wo_a0_primitive_framework.py`, `test_wo_a3_primitive_verify.py`, and matching tool suites.
  - Tail: `......... [100%]`
- PASS: full AppKit-focused suite (`appkit`, `verify_appkit`, `f31/f52/f53/e61`, `wo_a*` tests).
  - Tail: `................ [100%]` with one expected `x`.
- PASS: `scripts/check_arch_budget.py`
  - Tail: `ARCH BUDGET OK - no class > 800 / function > 200 LOC outside the 5 capped-coordinator + 17 dispatcher allowances.`
- PASS: `basedpyright --venvpath /var/home/dylan/projects/disclaude`
  - Tail: `0 errors, 0 warnings, 0 notes`
- PASS: `git diff --check`
- NOTE: unqualified full monorepo `pytest` and the bounded non-integration variant both remained active/silent after initial passing dots in this sandbox and were interrupted cleanly with exit 130; no failure tail was produced.
