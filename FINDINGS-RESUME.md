# Blocked Answer Resume Findings

## Reproduction

- Reproduced before the fix with the real plan-gated loop, `DiscoKernel.send_user_turn`, and a scripted fake provider.
- A blocked landing answer sent as a steer (`Continue with the approved plan and build step 1.`) wrote `revision_steer_pending`.
- The next fake-provider driver request was composed in `OperatingMode.PLANNING`, and a new planning marker was emitted instead of resuming the approved execution segment.

## Changes

- Added event-log signals for:
  - the current status gate being a blocked `AWAITING_USER_QUESTION` landing;
  - the latest user message being an answer to a blocked landing, even after `run()` emits its bare `RUNNING` resume marker.
- Updated `DiscoKernel.send_user_turn` to skip `revision_steer_pending` for blocked-landing answers only.
- Updated `AgentLoop.send_message(steer=True)` and the loop follow-up replan consumer to treat blocked-landing answers as model context, not host-side revision steers.
- Added scripted-provider regression coverage for execution resume, model-initiated plan update, ordinary non-blocked question behavior, and pending revision planning.

## Deviations From Spec

- Current code stamps `revision_steer_pending` only when `signals.is_revision_intent(text)` is true. That predicate treats ambiguous replies like `continue` as revision intent, so the live failure still reproduces, but not every possible steer text is stamped.
- The model-facing loop does not expose a `request_plan` tool. The implemented model-initiated revision path is `propose_plan_update`; the test uses that existing path. Because the fresh-session guard intentionally refuses `propose_plan_update` before any real action, the fake first performs a read and then proposes the plan update.
- Suppressing only the kernel marker was insufficient in code reality: after the answer, `AgentLoop.run()` emits a bare `RUNNING` marker, and the mid-loop fallback text heuristic could still re-enter planning. The fix therefore also guards the loop consumer using the latest-user-answer signal.

## Verification

- `PYTHONPATH=/home/dylan/projects/disclaude-wt-blockresume/packages/agent-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/app-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/core/src:/home/dylan/projects/disclaude-wt-blockresume/packages/retrieval/src:/home/dylan/projects/disclaude-wt-blockresume/packages/tools/src /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/agent-server/tests/test_blocked_answer_resume.py -q` passed.
- `PYTHONPATH=/home/dylan/projects/disclaude-wt-blockresume/packages/agent-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/app-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/core/src:/home/dylan/projects/disclaude-wt-blockresume/packages/retrieval/src:/home/dylan/projects/disclaude-wt-blockresume/packages/tools/src /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests/test_bug12_followup_replan.py packages/core/tests/test_replan_pending_valve.py packages/core/tests/test_mode_desync.py packages/agent-server/tests/test_blocked_answer_resume.py -q` passed.
- `PYTHONPATH=/home/dylan/projects/disclaude-wt-blockresume/packages/agent-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/app-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/core/src:/home/dylan/projects/disclaude-wt-blockresume/packages/retrieval/src:/home/dylan/projects/disclaude-wt-blockresume/packages/tools/src /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/agent-server/tests/test_build_kernel_interface.py packages/agent-server/tests/test_build_kernel_pinning.py -q` passed.
- `PYTHONPATH=/home/dylan/projects/disclaude-wt-blockresume/packages/agent-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/app-server/src:/home/dylan/projects/disclaude-wt-blockresume/packages/core/src:/home/dylan/projects/disclaude-wt-blockresume/packages/retrieval/src:/home/dylan/projects/disclaude-wt-blockresume/packages/tools/src /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q` passed, with existing deprecation/runtime warnings.
