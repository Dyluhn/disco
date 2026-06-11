# BP-05 — Browser-verified completion (Manus's verify rule as a gate)

**Read `README.md` first. Requires BP-04.**

## Why

Manus's production prompt rule, verbatim from the leaked schema research: *"For web
services, must first test access locally via browser"*. Today our finish path accepts an
HTTP-200 (`verify_app` / `_app_verify_command` in `loop/engine.py`) — a server that
returns 200 with a blank, JS-crashed page passes. The gate must require that the agent
actually LOOKED at its app through the BP-04 browser and that the page is not throwing.

## The decided design

Extend the existing finish-gate machinery (do not build a parallel one): the same place
that runs `_finish_verify_passed()` and the PLAN-MODE EXECUTION GATE (engine.py — locate
by the string `PLAN-MODE EXECUTION GATE` and by `_finish_verify_passed`) gains a
**browser-verify requirement** for web deliverables in Build mode.

### Definitions (exact, model-checkable)

- **Web deliverable**: at finish time, `index.html` exists at the workspace root OR
  `server_status` reports port 8000 owned by a non-`preview` session.
- **Valid verification observation**: an ObservationEvent from tool `browser` where
  `structured["url"]` starts with `http://127.0.0.1:8000` or `http://localhost:8000`,
  `success` is True, and `structured["console"]` contains **zero `error`-level entries**,
  occurring at an event seq **after** the last state-changing action (reuse the
  productive-action classification: `_NON_PRODUCTIVE_TOOLS` complement — the same set the
  execution gate uses).

### Gate behavior

On FINISH attempt in Build/execution mode where *web deliverable* holds and no *valid
verification observation* exists:

1. Refuse finish exactly the way the execution gate does (append a system nudge, continue
   the loop — no error event). Nudge text, verbatim:
   `"Before finishing: verify your app the way a user would. Use the browser tool to
   navigate to http://127.0.0.1:8000/, read the CONSOLE output, and fix any errors you
   see. Finish only after a clean load."`
2. If the last browser observation against :8000 had console errors, the nudge instead
   quotes the first error line (give the model the actual signal).
3. Hard cap: after 3 refusals without a clean verification, allow finish but emit a
   visible `MessageEvent` (environment): `"⚠ finished WITHOUT a clean browser
   verification — last console errors: …"` — surfaced in the UI feed. (Never deadlock a
   run; surface the truth instead.)
4. Non-web deliverables are untouched. The old `verify="app:url"`/`verify="static"` paths
   remain for explicit checks but no longer satisfy the web gate by themselves.

### Prompt addendum (prompts.py, same section BP-03 rewrote — append one bullet)

```
"  • Before declaring a web build finished, load it in the browser tool "
"(http://127.0.0.1:8000/) and read the console. A build you have not seen render "
"is not finished.\n"
```

## Implementation notes

- The gate reads the event list it already has at the finish branch; add a pure helper
  `def _browser_verified(events, since_seq) -> tuple[bool, str | None]` in engine.py
  (returns ok + first-error-line) with unit tests. No new state on the loop object except
  the refusal counter (pattern: existing execution-gate counter).
- `structured` is available on ObservationEvent.tool_result (`structured` field — see
  `events.py` ToolResult). If observation events drop `structured` during serialization,
  fix the serialization (it must round-trip; check `test_serialization.py` conventions).

## Acceptance

1. **Unit** (`packages/core/tests/test_finish_browser_gate.py`): scripted event lists —
   (a) web deliverable + no browser obs → refused with verbatim nudge; (b) browser obs
   BEFORE last edit → still refused; (c) clean obs after last edit → finish passes;
   (d) obs with console errors → refusal quotes the error; (e) 3 refusals → finish with
   the ⚠ environment message; (f) non-web build → gate inert.
2. **Behavioral (live driver, process backend)**: prompt a build that ships a deliberate
   `console.error` (seed it via the task prompt: "the page must call console.error('X')
   on load" — then a second run without it). Run A: agent finishes only via the 3-refusal
   ⚠ path or by removing the error — either way the event log shows the gate firing and
   the nudge with the quoted error. Run B (clean app): log shows navigate → finish with
   zero refusals. Save both event logs under `test-record/bp-05/`.
3. **UI surface (live, Firefox)** — `frontend/e2e/bp-05-verify-gate.spec.ts`: Run B
   driven through the UI; assert the feed contains the browser observation before the
   FINISHED status chip; for Run A assert the ⚠ message renders in the feed (or the fix
   happened — assert whichever the log says). Screenshots:
   `verify-clean-finish.png`, `verify-warned-finish.png` →
   `test-record/screenshots/bp-05/`, sent to user.

## Prohibitions

- Do not satisfy the gate with `verify_app`, curl, or `server_status` — only a BP-04
  browser observation counts.
- Do not block finish forever (the 3-refusal release valve is part of the spec).
- No prompt-only enforcement: the gate is code; the prompt bullet is guidance.
