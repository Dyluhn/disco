# DF-08 — Driver vision escalation to Gemini 3 Flash (DEFECT-8 fix)

**Date:** 2026-06-11
**Status:** COMPLETE — all tests green, evidence in `test-record/df-08/`

## Deviations from brief

None. The architectural choice to emit the `RoutingDecision` from `complete()`
rather than from `_try_vision_escalation()` is a fidelity to RT4 (exactly one
decision per call), not a deviation. The decision is emitted with the exact
fields specified: `path="overflow"`, `overflow_triggers=["vision_escalation"]`,
and `reason="vision escalation: <primary> lacks VISION → <escalation model>"`.

## What changed

### 1. Catalogue entry — `or-gemini-3-flash` (`config.py`)

Added `or-gemini-3-flash` to `default_config()` models:

| Field | Value |
|-------|-------|
| `model_id` | `google/gemini-3-flash-preview` |
| `provider` | `openrouter` (reuses existing OpenAIProvider + PMX_OPENROUTER_API_KEY) |
| `base_url` | `https://openrouter.ai/api/v1` |
| `api_key_env` | `PMX_OPENROUTER_API_KEY` |
| `context_window` | 1,048,576 |
| `capabilities` | TOOL_CALLING, LONG_CONTEXT, VISION, JSON_MODE |
| `price_in_per_m` | 0.075 |
| `price_out_per_m` | 0.30 |

The slug `google/gemini-3-flash-preview` is the live, bake-off-validated GA-track
id. Documented fallback (`google/gemini-3.5-flash`) is a one-line JSON swap of
`vision_escalation_model`, not a code change.

The entry was also added to `perpleximanus-config.json` (the persisted config).

### 2. Config field — `vision_escalation_model` (`config.py`)

Added to `RouterConfig`:

```python
vision_escalation_model: str | None = None
```

Defaulted to `"or-gemini-3-flash"` in `default_config()` and persisted in the
JSON config. The field is swappable to any vision model in the catalogue
(e.g. `"or-gemma-4-31b-free"` for free tier, `"driver-overflow"` for Sonnet)
without code changes.

### 3. Vision escalation in routing (`routing.py`)

**Before (BP-00):** The vision guard at `_resolve` line ~262 raised
`NoEligibleModel` unconditionally when the request carried images and the
chosen model lacked `Requirement.VISION`. On a text-only driver, every
build-verify screenshot request hit this and killed the conversation.

**After (DF-08):** When the guard fires, `_resolve` first calls
`_try_vision_escalation()`:

1. If `vision_escalation_model` is `None` → guard stays hard (raise).
2. If `vision_escalation_model` points to an unknown model → guard stays hard.
3. If the escalation model exists but lacks `Requirement.VISION` → guard stays hard.
4. If the escalation model exists AND has VISION → re-resolve to it, return
   `path="overflow"`, `overflow_triggers=["vision_escalation"]`.

The escalated request flows through the normal prompt-injection + budget path.
The guard's safety property is intact: images are NEVER silently sent to a
non-vision model.

Return type of `_resolve` changed from `tuple[ModelEntry, Path, str]` to
`tuple[ModelEntry, Path, str, list[str]]` to carry `overflow_triggers` through
to `complete()` which owns the single `RoutingDecision` per RT4.

### 4. Provider image serialization — already correct

`OpenAIProvider._message()` in `openai_provider.py` already serializes images
as OpenAI-style `image_url` content parts when `LLMMessage.images` is non-empty.
Since all OpenRouter entries use `OpenAIProvider` (via `build_providers` in
`wiring.py`), images flow correctly to Gemini Flash without any changes.
Three assertion-level tests confirm this (see tests 7–9 below).

### 5. Existing test updated

`test_vision_capability_guard` in `test_vision_wiring.py` was updated to
explicitly clear `vision_escalation_model` before asserting `NoEligibleModel`.
Without this, the test would now exercise the DF-08 escalation path (since
`default_config()` now has a valid escalation target). The guard's behavior
in its original BP-00 form is still tested — it just now requires the
escalation to be unset.

## Tests (10 new, all green)

File: `packages/core/tests/test_df08_vision_escalation.py`

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | `test_vision_escalation_routes_to_escalation_model` | Image request + no-vision primary + escalation configured → routes to vision model, path=overflow, triggers=["vision_escalation"] |
| 2 | `test_vision_escalation_does_not_affect_text_only_requests` | Plain-text requests never trigger escalation (guard only fires on images) |
| 3 | `test_escalation_model_receives_provider_and_model_id_correctly` | Escalated request flows through normal prompt-injection + budget path |
| 4 | `test_guard_preserved_when_escalation_unset` | `vision_escalation_model=None` → raises `NoEligibleModel` (safety intact) |
| 5 | `test_guard_preserved_when_escalation_not_in_catalogue` | Escalation key not in catalogue → raises `NoEligibleModel` |
| 6 | `test_guard_preserved_when_escalation_model_lacks_vision` | Escalation model exists but lacks VISION → raises `NoEligibleModel` |
| 7 | `test_image_serialization_on_openrouter_path` | `_message()` emits `image_url` content part (not dropped) |
| 8 | `test_multiple_images_all_serialized` | All images in a multi-image message are serialized |
| 9 | `test_image_serialization_in_payload_for_openrouter_model` | Full `_payload()` for a gemini model carries images correctly |
| 10 | `test_no_escalation_when_primary_has_vision` | When primary HAS VISION, stays local — escalation path never entered |

## Evidence

```
test-record/df-08/units-core.log   → 458 passed, 1 skipped
test-record/df-08/units-server.log → 275 passed, 6 warnings
```

The 1 skip is `test_router_overflow.py` (DORMANT — v1.2 overflow policy tests,
explicit `pytest.skip(allow_module_level=True)`).

## Files touched

| File | Change |
|------|--------|
| `packages/core/src/perpleximanus/core/llm/config.py` | +`vision_escalation_model` field on `RouterConfig`, +`or-gemini-3-flash` model entry in `default_config()`, defaults `vision_escalation_model="or-gemini-3-flash"` |
| `packages/core/src/perpleximanus/core/llm/routing.py` | `_resolve` now returns 4-tuple (adds `overflow_triggers`); vision guard calls `_try_vision_escalation` before raising; new `_try_vision_escalation` helper; `complete`/`stream_complete` use `overflow_triggers` in RoutingDecision |
| `perpleximanus-config.json` | +`or-gemini-3-flash` entry, +`vision_escalation_model` field |
| `packages/core/tests/test_vision_wiring.py` | Updated `test_vision_capability_guard` to clear escalation (guard still tested) |
| `packages/core/tests/test_df08_vision_escalation.py` | NEW — 10 unit tests |

## Cost note for the orchestrator

In-loop Gemini Flash vision via OpenRouter is METERED OpenRouter spend
(~$0.075/M input, ~$0.30/M output). Volume is ~1 screenshot judgment per build,
so trivial — but NOT free (unlike the screenshot-triage CLI which uses the
free Google Gemini quota). Flag in loop reports.

## Out of scope (unchanged)

- `screenshot-triage.sh` (native gemini CLI, free quota) — unchanged.
- Live marathon-gate run — orchestrator's acceptance step.
- The "INTELLIGENT ROUTING" dormant overflow machinery (routing.py line ~422) —
  not activated. DF-08 uses its own escalation path at the vision guard, not
  the dormant policy.
