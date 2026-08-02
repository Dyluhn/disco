"""Recovery paths for models that refuse to submit revision plans."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..events import ConversationStatus, StatusEvent
from . import signals
from .control import Disp

if TYPE_CHECKING:
    from .ports import GateCounterPort, LoopEventPort, PlanLifecyclePort

    class _LoopFacet(GateCounterPort, LoopEventPort, PlanLifecyclePort, Protocol):
        """The loop capability this module uses: gate counters, the event log, the plan lifecycle.
        """


async def harvest_revision_plan_after_refusal(loop: _LoopFacet) -> Disp | None:
    """Route a synthesized one-step revision plan through the normal approval gate."""
    synth = signals.harvested_revision_plan_from_user(await loop._events())
    if synth is None:
        return None
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="harvested_revision_plan",
        )
    )
    await loop._emit(synth)
    loop._plan_explore_reads = 0
    loop._plan_nudges = 0
    return await loop._route_plan_approval_gate(synth)
