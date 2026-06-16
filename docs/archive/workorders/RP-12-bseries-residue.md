# RP-12 — B-series residue: B2 kernel output discipline, B3 tail variation, B9 prefill masking

Parent plan: docs/next-fix-set-plan.md §2 RP-12 (locked). Source doc:
docs/agent-architecture-rebuild-plan.md — B2 (line ~71), B3 (~78), B9 (~139).
Read all three sections BEFORE coding. These are gap-closure debt from the
original hardening series; none got a BP order (R4 audit confirmed).

## Rung 1 — B2: kernel output discipline (kernel.py + prompts.py)

- In `packages/tools/src/disco/tools/sandbox/kernel.py` (BP-08's
  persistent kernel): when an execution result's stdout/repr exceeds a
  threshold (2000 chars), write the FULL output to a workspace file
  (`/workspace/.outputs/<cell or ts>-out.txt`), and return to context only:
  head (~500 chars) + `[full output: <path>, N bytes]`. Errors/tracebacks are
  NEVER truncated this way (B4: the model must see failures).
- In `packages/core/src/disco/core/llm/prompts.py`, add one short
  anti-dump paragraph to the build-agent guidance: prefer "run code, write
  results to a file, return the path/summary" over dumping large outputs into
  the transcript.
- Extend `packages/tools/tests/test_kernel_session.py`: big-output → file +
  head + marker; small output → unchanged; traceback → full, untruncated.

## Rung 2 — B3: deterministic-by-seq tail variation (view.py)

- In `packages/core/src/disco/core/view.py`, where ObservationEvent /
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
  (`packages/core/src/disco/core/llm/types.py:85`). In
  `openai_provider.py`, when set, append a partial assistant message with that
  content to the outgoing messages (prefill seam). Both complete and
  stream_complete paths.
- In `packages/core/src/disco/core/loop/engine.py`: ONE genuine
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

## Amendment (2026-06-11) — OSS-harvest weak-model FC kit (rungs 4-7)

Folded from docs/next-fix-set-plan.md §7 steals #1/#6/#7 + §8 (OpenCode
invalid-tool reroute), per the ratified order-impact note. Defense-in-depth
ordering: grammar (4) prevents most syntax errors at decode time → reroute (7)
catches tool-NAME hallucinations that are valid JSON → repair (5) salvages
malformed JSON when grammar is off/unavailable → requery (6) is the bounded
last resort. Build 5 → 7 → 6 → 4 (each is independently shippable; 4 last
because it's flagged-off mechanism only).

### Rung 4 — grammar-constrained tool calls (llama.cpp json_schema), flagged OFF

- In `openai_provider.py`: when a new config flag (follow Rung 3's flag
  pattern; default OFF) is set and the request carries tools, translate the
  tool schemas into the llama.cpp `json_schema`/grammar request field so
  invalid tool-call JSON is rejected at DECODE time. Both complete and
  stream_complete.
- Mechanism only — NO live llama.cpp validation (reviewer's, same posture as
  Rung 3). Flag off → byte-identical requests to today (assert in tests).

### Rung 5 — layered tool-call repair (Pi pattern)

- At the provider's tool-call parse seam in `openai_provider.py`: before
  rejecting a malformed tool-call payload, attempt (a) strict parse, (b)
  mechanical JSON repair — strip markdown fences, balance braces/brackets,
  escape bare control chars, (c) type-coercing validation against the tool's
  schema (string "42" → int 42, "true" → bool; NEVER coerce across
  incompatible types). Each stage only runs if the previous failed; log which
  stage rescued the call (structured log field, for later eval counts).
- Unrecoverable → surface the existing parse failure unchanged (Rung 6 takes
  over in the engine).

### Rung 6 — bounded requery-outside-log (SWE-agent pattern)

- In `engine.py`, at the seam where a model response fails to yield a valid
  action: retry the SAME query up to 2 times WITHOUT persisting the malformed
  attempt to the event store — the malformed text + a one-line corrective
  hint go only into the TRANSIENT retry request, never into durable history
  (post-resume re-reads must not see the garbage).
- After the bound: ONE refusal-with-feedback enters the log (the existing
  failure path), so the valve/stuck machinery sees exactly one event, not
  three.

### Rung 7 — invalid-tool reroute (OpenCode pattern)

- In `engine.py`, at action validation: a syntactically-valid call naming a
  tool that is NOT in the current step's offered toolset routes to an
  internal handler that emits a normal failed ToolResult with actionable
  feedback ("tool 'search_web' does not exist; available now: ...the offered
  list..."), instead of an agent_error loop.
- MUST respect DC-05 withholding: the suggestion list is `_tools_for_step()`'s
  CURRENT offer — a withheld tool's name must never appear in the feedback
  (it would advertise the meta tools the withholding hides).
- Interplay: this replaces neither the serve/remember/ask backstops (those
  intercept OFFERED virtuals) nor the withholding itself — it only converts
  unknown-name errors into feedback.

### Amendment tests

NEW `packages/core/tests/test_toolcall_defense.py`:
- repair: fenced JSON → parsed; unbalanced brace → parsed; "42" vs int schema
  → coerced; garbage → rejected unchanged.
- requery: malformed → no new store event + same-query retry; 2 failures then
  valid → only the valid action persisted; 3 failures → exactly ONE refusal
  event.
- reroute: unknown tool name → failed ToolResult naming only currently-offered
  tools; under a fresh post-resume session (withheld set) the feedback lists
  the LEAN toolset only.
- grammar flag: on → request carries the schema field; off → byte-identical.

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

- packages/tools/src/disco/tools/sandbox/kernel.py
- packages/tools/tests/test_kernel_session.py
- packages/core/src/disco/core/llm/prompts.py
- packages/core/src/disco/core/llm/types.py
- packages/core/src/disco/core/llm/openai_provider.py
- packages/core/src/disco/core/view.py
- packages/core/src/disco/core/loop/engine.py
- packages/core/tests/test_tail_variation.py
- packages/core/tests/test_prefill_masking.py
- packages/core/tests/test_toolcall_defense.py
- test-record/rp-12/units-tools.log
- test-record/rp-12/units-core.log
- agent-projects/gemini/rp-12-report.md

## Report

`agent-projects/gemini/rp-12-report.md`: per-rung summary, test counts,
deviations flagged at top with justification.
