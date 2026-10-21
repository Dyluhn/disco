"""Validate progress against the host's current approved plan at execution time."""

from .._view_plan import _latest_plan, _progress_index, _valid_progress_snapshot
from ..events import ActionEvent, Event, PlanEvent, StatusEvent


def _approved_progress_plan(events: list[Event]) -> PlanEvent:
    plan = _latest_plan(events)
    if plan is None:
        raise ValueError("Plan progress requires a current approved plan; no plan is available.")
    approval = next(
        (
            event
            for event in events[events.index(plan) + 1 :]
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        None,
    )
    transition = approval.plan_verification_transition if approval is not None else None
    if approval is None or (
        transition is not None
        and (
            transition.new_plan_event_id != plan.id or transition.new_plan_revision != plan.revision
        )
    ):
        raise ValueError(
            f"Plan revision {plan.revision} is not approved; progress was not applied."
        )
    return plan


def _validate_plan_progress(action: ActionEvent, events: list[Event]) -> None:
    call = action.tool_call
    if call is None or call.tool_name not in {"plan_step", "update_plan_progress"}:
        return
    if call.tool_name == "update_plan_progress":
        rows = call.arguments.get("steps")
        if not _valid_progress_snapshot(rows):
            return  # Preserve the executor's actionable schema-validation error.
        indices = [_progress_index(row["index"]) for row in rows]
    else:
        index = _progress_index(call.arguments.get("index"))
        if index is None:
            return
        indices = [index]
    plan = _approved_progress_plan(events)
    if any(index is None or not 1 <= index <= len(plan.steps) for index in indices):
        raise ValueError(
            f"Plan progress was not applied: current approved plan revision {plan.revision} "
            f"has {len(plan.steps)} steps (indices 1 through {len(plan.steps)}). "
            "Correct the step indices against this plan and resend the update; "
            "none of this update's rows were applied."
        )
