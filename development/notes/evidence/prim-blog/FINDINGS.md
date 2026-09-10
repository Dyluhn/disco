# FINDINGS — blog primitive

## Decisions

- Implemented only the `blog` primitive. No analytics or feature-flag files were added.
- Fold stores posts on `AppSpec.blog` so regenerated trees remain driven by `.disco/appspec.json`.
- Blog is addable on shared React AppKit hosts (`lead_gen`, `directory`, `records`) and refuses stubs such as `hello`.
- Markdown lowers to structured React data rendered as React nodes; no `dangerouslySetInnerHTML` or raw HTML injection.
- Blog emits SPA routes (`/blog`, `/blog/<slug>`), generated blog components/data, and `public/rss.xml`. RSS uses `app.seo.base_url` when present, otherwise relative links.
- SEO sitemap integration is via the existing `_seo_files` helper reading blog routes when `app.blog` exists.
- Registered a WO-A3 verify hook checking AppSpec blog presence, index/data coverage, post routes/components, and parseable RSS with every post.

## Verification

- Focused blog/SEO/A3:
  `PYTHONPATH=current/packages/core/src:current/packages/tools/src:current/packages/tools/tests /var/home/dylan/projects/disco/.venv/bin/pytest current/packages/core/tests/test_f53_blog_primitive.py current/packages/tools/tests/test_f53_add_blog_primitive.py current/packages/core/tests/test_wo_a3_primitive_verify.py current/packages/core/tests/test_f53_seo_primitive.py current/packages/tools/tests/test_f53_add_seo_primitive.py`
  Tail: `75 passed in 1.32s`
- Adjacent AppKit primitives, non-integration:
  `PYTHONPATH=current/packages/core/src:current/packages/tools/src:current/packages/tools/tests /var/home/dylan/projects/disco/.venv/bin/pytest -m 'not integration' current/packages/core/tests/test_appkit_generator.py current/packages/core/tests/test_appkit_directory.py current/packages/core/tests/test_records_primitive.py current/packages/core/tests/test_records_auth.py current/packages/core/tests/test_f31_form_primitive.py current/packages/core/tests/test_f52_collection_primitive.py current/packages/tools/tests/test_f31_add_form_primitive.py current/packages/tools/tests/test_f52_add_collection_primitive.py current/packages/tools/tests/test_wo_a1_add_primitive.py current/packages/tools/tests/test_wo_a3_verify_dispatch.py`
  Tail: `154 passed, 2 deselected, 1 xfailed in 3.04s`
- AppKit tool/verifier/spec groups:
  `PYTHONPATH=current/packages/core/src:current/packages/tools/src:current/packages/tools/tests /var/home/dylan/projects/disco/.venv/bin/pytest -m 'not integration and not sandbox_integration' current/packages/tools/tests/test_app_kit_tools.py current/packages/tools/tests/test_appkit_directory_tools.py current/packages/tools/tests/test_appkit_scope_enforcement.py current/packages/tools/tests/test_verify_appkit_app.py current/packages/core/tests/test_verify_appkit_gate.py current/packages/core/tests/test_appkit_spec.py current/packages/core/tests/test_appkit_snapshot.py current/packages/core/tests/test_appkit_semantic_metadata.py`
  Tail: `245 passed in 6.33s`
- Static checks:
  `basedpyright --project pyproject.toml --venvpath /var/home/dylan/projects/disco` -> `0 errors, 0 warnings, 0 notes`
  `python development/scripts/check_arch_budget.py` -> `ARCH BUDGET OK`
  `ruff check` on new blog files/tests -> `All checks passed!`
  `ruff check --select I` on shared touched files -> `All checks passed!`
  `git diff --check` -> clean

## Notes

- A broader AppKit run without deselecting integration tests produced two failures in existing records integration tests because this sandbox cannot open sockets (`PermissionError: [Errno 1] Operation not permitted`). The same suite with `-m 'not integration'` is green, as recorded above.
