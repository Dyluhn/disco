# BP-12 report — first-class Resume (HTTP route + legality + UI gating)

**AUTHORSHIP NOTE (honest record): this report is ORCHESTRATOR-AUTHORED.**
The implementation worker (Sonnet, tmux `wkr-bp-12`, dispatched ~11:30) was
killed by the 60-minute wall-clock watchdog at ~76 min
(`.orchestrate/stop/bp-12-wall-timeout-20260610T121610.txt`) before writing
this report or its integration logs. The worker's code was COMPLETE and
productive at kill time (it was running the full test suite after its final
engine fix). The orchestrator took over: reviewed the full diff, ran every
acceptance rung itself, corrected the manifest, and wrote this report.
Nothing below is asserted on the worker's word — every result was re-run
and verified by the orchestrator.

## Step 0 — prior resume wiring (as found)

- WS `resume` frame existed (app.py) but routed to a thin kick that did not
  check legality, did not emit an environment message, and was the ONLY
  resume path — no HTTP route, so the REST surface (and anything that lost
  its WS) could not resume at all.
- Frontend `useBuildStream.resume` sent the WS frame; the Resume button
  rendered unconditionally with no legality gating (false affordance on
  RUNNING/FINISHED/ERROR).
- `LoopEngine.mode` was in-memory only — any server restart rebooted the
  loop into PLANNING, so a post-restart resume re-triggered the PLAN_NUDGE
  at finish even though the plan had been approved.

## What was built

### Backend

- **`runtime.resume_conversation(cid) -> dict`** (runtime.py): mode-agnostic,
  event-log-driven resume with an explicit legality table:
  - PAUSED → always legal.
  - IDLE → legal only with an unfinished plan (`_has_unfinished_plan`:
    a PlanEvent exists and no FINISHED StatusEvent follows).
  - RUNNING / FINISHED / ERROR → refused, `{"ok": False, "reason": …}`.
  - On legal resume: clears the cancel flag, appends **exactly one**
    `MessageEvent(source=ENVIRONMENT, content="Resumed by user.")`,
    flips `StatusEvent(RUNNING, detail="resumed")` for build/research
    (Deep Research dispatches to its existing checkpoint behavior — no
    status flip), then idempotent `kick()`.
- **`POST /conversations/{cid}/resume`** (app.py): runtime None → 409
  `runtime_unavailable`; non-ok legality → 409 with the reason; ok → 200.
  The WS `resume` frame was RE-POINTED at the same `resume_conversation`
  path — one implementation, two transports.
- **engine.py post-restart mode restoration** (out-of-manifest, accepted as
  a root-cause fix): on boot, scan `_boot_events` for
  `detail="plan_approved"` (emitted at plan approval AND by the orphan
  reconciler) vs `detail="planning"` (request_plan re-entry); when
  approved-and-not-re-entered, restore `self.mode = self._execution_mode`.
  Without this, every post-restart resume replanned. All symbol
  assumptions were verified against the live code by the orchestrator.

### Frontend

- `agent.ts` `resumeConversation(cid)` HTTP wrapper (agentLive fallback);
  `useBuildStream.resume` now calls it instead of the WS frame.
- New `canResume` memo: `status === "PAUSED" || (status === "IDLE" &&
  events.some(e => e.kind === "plan"))` — looser than the backend check but
  equivalent in practice (get_state derives status from the same event log;
  IDLE already implies no FINISHED event). BuildSurface passes
  `onResume={b.canResume ? b.resume : undefined}` so the button honestly
  hides when resume is illegal — no false affordance.
- AgentStatusBar Resume button: `onResume && (status === "PAUSED" ||
  status === "IDLE")`, aria-label "Resume the agent — continue this build
  where it left off".

## Deviations / manifest corrections (orchestrator rulings)

- **3 out-of-manifest files** touched by the worker — all reviewed and
  JUDGED JUSTIFIED, added to the manifest rather than reverted:
  `engine.py` (the mode-restoration root-cause fix), `useBuildStream.ts`
  (canResume), `BuildSurface.tsx` (wiring).
