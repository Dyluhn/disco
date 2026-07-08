# SPEC: identical-plan-revision streak — reset on real work + warn before halt

## The live failure (flags attempt 4, autonomous)

`packages/core/src/disco/core/loop/turn_control.py` ~1945-1990: in autonomous
mode, a proposed plan revision whose step TITLES match the prior plan
increments `self._loop._identical_plan_revisions`; at
`_PROPOSE_PLAN_UPDATE_REPEAT_CAP` the run lands STUCK(bookkeeping_only).

Two defects, proven in the event log of the failed run:
1. **The counter never resets on real work.** The model proposed the same
   revision 3× early (counter=3... at cap-1), then did the ACTUAL work —
   two file_edits, preview_start, verify_web_app — then re-proposed once more
   and was instantly STUCKed. The guidance text says "without doing real
   work"; the code never checks for work. Consecutive must mean
   consecutive-WITHOUT-INTERVENING-PRODUCTIVE-WORK.
2. **Duplicates look like success to the model.** Every duplicate proposal
   gets back `RUNNING plan_approved` — indistinguishable from a fresh
   approval — so the model cannot know it is spinning, and the cap arrives
   with ZERO warning (0→STUCK). House style is progressive: feedback first,
   then the halt.

## Required behavior

1. RESET ON WORK: when the new proposal's titles match the prior plan's,
   count it as consecutive ONLY if no productive action occurred between the
   prior PlanEvent and this one. Productive = any ActionEvent whose tool is
   NOT bookkeeping (bookkeeping = think / update_plan_progress /
   propose_plan_update / submit_plan — reuse the existing bookkeeping-tool set
   if one is defined; do not invent a divergent list). If real work intervened,
   the new identical proposal STARTS a new streak (counter = 1), not cap.
   Derive the betweenness from the EVENT LOG (the two PlanEvents bound the
   window); the in-memory counter may remain as the accumulator.
2. WARN AT CAP-1: when the counter reaches cap-1, ALSO emit a persisted
   environment MessageEvent (meta `diagnostic: "identical_plan_nudge"`)
   alongside the approval:
   "This revision is IDENTICAL to the already-approved plan — proposing it
    again does nothing. The plan is current: continue executing its steps,
    mark progress with update_plan_progress, or finish. One more identical
    proposal will halt the run."
3. The cap halt itself is unchanged (same shared blocked lander, same reason)
   — it now fires only after the warning AND only on a genuinely workless
   streak.

## Tests (loop-level, scripted fake provider — extend the existing autonomous plan-update coverage)

1. identical ×(cap-1) workless → warning diagnostic persisted once; one more
   identical workless → STUCK(bookkeeping_only) (existing behavior preserved).
2. identical ×(cap-1) → REAL WORK (a file_edit) → identical proposal again →
   counter restarted (no STUCK, no warning yet); the run continues.
3. DIFFERENT-steps proposal anywhere → counter resets (existing behavior).
4. Interactive (non-autonomous) path byte-identical to today.

## Verification
- PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q

## Constraints
- Smallest coherent diff; the shared blocked-lander path is untouched.
- Do NOT touch the running dev servers or the main checkout.
- Commit on THIS branch (wt-planstreak) with --no-verify; FINDINGS-PLANSTREAK.md.
