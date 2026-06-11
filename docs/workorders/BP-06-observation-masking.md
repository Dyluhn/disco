# BP-06 — Observation masking + restorable references; KV-prefix discipline

**Read `README.md` first. Independent of the S-series; do after BP-03.**

## Why (evidence-graded — this is the [C]ontrolled item)

arXiv 2508.21433 (the observation-masking study): masking old tool-output bodies while
keeping all reasoning/actions matches or beats LLM summarization at ~half the cost, in
exactly our file-heavy regime. Our current context hygiene is a destructive render-time
snip (`snip_content`, `core/events.py` — 8,000 chars, head 5,000 / tail 2,000) applied to
EVERY observation regardless of age, plus an LLM condenser. Old observations waste
context; recent big ones get mangled. Masking replaces age-blind truncation with
age-aware elision behind a restorable pointer.

## The decided design

Masking happens in the **View projection** (`core/view.py`, `View.of` — the pure
function), never in the store: the event log keeps full bodies forever (replay/restore
depends on this).

Constants (view.py, top, with the other knobs):

```python
_MASK_KEEP_RECENT = 8      # newest N observation events render in full
_MASK_MIN_CHARS = 600      # smaller bodies are never masked (cheap, often load-bearing)
```

Rules — exact:

1. Walk order unchanged. Identify the seqs of the last `_MASK_KEEP_RECENT`
   ObservationEvents in the (post-condensation) event sequence. Every OTHER
   ObservationEvent whose `tool_result.content` length > `_MASK_MIN_CHARS` renders as the
   stub (single line, exact format):
   `[masked output: {tool_name} #{seq} — {n_chars:,} chars, sha256:{hash12}. Re-run the
   tool (or file_read the same path) to see it again.]`
2. **Never masked**: AgentErrorEvents (B4 — errors are feedback; smoothing failures
   provably induces loops), pinned events (the existing `pinned` set: latest PlanEvent,
   KnowledgeEvents, DatasourceEvents), and MessageEvents.
3. Recent-window observations still pass through `snip_content` as today (8,000-char
   ceiling stays as the catastrophic-output stop).
4. ActionEvents are untouched (reasoning/actions stay verbatim — that's the technique).

### KV-prefix discipline (B5) — make the invariant testable

The View must be **append-stable** except at two known boundaries:

- the GAP D tail recitation (ephemeral, by design at the very end), and
- the single observation that crosses the `_MASK_KEEP_RECENT` boundary each turn
  (full → stub). One boundary change per appended observation; nothing else may differ.

Add `View.fingerprint()` → list of `sha256(role + content)[:16]` per message, and the
invariant test below. Hunt down any nondeterminism it catches: timestamps rendered into
messages, dict-ordering in `_snip_args`, set iteration in pinned collection, etc. Fix at
the source (sort, freeze, or drop the timestamp) — fixing the test is forbidden.

## Implementation

- `view.py`: masking inside `View.of` where ObservationEvents are rendered (they
  currently call `ObservationEvent.to_llm_message()`); add an optional
  `to_llm_message(masked=True)` or render the stub inline in view.py — choose the
  smaller diff, keep `events.py` semantics pure.
- The stub keeps `tool_call_id` so provider-side tool-call pairing stays valid.
- Condenser interaction: masking runs on the post-tombstone sequence; the condenser's
  token estimate must see the MASKED rendering (it already estimates from the rendered
  view — verify by reading `should_condense` call sites in engine.py; if it estimates
  from raw events, fix it to estimate from the view).

## Acceptance

1. **Unit** (`packages/core/tests/test_observation_masking.py`): rules 1–4 each have a
   test; stub format exact-match; tool_call_id preserved; error events with 50KB bodies
   never masked.
2. **Invariant test** (`test_view_kv_stability.py`): build 60 synthetic events; for k in
   30..60: `View.of(events[:k])` vs `View.of(events[:k+1])` — fingerprints identical
   except (a) appended messages, (b) the tail recitation, (c) exactly ≤1 full→stub flip.
   Also: two `View.of` calls in fresh processes (subprocess round-trip) → identical
   fingerprints.
3. **Replay A/B (the real harness — no synthetic content)**: `make replay` /
   `harness/` EE-Quest cassette: report total prompt tokens per step before vs after.
   Required: ≥30% cumulative prompt-token reduction by step 40, AND the replayed run
   still reaches the same terminal state. Paste the table into the report.
4. **Behavioral (live driver, process backend)**: one real build (the EE-Quest v2 prompt
   from `test-record/RECORD.md`) end-to-end; assert no context-window hard-reset fired
   (grep the event log for the hard-reset marker) and the build finishes. Save log under
   `test-record/bp-06/`.
5. **UI surface (live, Firefox)**: masking must be INVISIBLE to the user — the Activity
   feed renders full observation bodies from the event store, not the masked view. Spec
   `bp-06-feed-unmasked.spec.ts`: after the live run, an old observation in the feed
   still shows its real content (not the stub). Screenshot →
   `test-record/screenshots/bp-06/feed-unmasked.png`, sent to user.

## Prohibitions

- No masking in the store/event log. No LLM summarization added anywhere (controlled-
  negative). No changes to `_OBS_SNIP_*` values.
- Do not touch the read-counter family here (BP-07 owns its removal).
- The A/B in step 3 runs on the captured real cassette — building a synthetic cassette
  for it is a violation.