- `frontend/src/api/agent.resume.test.ts` was in the original manifest but
  never created — REMOVED from the manifest; coverage judged sufficient via
  the 12 pytest resume tests + `AgentStatusBar.resume.test.tsx`.
- Worker never verified gvisor workspace rehydration (the brief asked) —
  carried forward to bp-13's reconnect-resume rung.

## Acceptance ladder (ALL re-run by the orchestrator)

1. **Unit (python)**: `packages/agent-server/tests/test_resume.py` — 12 new
   tests (legality matrix, exactly-once env message, double-resume race,
   hermetic scripted-model harness). Agent-server suite **105/105 PASS**;
   core suite green (run per-package, never core+tools together).
2. **Unit (frontend)**: vitest **125/125 PASS** incl.
   `AgentStatusBar.resume.test.tsx`; `npx tsc --noEmit` clean.
3. **Integration rung 1** — `test-record/bp-12/_integration_runner.py` →
   `integration-resume.log`: Stop→Resume reaches FINISHED, 2 file_writes
   with NO duplicates, plan steps 1→2 with no reset, exactly-once
   "Resumed by user." env message. **PASS** (0.02s).
4. **Integration rung 2** — `_restart_survival_runner.py` →
   `restart-survival.log`: kill runtime → fresh runtime on the same store →
   reconcile → resume → FINISHED with NO re-execution of completed work.
   **PASS** (0.10s) — this is the rung that exercises the engine.py mode
   restoration.
5. **Live UI rung** — `frontend/e2e-live/bp-12-resume.spec.ts` (Firefox,
   real 27B driver, gvisor backend): real build → ≥2 observations → Stop →
   Stopped pill + Resume button → Resume → Working pill → FINISHED; event
   fingerprint asserts postPlans === prePlans (no re-plan), postActions >
   preActions (work continued), exactly-once resume env message.
   **PASS on run 5 (1.9 min)**: witness `{prePlans: 1, preActions: 3,
   postPlans: 1, postActions: 18, resumedNotes: 1, status: FINISHED}`.
   Honest run history (the resume CONTRACT executed correctly in every run
   that reached it — runs 2, 3, 4, 5; the failures were spec/infra):
   - Run 1: SPEC bug (orchestrator's) — demanded PAUSED after Stop, but the
     live-build graceful stop emits `IDLE detail="cancelled"`
     (engine.py `AgentLoop.cancel()`); PAUSED is the crash-reconciler/DR
     path. Spec fixed to accept both + assert the matching pill
     ("Stopped" per STATUS_LABEL). The IDLE-legality clause in
     `resume_conversation` is therefore the PRIMARY live path, not an edge.
   - Run 2: contract clean; spec died on a transient socket hang-up
     (uvicorn keep-alive recycle). Poll helper gained retries.
   - Run 3: contract clean, both screenshots captured; spec died on a
     45-60s server stall. Root cause found later: VM-201 degrading. This
     run ALSO surfaced the recreate-after-drop empty-workspace bug —
     sandbox recreated mid-run, agent observed "all files were lost"
     (evidence + fix order appended to bp-13's brief §2).
   - Run 4: contract clean; 10-min finish budget expired — VM-201 was by
     then OOM-wedged: 3.75/4 GiB consumed by 34 orphaned `pmx-sbx-*` + 1
     `pmx-egr-*` containers (NO orphan sweep exists — bp-13 §3, brief
     updated to include `pmx-egr-*`). Hypervisor `qm reset` + orphan reap
     required.
   - Run 5: healthy VM → clean PASS end-to-end.
   Evidence: `test-record/bp-12/ui-spec.log`, `resume-ui-witness.json`,
   `test-record/screenshots/bp-12/resume-paused.png` + `resume-running.png`
   (pixel-verified + visually confirmed by the orchestrator, SendUserFile'd).
6. `ruff check` clean on every bp-12-touched file. (11 pre-existing ruff
   errors elsewhere in bp-03/04-era files — verified NOT bp-12's, not
   blocking.)

## Process lesson (for the campaign record)

A 60-minute wall budget is too tight for orders whose acceptance includes
integration-runner debugging; the worker was killed mid-suite, productive to
the last line of its log. Subsequent orders dispatch with `--timeout-min`
75–90.
