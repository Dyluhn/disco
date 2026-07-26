> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era plan-progress source-unify fix plan, frozen mid-execution.
> Historical only. Not a source of current status or operating instructions.

# Fix: plan-completion reads `plan_step` only, ignores `update_plan_progress` (the #3 tool)

Source: a bias-free dual investigation (mine + a neutral codex review) of the live DeepSeek build
`conv_c93912fa` that PAUSED on the actionless valve at [822s] saying "plan steps remain undone" —
even though the agent marked all 6 steps done via `update_plan_progress` at [810s] and served a
deliverable at [814s]. Both investigations CONVERGED on the same root; codex added the view.py facet.

## Root cause (one gap, two symptoms)
The #3 redesign moved capable models onto the declarative `update_plan_progress` tool (full-state
snapshot) instead of incremental `plan_step`. But two consumers of plan-completion were never
updated and still read ONLY `plan_step(state="done")` ActionEvents:

1. `signals.py::plan_is_incomplete` (line 222; the `tool_name != "plan_step"` filter at line 249) —
   the truth-source for BOTH the FINISHED gate AND `plan_steps_complete` (line 265, the actionless
   valve's "don't pause a DONE build" guard, used at `turn_control.py:122/132/153`).
   → A capable model's "6/6 done" via `update_plan_progress` reads as 0/6 done → the FINISHED gate
   refuses AND the done-build guard fails → **the build PAUSES** ("plan steps remain undone").

2. `view.py` (lines ~212/231) — the model-facing plan/tail recap is ALSO built only from
   `plan_step` actions. → After `update_plan_progress`, the MODEL's own context still shows the plan
   as incomplete → it keeps "working" the only way left, **verifying** → the verify-spin (the
   "verification + no writing" symptom). The verification was correct behavior; the model just never
   saw that it was already done.

So: SYMPTOM A (pause-on-done) and SYMPTOM B (verify-spin / no-writing) are the SAME root — neither the
predicates nor the model recap recognize `update_plan_progress`.

## Codex review additions (round 1 → folded in)
- **CORRECTION:** `finish()` no longer reads `plan_is_incomplete` — it uses a productive-action gate
  + browser/DoD/stop hooks. So `plan_is_incomplete` now gates the ACTIONLESS/noop valve path
  (`turn_control.py`), NOT the explicit FINISHED transition. The pause is the actionless valve. The
  finish()'s productive-action gate is the INDEPENDENT guard that already blocks a bookkeeping-only
  finish — so making `update_plan_progress` authoritative for the valve's done-check does NOT enable
  a false finish (answers the risk question). Plan doc corrected accordingly.
- **MORE stale `plan_step`-only readers to fix (not just signals.py + view.py):**
  - `loop/messages.py` HS-03 recap — renders plan status from `plan_step` only AND is PERSISTED as an
    environment message, so fixing only view.py lets stale progress re-inject later. MUST fix.
  - `signals.py::plan_step_lag_signal` (line 311) — the soft-nudge auditor, plan_step-only.
  - `stuck.py:71` `_PLAN_META_TOOLS` excludes `plan_step` but not `update_plan_progress` — taxonomy
    mismatch that can create false stuck signals as reliance on update_plan_progress grows; add it.
- **Frontend:** `deriveBuildProgress` (the LIVE PlanPanel path, from #3) already handles
  update_plan_progress; legacy `derivePlanProgress` is plan_step-only — confirm it's unused; leave UI
  alone if so.
- **Semantics:** `update_plan_progress` is FULL-STATE/authoritative (schema + prompt). Merge =
  latest-event-wins; a snapshot sets the state of every step it lists; steps it omits RETAIN their
  last-known state (conservative — don't assume done, don't reset). This DELIBERATELY allows
  done→active (un-done) if a later event says so — a semantic change from today's add-only done-set;
  call it out in the docstring.
- **Test seq fallback:** some existing `plan_is_incomplete` tests use events WITHOUT seqs, so the
  `(e.seq or 0) < plan_seq` filter carries pre-replan marks forward. The shared reader must order by
  LIST POSITION relative to the plan event when seqs are absent/zero (or the tests move to sequenced
  events). Don't regress the re-plan-resets-checklist guarantee.
- **Acceptance:** add an END-TO-END loop test (`submit_plan → update_plan_progress(all done) →
  notify/noop×cap`) that lands FINISHED (not PAUSED) — proves the actual pause symptom, not just the
  helper. Plus parallel `update_plan_progress` coverage for the existing actionless completion tests.

## Fix — one shared "effective plan progress" reader
Add a single helper (in `signals.py`, the existing home of `plan_is_incomplete`) that computes the
effective per-step done-state by reading BOTH sources after the current plan, latest-event-wins:
- incremental `plan_step(index, state)` ActionEvents (existing), AND
- declarative `update_plan_progress({steps:[{index,state}...]})` ActionEvents (NEW) — a full snapshot
  that sets the entire checklist (a later snapshot supersedes earlier plan_step marks and vice-versa,
  strictly by event seq/order).
Semantics: walk events after `plan.seq` in order; a `plan_step` sets one index's state; an
`update_plan_progress` REPLACES the known states from its snapshot (it's declarative/full-state).
"done" iff the step's latest effective state is "done". Reuse this in:
- `plan_is_incomplete` (rewrite its done-set computation to use the shared reader) → fixes the
  FINISHED gate + `plan_steps_complete` + the actionless valve automatically (they all flow through it).
- `view.py` plan recap (lines ~212/231) → the model sees its `update_plan_progress` reflected, so it
  stops spinning on an apparently-incomplete plan.

## Scope / files (revised per codex)
- `packages/core/src/disco/core/loop/signals.py` — NEW shared `effective_plan_progress(events)` reader
  (merges plan_step + update_plan_progress, latest-wins, list-position fallback for seqless);
  rewrite `plan_is_incomplete` done-set to use it; `plan_steps_complete` inherits; also route
  `plan_step_lag_signal` (line 311) through it. Keep update_plan_progress in `_BOOKKEEPING_TOOLS`/
  `_NON_PRODUCTIVE_TOOLS` (it IS bookkeeping; this is about COMPLETION reading).
- `packages/core/src/disco/core/view.py` — plan/tail recap (~212/231) → shared reader.
- `packages/core/src/disco/core/loop/messages.py` — HS-03 recap → shared reader (persisted env msg).
- `packages/core/src/disco/core/loop/stuck.py` — add `update_plan_progress` to `_PLAN_META_TOOLS` (71).
- (verify only) `frontend/src/lib/buildTrace.ts` — confirm legacy `derivePlanProgress` is unused; the
  live `deriveBuildProgress` already handles update_plan_progress. Change UI only if actually wired.
- Tests: `packages/core/tests/**` — shared-reader unit tests + the e2e loop test + view/messages recap
  tests + parallel update_plan_progress coverage for the actionless completion tests.

## Acceptance gates
- New unit test reproducing the trace: a plan + an `update_plan_progress` snapshot marking all steps
  done (and NO `plan_step` events) ⇒ `plan_is_incomplete` returns (False, []), `plan_steps_complete`
  True; the actionless valve would FINISH not PAUSE. Mixed case: `plan_step` marks some + a later
  `update_plan_progress` snapshot ⇒ latest-wins resolves correctly. Re-plan resets (snapshot before
  the current plan doesn't count).
- `view.py` recap test: after an `update_plan_progress` snapshot, the model-facing plan recap shows
  the steps as done (not stuck incomplete).
- Existing plan_step-based tests STILL pass (incremental path unbroken — small models still work).
- Full pytest (core) + ruff + basedpyright (tree-wide) green. No behavior change for plan_step-only
  (small-model) builds.
- Live re-proof: re-run the DeepSeek macOS-clone build; it should FINISH on its own without the
  actionless PAUSE (no manual resume).
