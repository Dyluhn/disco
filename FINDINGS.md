# GLM Dead-Turn Widen Findings

## What changed

- Widened the OpenAI-compatible provider's empty-turn metadata classification:
  `finish_reason=="stop"`, zero visible content, and zero tool calls now
  classifies whether or not reasoning text was present. The existing
  `empty_reasoning_only` metadata key and payload shape are unchanged.
- Added a loop-level one-shot repair for execution-mode prose noops: when a
  build step returns visible prose with no tool call, the driver emits an
  ENVIRONMENT diagnostic/reminder marked `prose_noop_repair`, retries once with
  a transient reminder, and avoids counting that first repaired turn as a noop.
- Made the prose repair segment-scoped via the event log, so it survives loop
  recreation and does not fire a second time later in the same execution
  segment.
- Added provider and scripted fake-provider loop regressions for truly blank
  turns, prose noops, retry fallthrough, second prose noops, planning-mode prose,
  and independent empty/prose repair counters.

## Reproduction Evidence

- Before the source fix, the focused regression command failed as expected:
  `test_streaming_blank_stop_response_is_flagged` raised `KeyError:
  'empty_reasoning_only'`, and
  `test_prose_noop_in_execution_retries_once_and_executes_tool` showed the next
  request still ended with the model's prose instead of the transient
  tool-call reminder.

## Verification

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests/test_openai_provider.py packages/core/tests/test_loop_integration.py -q`
  passed, `34 passed`.
- Focused affected tests plus touched integration/provider tests passed,
  `36 passed`.
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests -q`
  completed with only the two known baseline F5 failures:
  - `packages/core/tests/test_f5_thinking_budget.py::test_user_and_tool_literal_think_is_NOT_stripped`
  - `packages/core/tests/test_f5_thinking_budget.py::test_assist_truncation_still_fires_for_non_assistant_role`
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/agent-server/tests -q`
  completed with one unrelated timing failure:
  `packages/agent-server/tests/test_host_proxy_C2.py::test_5xx_passthrough_no_retry`
  observed `0.181s` for an assertion requiring `<0.1s`; the same assertion also
  proved no retry occurred (`request_count == 1`).
- The isolated agent-server timing test passed on rerun.
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m ruff check --select F packages/core/src/disco/core/llm/openai_provider.py packages/core/src/disco/core/loop/driver.py packages/core/src/disco/core/loop/signals.py packages/core/tests/test_openai_provider.py packages/core/tests/test_loop_integration.py packages/core/tests/test_dc05_loop.py packages/core/tests/test_loop_extra.py`
  passed.

## Deviations / Code-Reality Notes

- Kept `_empty_reasoning_only_metadata(...)` and the
  `EMPTY_REASONING_ONLY_METADATA_KEY` name stable even though the classification
  now also covers truly blank stop finals.
- The loop-level prose diagnostic records `content_len`, `tool_call_count`, and
  `llm_response_id`. It does not record `finish_reason` because `AgentStep`
  does not carry provider finish metadata across the agent/driver boundary.
- The prose nudge is per execution segment, not per consecutive-noop streak.
  Real actions do not re-arm it; fresh user/resume/plan-approval boundaries do.
- Existing scripted `AgentStep` tests that used prose-only noops were adjusted
  because the repair is intentionally loop-level and applies to scripted fake
  agents as well as the router-backed fake provider.

# Mode Desync Findings

## Root Cause

Post-approval turn composition had two phase readers. The current objective
recitation came from durable plan/progress events, but the system prompt and
tool surface came from the in-memory `AgentLoop.mode` cache. A loop or runtime
re-kick that booted with `mode=PLANNING` after a durable `plan_approved` marker
therefore composed a self-contradictory driver request: execution objective in
the tail, planning prompt, and planning-only tools.

The fix extracts `signals.effective_mode(...)` from the existing
`plan_approved` vs later `planning` predicate and reconciles the loop cache from
that signal at run/drive boundaries. Driver composition now passes the effective
mode into both tool-surface selection and `agent.step(... mode=...)`. If a stale
planning cache is corrected while a `<current-objective>` is present, the driver
logs a loud mode-desync correction and uses the event-log verdict.

