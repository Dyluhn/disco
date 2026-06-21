# frontend/e2e-full/fixtures

Minimized event-log fixtures for the W9 frontend replay test suite
(`src/lib/buildTrace.replay.test.ts`). Each file is a small, hand-written
JSON array of `AgentEvent` objects plus an `expect` block and a `_why` note.
They are NOT giant recorded logs — the point is that they are minimal, human-
readable, and durable.

Design principle: the test's **input** is a real event stream shape; the
**output** is the real derived value from the production selector. No deriver
internals are mocked.

## Fixture index

| file | status | bug / behaviour locked in |
|---|---|---|
| `no_plan_progress_finished.json` | FINISHED | **#3 regression** — small/local model (e.g. Qwen) finished without ever calling `update_plan_progress`; `deriveBuildProgress` pre-fix returned an empty map and showed 0/4 steps done on a build that actually completed. The terminal-reconciliation path MUST set all steps to `done`. |
| `running_then_error.json` | ERROR | A RUNNING build transitions to ERROR via a `SandboxFileNotFoundError`. Guards: `deriveLiveSignal` must return `idle` on ERROR (not a confusingly active signal); the active progress step must become `stalled`; the errored action must appear as `status=failed` in `deriveActivity`. |
| `tool_error_visible.json` | ERROR | An `agent_error` event (tool call crashed, not just returned non-zero) for a `shell` action must surface in `deriveActivity` as `status=failed` with the error text in `expandable.error`. Distinct from a `success=false` observation — pre-gap had no dedicated test for the `agent_error` path. |
| `finished_with_partial_plan_updates.json` | FINISHED | A capable-model build where `update_plan_progress` was called mid-build (step 1 done, step 2 active, step 3 pending) but NO final 100%-done snapshot was sent before FINISHED. `deriveBuildProgress` MUST reconcile all steps to `done` — the stale partial snapshot must not leave the UI showing "1/3 done" after a successful build. |
| `cancelled_request.json` | IDLE | A build cancelled mid-run (user hit Cancel; loop stops; status → IDLE). `deriveLiveSignal` must return `idle`; the active step must become `stalled`; the already-done step must stay `done`. |

## Negative fixtures (W18, consumed by other layers)

The `packages/agent-server/tests/fixtures/negative/` directory mirrors
`no_plan_progress_finished.json` and contains additional fixtures consumed by
W14a (artifact validators) and W8 (scripted-agent tests):

| file | consumed by |
|---|---|
| `no_plan_progress_finished.json` | W9 frontend replay (this directory) |
| `procedural_image_deck.html` + `procedural_placeholder.png` | W14a deck provenance validator |
| `raw_html_deck_deliverable.json` | W14a default-format validator |

## Shape

```jsonc
{
  "events": [ /* array of AgentEvent objects (minimized, may omit optional fields) */ ],
  "final_status": "FINISHED" /* | "ERROR" | "IDLE" | ... */,
  "expect": { /* test-readable assertions */ },
  "_why": "human note on the bug/behaviour this guards"
}
```
