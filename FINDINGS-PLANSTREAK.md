# Plan-Streak Findings

## Reproduction

- Added loop-level scripted fake-provider coverage in `packages/core/tests/test_identical_plan_streak.py`.
- Initial targeted run reproduced both defects:
  - workless repeated identical revisions emitted no `identical_plan_nudge`;
  - an identical revision after a `file_edit` still landed `STUCK(bookkeeping_only)`.

## Implementation

- `propose_plan_update` now refreshes the persisted event log after emitting the new `PlanEvent`, finds the immediately prior `PlanEvent`, and checks the bounded window between those two plan events for non-bookkeeping `ActionEvent`s.
- Identical step titles increment the streak only when that bounded window has no productive action.
- If real work appears between the two plan events, the identical proposal starts a new streak at `1`.
- A persisted environment `MessageEvent` with `meta={"diagnostic": "identical_plan_nudge"}` is emitted when the streak reaches `cap - 1`.
- The cap halt still uses the existing `bookkeeping_only` blocked lander.

## Deviations / Code Reality

- The existing fresh-session gate refuses `propose_plan_update` before any real action in the session. The scripted-provider reproduction therefore performs one real `file_edit` after initial plan approval before exercising a workless repeated-revision streak.
- The spec names `think` as bookkeeping, but the central `_BOOKKEEPING_TOOLS` set did not include it. I added `think` to that shared set and mirrored the stuck-detector sentinel so the taxonomy remains centralized.
- A reset-after-work case can already have the earlier `cap - 1` warning persisted before the real work happens. The post-work identical proposal restarts at `1` and does not emit a second warning or halt.

## Verification

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q`
- Result: passed, with existing third-party/runtime warnings only.
