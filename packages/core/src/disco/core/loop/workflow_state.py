"""Workflow router state transitions that must be mirrored in the event log."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import ConversationStatus, PlanEvent, PlanStep, StatusEvent
from ..llm import OperatingMode
from ..workflow import WorkflowRun
from . import signals

if TYPE_CHECKING:
    from .engine import AgentLoop


def _workflow_plan_title(workflow_run: WorkflowRun) -> str:
    return (
        f"Run workflow: {workflow_run.definition.name} — "
        f"{workflow_run.definition.output_contract.path_template}"
    )


async def seed_approved_workflow_plan(
    loop: AgentLoop,
    workflow_run: WorkflowRun,
) -> None:
    """Append the same plan+approval facts an already-approved workflow represents."""
    events = await loop._events()
    plan = PlanEvent(
        summary=f"Run workflow: {workflow_run.definition.name}",
        steps=[PlanStep(title=_workflow_plan_title(workflow_run))],
        revision=signals.next_plan_revision(events),
        context=(
            "Synthesized from an enabled, approved workflow card after "
            "enter_workflow validated params and compiled the sealed tool scope."
        ),
    )
    await loop._emit(plan)
    await loop._emit(await loop._plan_approval_status(plan, await loop._events()))
    loop._workflow_run = workflow_run
    loop.mode = loop._execution_mode
    await loop._seed_context_from_plan()


async def abort_workflow_to_router(loop: AgentLoop) -> None:
    """Clear the sealed workflow run and return the loop to router planning mode."""
    loop._workflow_run = None
    loop.mode = OperatingMode.PLANNING
    await loop._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
