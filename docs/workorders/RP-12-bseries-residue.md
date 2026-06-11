# RP-12 — B-series residue: B2 kernel output discipline, B3 tail variation, B9 prefill masking

Parent plan: docs/next-fix-set-plan.md §2 RP-12 (locked). Source doc:
docs/agent-architecture-rebuild-plan.md — B2 (line ~71), B3 (~78), B9 (~139).
Read all three sections BEFORE coding. These are gap-closure debt from the
original hardening series; none got a BP order (R4 audit confirmed).

## Rung 1 — B2: kernel output discipline (kernel.py + prompts.py)

- In `packages/tools/src/perpleximanus/tools/sandbox/kernel.py` (BP-08's
  persistent kernel): when an execution result's stdout/repr exceeds a
  threshold (2000 chars), write the FULL output to a workspace file
  (`/workspace/.outputs/<cell or ts>-out.txt`), and return to context only:
  head (~500 chars) + `[full output: <path>, N bytes]`. Errors/tracebacks are
  NEVER truncated this way (B4: the model must see failures).
- In `packages/core/src/perpleximanus/core/llm/prompts.py`, add one short
  anti-dump paragraph to the build-agent guidance: prefer "run code, write
  results to a file, return the path/summary" over dumping large outputs into
  the transcript.
- Extend `packages/tools/tests/test_kernel_session.py`: big-output → file +
  head + marker; small output → unchanged; traceback → full, untruncated.

## Rung 2 — B3: deterministic-by-seq tail variation (view.py)

- In `packages/core/src/perpleximanus/core/view.py`, where ObservationEvent /
  ActionEvent render into LLM messages: rotate the SURFACE FORM (header
  phrasing / field ordering) among a small fixed set (3-4 variants), selected
  by `seq % k`. Purpose: break few-shot self-mimicry (the read-rut).
- HARD CONSTRAINTS:
  - Deterministic: the same event (same seq) must render byte-identically on
    every call — variation is ACROSS events, never within one event across
    renders. `packages/core/tests/test_view_kv_stability.py` MUST stay green.
  - Content untouched: only phrasing/ordering of the scaffold varies; tool
    names, args, results, masking behavior are unchanged.
  - Never vary the stable system/tool-def prefix (KV-cache, B5).
- NEW `packages/core/tests/test_tail_variation.py`: consecutive observation
  renders differ in scaffold; same seq renders identically twice; masked
  observations still masked under every variant.

## Rung 3 — B9: action-space masking via assistant prefill (flagged OFF)

- Mechanism only, behind a flag, NO live model validation (reviewer does the
  llama.cpp feasibility probe).
- Add `assistant_prefill: str | None = None` to `CompletionRequest`
  (`packages/core/src/perpleximanus/core/llm/types.py:85`). In
  `openai_provider.py`, when set, append a partial assistant message with that
  content to the outgoing messages (prefill seam). Both complete and
  stream_complete paths.
- In `packages/core/src/perpleximanus/core/loop/engine.py`: ONE genuine
  lifecycle use, gated by a config flag (default OFF — follow how existing
  PMX_* flags reach the engine; if none do, read the flag from env in the
  loop construction seam, not deep in step code): while
  `mode == OperatingMode.PLANNING` (engine.py:894 region), set the request's
  assistant_prefill to the tool-call opening the driver template expects, to
  force a tool call rather than prose. Keep it minimal and clearly marked
  EXPERIMENTAL; never apply outside PLANNING; never use as a loop guard.
- Unit tests (NEW `packages/core/tests/test_prefill_masking.py`): request
  carries prefill only when flag on + PLANNING; provider serializes the
  partial assistant message; flag off → byte-identical requests to today.

## Run ONLY

`uv run pytest packages/tools -q` and `uv run pytest packages/core -q`,
teed to `test-record/rp-12/units-tools.log` / `units-core.log`.
Do NOT start servers or run e2e — a harness may be live on :8000.

## Anti-scope

- No tool renaming (B9's prefix-group masking variant is OUT — Required-style
  only).
- No llama.cpp relaunch/template work (reviewer's).
- No engine behavior change with the flag off — assert this in tests.
- No changes to runtime.py, frontend, or retrieval.

## Manifest (the ONLY files you may touch)

- packages/tools/src/perpleximanus/tools/sandbox/kernel.py
- packages/tools/tests/test_kernel_session.py
- packages/core/src/perpleximanus/core/llm/prompts.py
- packages/core/src/perpleximanus/core/llm/types.py
- packages/core/src/perpleximanus/core/llm/openai_provider.py
- packages/core/src/perpleximanus/core/view.py
- packages/core/src/perpleximanus/core/loop/engine.py
- packages/core/tests/test_tail_variation.py
- packages/core/tests/test_prefill_masking.py
- test-record/rp-12/units-tools.log
- test-record/rp-12/units-core.log
- agent-projects/gemini/rp-12-report.md

## Report

`agent-projects/gemini/rp-12-report.md`: per-rung summary, test counts,
deviations flagged at top with justification.
