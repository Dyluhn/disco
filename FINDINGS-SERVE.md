# Serve + Elision Findings

## What Changed

- `serve` now normalizes model-facing `/workspace/...` paths before duplicate and existence checks.
  - `/workspace/sub/file.html` is checked and emitted as `sub/file.html`.
  - Root-like paths (`/workspace`, `/workspace/`, `.`, `./`, empty) are not valid deliverable entry paths.
  - If a root-like path is passed and `index.html` exists, `serve` auto-coerces to `index.html` and emits that deliverable.
  - If no root entry is present, the refusal now gives the entry-file recipe instead of saying the root does not exist.
  - Missing non-root paths now report the normalized path that was checked and avoid the old "Create it" wording.

- History elision now emits one canonical sentinel:
  - `[[DISCO-ELIDED: N chars — history display only; file_read the path if you need this content]]`
  - `_ELISION_MARKER_RE` and the tool-side direct guards detect the new sentinel and historical angle-bracket markers.
  - `_snip_args`, retargeting, prompts, observer/executor rejection messages, and direct file-tool refusals were updated together.

## Verification

- Reproduced the pre-fix failures with the new focused serve/elision regression tests.
- Passed focused core regression suite:
  - `pytest packages/core/tests/test_serve_output_truth.py packages/core/tests/test_k1_elision_guard.py packages/core/tests/test_context_budget.py packages/core/tests/test_f8_arg_truncation.py packages/core/tests/test_cw_p1_codex.py packages/core/tests/test_cw3_cache_prefix.py -q`
- Passed tool-side elision guard suite:
  - `pytest packages/tools/tests/test_executor_elision_guard.py packages/tools/tests/test_safe_write_file.py packages/tools/tests/test_run_project_script.py packages/tools/tests/test_exact_replace.py packages/tools/tests/test_fresh_edit_guard.py -q`
- Passed full core suite:
  - `pytest packages/core/tests -q`
- Full agent-server suite completed with one timing failure unrelated to this change:
  - `packages/agent-server/tests/test_host_proxy_C2.py::test_5xx_passthrough_no_retry`
  - Failure was `0.356s` elapsed against a `0.1s` timing threshold; isolated rerun of that test passed.

All commands used:

```sh
PYTHONPATH=$(ls -d $PWD/packages/*/src | tr '\n' ':') /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest ...
```

## Deviations

- `core` does not hard-import `disco.tools` at module import time. The serve normalizer lazy-loads `disco.tools.sandbox.base.strip_redundant_workspace_prefix` when available and keeps an exact fallback mirror for core-only installs.
- No `DeliverableEvent` schema field was added for auto-coercion notes. The coercion is logged, and the emitted deliverable path is the corrected `index.html`.
