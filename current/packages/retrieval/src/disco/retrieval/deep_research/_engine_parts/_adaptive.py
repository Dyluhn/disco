"""Adaptive probe-batch execution for :mod:`deep_research.engine`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...models import Passage as RetrievalPassage
from ..decompose import SubQuestion
from ..gather import SubQuestionResult
from ._drain import _dominant_bound

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..engine import DeepResearchRun, EmitFn, PopInjectedSourcesFn, PopSteersFn


def _assessment_actions(assessment: Any) -> list[dict[str, Any]]:
    return _actions_payload(assessment.actions)


def _actions_payload(actions: Any) -> list[dict[str, Any]]:
    return [
        {
            "kind": action.kind,
            "probe": action.probe.query if action.probe else None,
            "target": action.target_id,
            "reason": action.reason,
        }
        for action in actions
    ]


async def gather_adaptive_batches(
    run: DeepResearchRun,
    pending: list[SubQuestion],
    sections: list[Any],
    carried_passages: list[RetrievalPassage],
    *,
    started: float,
    bounded_by: str | None,
    should_cancel: Callable[[], bool] | None,
    emit: EmitFn,
    pop_steers: PopSteersFn,
    pop_injected_sources: PopInjectedSourcesFn,
) -> tuple[list[SubQuestionResult], str | None, list[RetrievalPassage]]:
    """Gather small batches and reassess the global evidence after each."""
    results: list[SubQuestionResult] = []
    carried = list(carried_passages)
    while pending:
        tasks = run._start_gather_tasks(pending, emit=emit)
        batch_results, bounded_by, injected = await run._drain_and_synthesize(
            tasks,
            started=started,
            bounded_by=bounded_by,
            should_cancel=should_cancel,
            emit=emit,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )
        results.extend(batch_results)
        carried.extend(injected)
        controller = run._controller
        if controller is None:
            break
        assessment = controller.assess(batch_results)
        capacity_actions = (
            ()
            if bounded_by in {"stopped", "wall_clock"}
            else controller.ensure_capacity(batch_results)
        )
        await emit(
            "phase",
            {
                "phase": "evidence_assessment",
                "completed": assessment.completed,
                "evidence": assessment.evidence,
                "capacity": controller.capacity().__dict__,
                "actions": _assessment_actions(assessment),
                "capacity_actions": _actions_payload(capacity_actions),
            },
        )
        if bounded_by in {"stopped", "wall_clock"}:
            break
        if run._source_budget.remaining <= 0:
            bounded_by = _dominant_bound(bounded_by, "sources")
            break
        pending = [probe.to_subquestion() for probe in controller.next_batch()]
        run._scheduled_subquestions = len(sections) + controller.scheduled
        if not pending and controller.active > 0:
            bounded_by = _dominant_bound(bounded_by, "subquestions")
        elif not pending and not controller.capacity().sufficient:
            # Capacity is the completion predicate. If the queue is empty
            # before it is met, the only honest terminal explanation is the
            # hard sub-question cap (or a prior source/wall-clock bound).
            bounded_by = _dominant_bound(bounded_by, "subquestions")
    return results, bounded_by, carried


__all__ = ["gather_adaptive_batches"]
