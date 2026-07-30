"""Internal AgentLoop collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine_contracts import (
    _FORCE_SUBMIT_DIRECTIVE,
    _INVALID_PLAN_DONE_CONDITION_CAP,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
    ToolCall,
    preflight_plan_revision,
    signals,
    validate_plan_conditions,
)

if TYPE_CHECKING:
    from .loop_facade_compat import _AgentLoopCompatibility as AgentLoop


class PlanSubmissionController:
    """Validate and route one structured planning-phase submission."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @staticmethod
    def _attempt_number(events: list[Event], detail: str) -> int:
        start_seq = signals.current_planning_segment_start_seq(events)
        return 1 + sum(
            1
            for event in events
            if isinstance(event, StatusEvent)
            and event.detail == detail
            and (start_seq is None or (event.seq or 0) > start_seq)
        )

    async def _reject_invalid_conditions(
        self,
        plan: PlanEvent,
        tool_call: ToolCall,
        events: list[Event],
    ) -> Disp | None:
        condition_errors = validate_plan_conditions(
            tool_call.arguments,
            plan,
            self._loop._strict_appkit_active_reader,
        )
        if not condition_errors:
            return None
        self._loop._planner.discard_plan_predicates(plan.revision)
        attempt = self._attempt_number(events, "invalid_plan_done_conditions")
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="invalid_plan_done_conditions",
            )
        )
        rendered = "\n".join(f"- {error}" for error in condition_errors)
        if attempt >= _INVALID_PLAN_DONE_CONDITION_CAP:
            await self._loop._land_blocked(
                reason="invalid_plan_done_conditions",
                guidance=(
                    "The planner repeatedly submitted unsafe Definition-of-Done "
                    f"conditions ({attempt}/{_INVALID_PLAN_DONE_CONDITION_CAP}). "
                    "No plan was approved and no execution began. Correct these "
                    f"conditions before retrying:\n{rendered}"
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="invalid_plan_done_conditions",
            )
            return Disp.HALT
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Your proposed plan was NOT accepted because its done_condition "
                        "values would become approved plan verifiers and are "
                        f"unsafe or internally inconsistent:\n{rendered}\n\n"
                        "Submit a corrected plan. Do not replace a required directory "
                        "with a file and do not guess a local preview port or a future/"
                        "placeholder host. Use http_ok only with a concrete already-known "
                        "FQDN/IP. Omit a condition when there is no safe, exact machine-"
                        f"checkable predicate. Invalid plan attempt {attempt}/"
                        f"{_INVALID_PLAN_DONE_CONDITION_CAP}.\n</system-reminder>"
                    ),
                ),
                meta={"blocking": "invalid_plan_done_conditions"},
            )
        )
        return Disp.CONTINUE

    async def _recover_empty_initial_plan(
        self, plan: PlanEvent, events: list[Event]
    ) -> Disp | None:
        """Bound malformed summary-only initial plans without inventing scope."""
        if plan.steps or signals.revision_planning_has_prior_approval(events):
            return None
        self._loop._planner.discard_plan_predicates(plan.revision)
        attempt = self._attempt_number(events, "invalid_plan_no_steps")
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="invalid_plan_no_steps",
            )
        )
        if attempt == 1:
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="force_submit_plan",
                )
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "Your proposed plan was NOT accepted because `steps` was "
                            "empty. Submit it again with at least one concrete step object, "
                            'for example: {"summary":"Build the requested site",'
                            '"steps":[{"title":"Create and verify the requested site"}]}. '
                            "A single broad step is valid; do not invent extra scope.\n"
                            "</system-reminder>"
                        ),
                    ),
                    meta={"blocking": "invalid_plan_no_steps"},
                )
            )
            self._loop._plan_nudges = 0
            return Disp.CONTINUE

        instruction = signals.latest_user_text(events)
        if not instruction:
            await self._loop._land_blocked(
                reason="initial_plan_no_concrete_steps",
                guidance=(
                    "The planner repeatedly submitted an empty initial plan and no "
                    "user-owned instruction was available for bounded recovery. No plan "
                    "was approved and no execution began."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="initial_plan_no_concrete_steps",
            )
            return Disp.HALT
        recovered = PlanEvent(
            summary=(instruction[:120] if plan.summary == "Proposed plan" else plan.summary),
            steps=[PlanStep(title=instruction[:200])],
            revision=plan.revision,
            context=(f"{plan.context}\n\n" if plan.context else "")
            + (
                "Host-recovered one capstone from the exact user instruction after "
                "two empty submissions."
            ),
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="plan_steps_recovered_from_user_instruction",
            )
        )
        await self._loop._emit(recovered)
        self._loop._plan_nudges = 0
        return await self._loop._route_plan_approval_gate(recovered)

    async def _recover_empty_revision_plan(
        self, plan: PlanEvent, events: list[Event]
    ) -> Disp:
        if not signals.revision_force_submit(events):
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="force_submit_plan",
                )
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_FORCE_SUBMIT_DIRECTIVE),
                )
            )
            self._loop._plan_nudges = 0
            return Disp.CONTINUE
        instruction = signals.current_revision_instruction(events)
        if instruction:
            plan = PlanEvent(
                summary=plan.summary or instruction[:120],
                steps=[PlanStep(title=instruction[:200])],
                revision=plan.revision + 1,
                context=plan.context,
            )
            await self._loop._emit(plan)
        else:
            await self._loop._land_blocked(
                reason="revision_no_concrete_steps",
                guidance=(
                    "The planner submitted an empty revision plan after the force-submit "
                    "recovery, and there was no current revision instruction to "
                    "synthesize a concrete step from."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="revision_no_concrete_steps",
            )
            return Disp.HALT
        return await self._loop._route_plan_approval_gate(plan)

    async def handle(self, tool_call: ToolCall, events: list[Event]) -> Disp:
        self._loop._plan_explore_reads = 0
        plan = self._loop._plan_from_args(tool_call.arguments, events)
        invalid = await self._reject_invalid_conditions(plan, tool_call, events)
        if invalid is not None:
            return invalid
        empty_initial = await self._recover_empty_initial_plan(plan, events)
        if empty_initial is not None:
            return empty_initial
        revision_disposition = await preflight_plan_revision(self._loop, plan, events)
        if revision_disposition is not None:
            return revision_disposition
        await self._loop._emit(plan)
        if (
            not plan.steps
            and self._loop._revision_force_submit_enabled
            and signals.in_planning_for_revision(events)
        ):
            return await self._recover_empty_revision_plan(plan, events)
        return await self._loop._route_plan_approval_gate(plan)


async def _handle_submitted_plan(
    loop: AgentLoop, tool_call: ToolCall, events: list[Event]
) -> Disp:
    """Compatibility entry point for the historical engine helper."""
    return await PlanSubmissionController(loop).handle(tool_call, events)
