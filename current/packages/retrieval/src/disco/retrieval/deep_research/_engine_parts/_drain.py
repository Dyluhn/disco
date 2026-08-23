"""RP-04 drain consumer, extracted from `DeepResearchRun._drain_gathered`.

Owns the research-phase control flow: the should-cancel/wall-clock stop
check, the D3 steer/inject-source mid-run checkpoints, and awaiting +
error-handling one gather leg's task. Report writing happens ONLY after the
whole research phase completes (`_engine_parts._compiler`); nothing here
produces report prose.

Every helper that needs the live `DeepResearchRun` takes it as an explicit
`run` parameter (never a method) — see `_engine_parts/__init__.py`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ...models import Passage as RetrievalPassage
from ..decompose import SubQuestion
from ..gather import GatherLegContext, SubQuestionResult

if TYPE_CHECKING:
    from ..engine import DeepResearchRun, EmitFn, PopInjectedSourcesFn, PopSteersFn

GatherTask = tuple[SubQuestion, "asyncio.Task[SubQuestionResult]", str, str, GatherLegContext]

_BOUND_PRIORITY = {
    None: 0,
    "rounds": 1,
    "subquestions": 2,
    "sources": 3,
    "wall_clock": 4,
    "stopped": 5,
}


class _WallClockExpired(Exception):
    """An in-flight gather operation crossed the research-phase deadline."""


def seconds_left(run: DeepResearchRun, started: float) -> float:
    """Research-phase seconds remaining under the tier wall clock.

    The tier wall clock bounds RESEARCH. A tier-proportional reserve is held
    back for report writing (outline, sections, verification, summary) so
    gathering cannot starve the writing phase; the writing phase itself runs
    under its calls' own bounded retries, never a shrinking global slice.
    """
    minimum_words = run._bound.report_min_words
    if minimum_words >= 8_000:
        estimated_sections = 12
    elif minimum_words >= 4_000:
        estimated_sections = 9
    elif minimum_words >= 1_500:
        estimated_sections = 5
    else:
        estimated_sections = 1
    estimated_sections = min(run._bound.max_subquestions, estimated_sections)
    reserve = max(60.0, 35.0 * estimated_sections + 45.0)
    reserve = min(reserve, run._bound.max_wall_clock_s * 0.4)
    return max(0.0, run._bound.max_wall_clock_s - reserve - (time.monotonic() - started))


def _seconds_left(run: DeepResearchRun, started: float) -> float:
    return seconds_left(run, started)


def _dominant_bound(current: str | None, candidate: str | None) -> str | None:
    """Keep the reason that imposed the strongest terminal bound."""
    if _BOUND_PRIORITY.get(candidate, 0) > _BOUND_PRIORITY.get(current, 0):
        return candidate
    return current


async def _cancel_and_drain(gather_tasks: list[GatherTask]) -> None:
    """Cancel unfinished gather legs and wait until their cleanup has settled."""
    pending = [task for _, task, _, _, _ in gather_tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _check_stop_condition(
    run: DeepResearchRun,
    gather_tasks: list[GatherTask],
    started: float,
    bounded_by: str | None,
    should_cancel: Callable[[], bool] | None,
) -> tuple[bool, str | None]:
    """Return `(stop, bounded_by)`. Signals stop when Stop was requested or
    when the wall-clock bound tripped AND legs are still running. The caller
    drains cancellation before returning. When the wall
    clock tripped but every leg already finished, `bounded_by` is still set
    to "wall_clock" (so the report is honest about it) but the loop is NOT
    stopped — matches the original inline check exactly."""
    if should_cancel is not None and should_cancel():
        return True, "stopped"
    if _seconds_left(run, started) > 0:
        return False, bounded_by
    bounded_by = _dominant_bound(bounded_by, "wall_clock")
    still_running = [t for _, t, _, _, _ in gather_tasks if not t.done()]
    if not still_running:
        return False, bounded_by
    return True, bounded_by


async def _drain_steer_hooks(
    run: DeepResearchRun,
    gather_tasks: list[GatherTask],
    pop_steers: PopSteersFn,
    emit: EmitFn,
) -> str | None:
    """D3 steer checkpoint: pop any pending steer strings and start a fresh
    gather leg for each, appending it to `gather_tasks` in place so the
    drain loop's index-based while-loop picks it up naturally. No-op
    (OFF-path) when `pop_steers` is None."""
    if pop_steers is None:
        return None
    bounded_by: str | None = None
    for steer_text in pop_steers():
        controller = run._controller
        scheduled = controller.scheduled if controller is not None else run._scheduled_subquestions
        if scheduled >= run._bound.max_subquestions:
            bounded_by = _dominant_bound(bounded_by, "subquestions")
            await emit(
                "observation",
                {
                    "subquestion": steer_text,
                    "ok": False,
                    "detail": "mid-run steer not started: sub-question cap reached",
                },
            )
            continue
        if run._source_budget.remaining <= 0:
            bounded_by = _dominant_bound(bounded_by, "sources")
            await emit(
                "observation",
                {
                    "subquestion": steer_text,
                    "ok": False,
                    "detail": "mid-run steer not started: source cap reached",
                },
            )
            continue
        if controller is not None:
            action = controller.queue_probe(steer_text, origin="follow_up", priority=True)
            if action is None:
                await emit(
                    "observation",
                    {
                        "subquestion": steer_text,
                        "ok": False,
                        "detail": "mid-run steer already represented by an active probe",
                    },
                )
                continue
            await emit(
                "observation",
                {
                    "subquestion": steer_text,
                    "ok": True,
                    "detail": f"mid-run steer: adding research probe '{steer_text}'",
                },
            )
            continue
        steer_budget = min(
            run._source_budget.remaining,
            max(1, run._bound.max_sources // max(1, len(gather_tasks))),
        )
        new_subq = SubQuestion(title=steer_text)
        await emit(
            "observation",
            {
                "subquestion": steer_text,
                "ok": True,
                "detail": (f"mid-run steer: adding research section '{steer_text}'"),
            },
        )
        gather_tasks.append(run._start_one_steer_task(new_subq, steer_budget, emit=emit))
        run._scheduled_subquestions += 1
    return bounded_by


async def _drain_injected_sources(
    run: DeepResearchRun,
    pop_injected_sources: PopInjectedSourcesFn,
    subq_namespace: str,
) -> list[RetrievalPassage]:
    """D3 inject-source checkpoint: pop any pending injected passages and,
    when an embedder is available, upsert them into the current leg's
    namespace so `_retrieve_for_section` finds them via cosine similarity.
    Returns the newly-injected passages (empty when `pop_injected_sources`
    is None or nothing was pending) for the caller to accumulate. OFF-path:
    no-op."""
    if pop_injected_sources is None:
        return []
    new_injected = pop_injected_sources()
    if not new_injected:
        return []
    if run._embedder is not None:
        try:
            vecs = await run._embedder.embed([p.text for p in new_injected])
            await run._vector_store.upsert(subq_namespace, new_injected, vecs)
        except Exception:  # noqa: BLE001
            pass  # fallback_passages path covers it
    return new_injected


async def _await_leg_result(
    task: asyncio.Task[SubQuestionResult],
    gather_tasks: list[GatherTask],
    timeout_s: float,
) -> SubQuestionResult | None:
    """Await one leg's task. Returns `None` on a clean Stop-triggered
    cancellation (the caller breaks the drain loop); on any OTHER exception,
    cancels every sibling leg before re-raising so a gather failure never
    leaks still-running tasks into the long-lived server loop."""
    try:
        return await asyncio.wait_for(task, timeout=timeout_s)
    except TimeoutError as exc:
        await _cancel_and_drain(gather_tasks)
        raise _WallClockExpired from exc
    except asyncio.CancelledError:
        return None
    except Exception:
        await _cancel_and_drain(gather_tasks)
        raise


def _merge_leg_result(
    sub_result: SubQuestionResult,
    results: list[SubQuestionResult],
    injected_passages: list[RetrievalPassage],
    bounded_by: str | None,
) -> str | None:
    """Fold any D3-injected passages into this leg's result, append it to the
    run's accumulated `results`, and propagate a `bounded_by_rounds` leg into
    the run-level `bounded_by`. Returns the (possibly updated) `bounded_by`."""
    if injected_passages:
        sub_result.passages.extend(injected_passages)
    results.append(sub_result)
    if sub_result.bounded_by_rounds:
        bounded_by = _dominant_bound(bounded_by, "rounds")
    if sub_result.bounded_by_sources:
        bounded_by = _dominant_bound(bounded_by, "sources")
    return bounded_by


async def drain_and_synthesize(
    run: DeepResearchRun,
    gather_tasks: list[GatherTask],
    *,
    started: float,
    bounded_by: str | None,
    should_cancel: Callable[[], bool] | None,
    emit: EmitFn,
    pop_steers: PopSteersFn = None,
    pop_injected_sources: PopInjectedSourcesFn = None,
) -> tuple[list[SubQuestionResult], str | None, list[RetrievalPassage]]:
    """RP-04 consumer: drain the gather tasks in order. Honors Stop
    (`should_cancel`) and the research wall-clock bound at every sub-question
    boundary, cancelling the still-running legs when either trips. Returns
    `(results, bounded_by, injected_passages)`. Report writing happens only
    after the whole research phase completes — never here.

    D3 — mid-run steer / inject hooks (both default None → OFF-path is
    byte-identical to a run without hooks):
    - `pop_steers`: drained at each boundary via `_drain_steer_hooks`; each
      returned string becomes a new gather leg appended to `gather_tasks`
      (the index-based while loop naturally picks them up).
    - `pop_injected_sources`: drained at each boundary via
      `_drain_injected_sources`; accumulated passages are folded into every
      subsequent section's candidate set. Returned alongside `results` so
      `run()` can add them to `carried_passages` for the final report
      assembly."""
    results: list[SubQuestionResult] = []
    injected_passages: list[RetrievalPassage] = []  # accumulated inject-source passages

    i = 0
    while i < len(gather_tasks):
        _subq, task, _subq_id, subq_namespace, _leg_context = gather_tasks[i]
        i += 1

        stop, bounded_by = _check_stop_condition(
            run, gather_tasks, started, bounded_by, should_cancel
        )
        if stop:
            await _cancel_and_drain(gather_tasks)
            break

        steer_bound = await _drain_steer_hooks(run, gather_tasks, pop_steers, emit)
        bounded_by = _dominant_bound(bounded_by, steer_bound)

        try:
            new_injected = await asyncio.wait_for(
                _drain_injected_sources(run, pop_injected_sources, subq_namespace),
                timeout=_seconds_left(run, started),
            )
        except TimeoutError:
            await _cancel_and_drain(gather_tasks)
            bounded_by = _dominant_bound(bounded_by, "wall_clock")
            break
        injected_passages.extend(new_injected)

        try:
            sub_result = await _await_leg_result(task, gather_tasks, _seconds_left(run, started))
        except _WallClockExpired:
            bounded_by = _dominant_bound(bounded_by, "wall_clock")
            break
        if sub_result is None:
            break

        bounded_by = _merge_leg_result(sub_result, results, injected_passages, bounded_by)
    return results, bounded_by, injected_passages
