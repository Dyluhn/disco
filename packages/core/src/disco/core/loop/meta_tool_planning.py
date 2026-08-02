"""Internal Loop turn-control collaborator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..events import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from . import signals
from .boundaries import AgentStep
from .control import Disp
from .plan_revisions import (
    normalized_execution_contract,
    preflight_plan_revision,
    reject_invalid_revision_conditions,
)
from .turn_control_support import (
    _IDENTICAL_PLAN_NUDGE_DIAGNOSTIC,
    _IDENTICAL_PLAN_NUDGE_TEXT,
    _PROPOSE_PLAN_UPDATE_REPEAT_CAP,
    _prior_plan_and_productive_action_between,
)

if TYPE_CHECKING:
    from .meta_tool_common import MetaToolLoopFacet

_LOG = logging.getLogger("disco.loop")


class MetaToolPlanningMixin:
    _loop: MetaToolLoopFacet

    @staticmethod
    def _invalid_empty_revision_count(events: list[Event]) -> int:
        latest_approval_seq = max(
            (
                event.seq or 0
                for event in events
                if isinstance(event, StatusEvent) and event.detail == "plan_approved"
            ),
            default=0,
        )
        return sum(
            1
            for event in events
            if isinstance(event, StatusEvent)
            and event.detail == "invalid_plan_no_steps"
            and (event.seq or 0) > latest_approval_seq
        )

    async def _recover_empty_revision(
        self,
        new_plan: PlanEvent,
        events: list[Event],
    ) -> PlanEvent | Disp:
        if new_plan.steps:
            return new_plan
        self._loop._planner.discard_plan_predicates(new_plan.revision)
        prior = self._invalid_empty_revision_count(events)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="invalid_plan_no_steps",
            )
        )
        if prior == 0:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "The proposed plan update was NOT accepted because "
                            "`steps` was empty. Submit it again with at least one "
                            "concrete step object. A single broad step is valid; "
                            "preserve the current product and do not invent extra scope.\n"
                            "</system-reminder>"
                        ),
                    ),
                    meta={"blocking": "invalid_plan_no_steps"},
                )
            )
            return Disp.CONTINUE
        fallback = (
            new_plan.summary
            if new_plan.summary != "Proposed plan"
            else signals.current_revision_instruction(events)
            or signals.latest_user_text(events)
            or ""
        ).strip()
        if not fallback:
            await self._loop._land_blocked(
                reason="revision_plan_no_concrete_steps",
                guidance=(
                    "The planner repeatedly submitted an empty plan update and no "
                    "bounded user/model instruction was available for recovery. The "
                    "empty revision was not approved."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="revision_plan_no_concrete_steps",
            )
            return Disp.HALT
        recovered = PlanEvent(
            summary=new_plan.summary,
            steps=[PlanStep(title=fallback[:200])],
            revision=new_plan.revision,
            context=new_plan.context,
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="plan_steps_recovered_from_instruction",
            )
        )
        return recovered

    def _record_identical_revision(
        self,
        new_plan: PlanEvent,
        events: list[Event],
    ) -> None:
        prior_plan, productive_between = _prior_plan_and_productive_action_between(events, new_plan)
        if prior_plan is not None and normalized_execution_contract(
            prior_plan
        ) == normalized_execution_contract(new_plan):
            if productive_between:
                self._loop._identical_plan_revisions = 1
            else:
                self._loop._identical_plan_revisions += 1
            return
        self._loop._identical_plan_revisions = 0

    async def _auto_approve_revision(self, new_plan: PlanEvent) -> Disp:
        events = await self._loop._events()
        self._record_identical_revision(new_plan, events)
        if self._loop._identical_plan_revisions >= _PROPOSE_PLAN_UPDATE_REPEAT_CAP:
            await self._loop._valve.land_blocked(
                reason="bookkeeping_only",
                guidance=(
                    "Autonomous mode proposed the same plan revision repeatedly "
                    "without doing real work."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="bookkeeping_only",
            )
            return Disp.HALT
        if self._loop._identical_plan_revisions == _PROPOSE_PLAN_UPDATE_REPEAT_CAP - 1:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_IDENTICAL_PLAN_NUDGE_TEXT),
                    meta={"diagnostic": _IDENTICAL_PLAN_NUDGE_DIAGNOSTIC},
                )
            )
        await self._loop._emit(await self._loop._plan_approval_status(new_plan, events))
        self._loop.mode = self._loop._execution_mode
        next_step = new_plan.steps[0].title if new_plan.steps else "the first step"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"Plan revision {new_plan.revision} is APPROVED "
                        "(auto-approved — no human gate in autonomous mode). Do "
                        "NOT propose it again. Continue executing now — next "
                        f"step: '{next_step}'. Your next output must be a real "
                        "tool call (file/shell/etc.), not another plan proposal."
                        "\n</system-reminder>"
                    ),
                ),
                meta={"diagnostic": "auto_approval_ack"},
            )
        )
        await self._loop._seed_context_from_plan()
        return Disp.CONTINUE

    async def handle_propose_plan_update(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None
        new_plan = self._loop._plan_from_args(step.tool_call.arguments, events)
        from .plans import validate_plan_conditions

        condition_errors = validate_plan_conditions(
            step.tool_call.arguments,
            new_plan,
            self._loop._strict_appkit_active_reader,
        )
        if condition_errors:
            return await reject_invalid_revision_conditions(
                self._loop,
                new_plan,
                condition_errors,
            )
        recovered = await self._recover_empty_revision(new_plan, events)
        if isinstance(recovered, Disp):
            return recovered
        new_plan = recovered
        revision_disposition = await preflight_plan_revision(self._loop, new_plan, events)
        if revision_disposition is not None:
            return revision_disposition
        emitted_plan = await self._loop._emit(new_plan)
        if isinstance(emitted_plan, PlanEvent):
            new_plan = emitted_plan
        if self._loop._autonomous:
            return await self._auto_approve_revision(new_plan)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                detail=new_plan.id,
            )
        )
        return Disp.HALT