## Regression Evidence

- Reproduced before the fix with
  `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests/test_mode_desync.py -q`.
- The failing assertion captured the post-approval `CompletionRequest` with
  `req.profile.mode == OperatingMode.PLANNING` after a durable
  `plan_approved` event and an execution `<current-objective>` tail.
- The regression now covers:
  - `questions_v2` intake -> user steer answer -> `think` -> `submit_plan` -> approve -> stale planning cache.
  - Plain plan approval with a stale planning cache.
  - Autonomous inline approval with a stale planning cache.
- The same file now passes: `3 passed`.

## Verification

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests/test_mode_desync.py packages/core/tests/test_plan_approval_execution.py packages/core/tests/test_planner_gate.py packages/core/tests/test_bug12_followup_replan.py packages/core/tests/test_replan_pending_valve.py packages/core/tests/test_autonomous_mode.py -q` - passed, `43 passed`.
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q` - completed with 2 failures in pre-existing provider tests:
  - `packages/core/tests/test_f5_thinking_budget.py::test_user_and_tool_literal_think_is_NOT_stripped`
  - `packages/core/tests/test_f5_thinking_budget.py::test_assist_truncation_still_fires_for_non_assistant_role`
- Those failures reproduce in isolation and are unrelated to this mode fix. They
  come from an existing contract conflict: F5 tests expect orphan `role:"tool"`
  messages to remain on the wire for literal `<think>` preservation, while
  `test_tool_call_ordering.py::test_orphan_tool_result_is_dropped` pins the
  provider normalizer to drop orphan tool results for OpenAI-compatible adjacency.
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m ruff check --select F packages/core/src/disco/core/loop/driver.py packages/core/src/disco/core/loop/engine.py packages/core/src/disco/core/loop/signals.py packages/core/tests/test_mode_desync.py` - passed.

## Deviations / Code-Reality Notes

- Used a loop-level regression with `BuildAgent`, `DefaultLLMRouter`, and a
  scripted fake provider instead of driving a live server. This matches the
  environment note preference and avoids touching running dev servers.
- `signals.effective_mode(...)` accepts a `default` mode for logs with no
  planning lifecycle markers so non-build surfaces do not get forced into
  PLANNING. Build loops still default to PLANNING before approval.
- The spec mentioned a temporary `openai_provider.py` `_wire_dump` helper, but no
  such helper exists in this worktree, so there was nothing to remove.

# Provider Keys Findings

## What changed

- Added persisted provider objects with encrypted `provider_<id>` secrets, server-side presets, kind-specific `/models` probing, TTL cache + singleflight, and enable/disable routes that create/remove ordinary catalogue entries.
- Added normalized catalogue support for OpenAI-compatible, Anthropic, and Gemini shapes.
- Added the generic Providers UI: preset + key add flow, inline browse/search, toggles, unknown-pricing display, and manual model-ID add when `/models` is unavailable.
- Kept the existing dedicated OpenRouter section/routes intact and visible, including the existing image-pricing enrichment path.

## Verification

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/app-server/tests -q` — passed.
- `cd frontend && npm ci` — completed.
- `cd frontend && npx vitest run src/components/settings/` — passed.
- `cd frontend && npm run typecheck:build` — passed.

## Deviations / Code-Reality Notes

- Persistence is backed by an inert `RouterConfig.providers` field so provider definitions survive the existing `ConfigStore` load/save cycle. The agent/runtime still only consumes enabled catalogue models.
- The existing model catalogue schema has no explicit label field and no first-class “unknown pricing” state. The browse UI shows `pricing unknown`; enabled models use the existing `ModelUpsert` path with price `0` when unknown and can be edited afterward.
- The generic OpenRouter preset exists, but the old OpenRouter section remains in the Settings Providers area because the spec explicitly said not to break the existing OpenRouter section/routes this pass.
- `ProviderPresetDTO` includes `requires_base_url` for the Custom preset so the frontend does not hardcode which preset needs a URL.
- `ImageGenSection.test.tsx` had a brittle `findByRole("status")` assertion that failed because the component can render both the not-configured warning and the offline-test status. The assertion now targets the specific not-configured text.
