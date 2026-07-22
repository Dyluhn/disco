# SPEC: exact plan-revision idempotence with a residual autonomous streak guard

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

1. Compare the ordered execution contract exactly: step title, detail, typed
   predicate, and dependency position. Summary/context are presentation, and
   `renamed_from` is normalized away only for duplicate comparison after an
   audited rename.
2. If that contract equals the currently approved plan and there is no newer
   USER instruction, verifier failure, or successful productive action, treat
   the proposal as idempotent in both interactive and autonomous modes and on
   both revision entry paths. Emit no PlanEvent, revision increment, or approval
   gate. Persist paired execution guidance naming the next incomplete step and
   the full current typed contract.
3. Any structural change or new causal evidence follows the normal one-gate
   approval path. This decision reconstructs from durable events, not the
   in-memory repeat counter.
4. The autonomous repeat cap remains only as a residual defense for
   non-idempotent churn. It is not the duplicate-plan correction. Its existing
   warning and shared blocked-lander path remain unchanged.

## Tests (loop-level, scripted fake provider — extend the existing autonomous plan-update coverage)

1. A workless exact duplicate is redirected before the cap with no PlanEvent or
   approval and execution continues.
2. Successful productive work makes an otherwise-identical proposal a genuine
   revision; its immediate causeless duplicate is redirected.
3. A structurally different proposal gates once; its immediate duplicate is
   redirected.
4. A new interactive USER instruction is causal evidence, so an otherwise
   identical proposal still reaches one approval gate.
5. SQLite restart reconstructs the same no-op decision.

## Verification
- PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q

## Constraints
- Smallest coherent diff; the shared blocked-lander path is untouched.
- Do NOT touch the running dev servers or the main checkout.
