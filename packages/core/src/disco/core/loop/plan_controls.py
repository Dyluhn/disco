"""Internal _LoopFacet collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..events import AlternativeOption
from .engine_contracts import (
    _CONTINUE_OPTION_ID,
    _LOG,
    ActionEvent,
    AlternativesEvent,
    ConversationState,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanRevisionWeakeningError,
    PlanVerificationTransition,
    StatusEvent,
    ToolCall,
    _design_direction_brief_text,
    _optional_async_fence,
    assert_plan_revision_approvable,
    cast,
    event_matches_current_workspace_view,
    predicate_fingerprints,
    reject_plan_weakening,
    signals,
)

if TYPE_CHECKING:
    from .ports import (
        ConversationModePort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
    )

    class _LoopFacet(

        ConversationModePort,

        GateCounterPort,

        LoopEventPort,

        PlanLifecyclePort,

        ToolExecutionPort,

        TurnControlPort,

        Protocol,

    ):
        """The loop capability this module uses: conversation mode, gate counters, the event log,
        the plan lifecycle, tool execution, turn control.
        """


class PlanApprovalController:
    def __init__(self, loop: _LoopFacet) -> None:
        self._loop = loop

    async def _plan_approval_status(
        self,
        plan: PlanEvent,
        events: list[Event],
    ) -> StatusEvent:
        """Build the single append-only event that approves and audits a plan.

        No mutable plan-verifier row exists: the latest such event selects the
        current PlanEvent after a restart. External DoD bytes are read only so a
        model approval can neither remove nor shadow them.
        """
        assert_plan_revision_approvable(events, plan)
        old_plan = signals.latest_approved_plan(events)
        external = await self._loop.store.get_external_dod_spec(self._loop.conversation_id)
        reason = (
            signals.APPROVED_PLAN_VERIFIER_REPAIR
            if signals.plan_is_verifier_repair(events, plan)
            else "approved_plan_revision"
            if old_plan is not None
            else "approved_initial_plan"
        )
        transition = PlanVerificationTransition(
            old_plan_revision=old_plan.revision if old_plan is not None else None,
            old_plan_event_id=old_plan.id if old_plan is not None else None,
            old_predicate_fingerprints=predicate_fingerprints(
                self._loop._plan_verification_predicates(old_plan)
            ),
            new_plan_revision=plan.revision,
            new_plan_event_id=plan.id,
            new_predicate_fingerprints=predicate_fingerprints(
                self._loop._plan_verification_predicates(plan)
            ),
            external_predicate_fingerprints=predicate_fingerprints(
                external.predicates if external is not None else []
            ),
            reason=reason,
        )
        return StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_approved",
            plan_verification_transition=transition,
        )

    async def approve_plan(self) -> ConversationState:
        """Approve the pending plan: flip into execution mode (full tools restored)
        and resume to RUNNING. The caller then re-runs the loop. The per-action
        risk gate still governs the build that follows (defense in depth)."""
        async with self._loop._lock, _optional_async_fence(self._loop._control_fence):
            state = await self._loop.get_state()
            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
                return state
            events = await self._loop._events()
            plan = next(
                (
                    event
                    for event in reversed(events)
                    if isinstance(event, PlanEvent) and event.id == state.pending_plan_id
                ),
                None,
            )
            if plan is None:
                _LOG.error(
                    "Plan approval for %s had no persisted pending PlanEvent; "
                    "preserving prior approval.",
                    self._loop.conversation_id,
                )
                return state
            if not event_matches_current_workspace_view(events, plan):
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="stale_plan_approval")
                )
                return await self._loop.get_state()
            try:
                approval = await self._loop._plan_approval_status(plan, events)
            except PlanRevisionWeakeningError as exc:
                await reject_plan_weakening(self._loop, plan, exc.diff)
                return await self._loop.get_state()
            await self._loop._emit(
                approval.model_copy(update={"agent_view_id": plan.agent_view_id})
            )
            self._loop.mode = self._loop._execution_mode
            await self._loop._seed_context_from_plan()
        return await self._loop.get_state()

    async def _seed_context_from_plan(self) -> None:
        """CXT-6/CXT-7 — seed durable context from the approved plan: current_goal.md
        (= plan.summary) and todo.md (the step checklist), the live execution memory.
        Best-effort: a seeding error never blocks approval. A revised-plan re-approval
        re-seeds (so the durable context tracks the current contract); minor in-build
        updates are the agent's via the context_memory tool."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return
        try:
            from ..context import ArtifactMemoryStore
            from ..design import (
                direction_from_markdown,
                direction_tokens_css,
                pick_direction,
                render_design_direction,
            )
            from ..view import _latest_plan
            from .context_builder import render_plan_as_todo_markdown

            events = await self._loop._events()
            plan = _latest_plan(events)
            if plan is None or not getattr(plan, "steps", None):
                return
            store = ArtifactMemoryStore(sbx)
            if plan.summary:
                await store.write_goal(plan.summary)
            await store.seed_todo(render_plan_as_todo_markdown(plan))
            committed_markdown = await store.read_design_direction()
            if committed_markdown is not None:
                # The direction is a project commitment, not a fresh classifier
                # result for every revised plan.  Re-picking from free-form plan
                # prose made unrelated follow-ups silently restyle the project and
                # could even flip the reference back on the next approval.  Keep a
                # valid commitment byte-identical and repair only its mechanical
                # companion from the committed ID.
                committed = direction_from_markdown(committed_markdown)
                if committed is None:
                    _LOG.warning(
                        "CXT-7 committed design direction is unparseable for %s; "
                        "preserving it instead of silently selecting a new style",
                        self._loop.conversation_id,
                    )
                    return
                expected_tokens = direction_tokens_css(committed)
                if await store.read_design_direction_tokens() != expected_tokens:
                    await store.write_design_direction_tokens(expected_tokens)
                return
            brief = _design_direction_brief_text(plan, events)
            if brief:
                direction = pick_direction(brief, self._loop.conversation_id)
                # Write the companion first and the parsed markdown commitment
                # last.  If a backend dies between the writes, the next approval
                # has no valid commitment marker and deterministically repairs
                # both instead of accepting a direction without matching tokens.
                await store.write_design_direction_tokens(direction_tokens_css(direction))
                await store.write_design_direction(render_design_direction(direction))
        except Exception:
            _LOG.warning(
                "CXT-7 context seed from plan failed for %s",
                self._loop.conversation_id,
                exc_info=True,
            )

    async def _continue_anyway(self) -> ConversationState:
        await self._loop._emit(
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(
                    role="user",
                    content=(
                        "Continue — keep working. I've reviewed the failures and "
                        "want you to proceed with your own best next step. Don't just "
                        "repeat the exact action that was failing; adjust your approach."
                    ),
                ),
            )
        )
        await self._loop._emit(
            StatusEvent(status=ConversationStatus.RUNNING, detail="continue_anyway")
        )
        return await self._loop.get_state()

    async def _reject_unknown_option(self, option_id: str) -> ConversationState:
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"User picked an unknown alternative id ({option_id!r}). "
                        "Re-emit the gate or ask for direction.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return await self._loop.get_state()

    async def _record_label_option(self, option: AlternativeOption) -> None:
        reply = option.title.strip()
        if option.description.strip():
            reply = (
                f"{reply}. {option.description.strip()}"
                if reply
                else option.description.strip()
            )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content=reply),
            )
        )

    async def _prepare_runnable_option(
        self,
        alternative: AlternativesEvent,
        option: AlternativeOption,
    ) -> tuple[Disp, Event | None]:
        await self._loop._emit(
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(
                    role="user",
                    content=(
                        f"Try the alternative approach: “{option.title}”. "
                        f"{option.description}"
                    ),
                ),
            )
        )
        action = ActionEvent(
            source=EventSource.AGENT,
            agent_view_id=alternative.agent_view_id,
            thought=f"User picked alternative: {option.title}. {option.description}",
            tool_call=ToolCall(
                tool_name=option.tool_name,
                arguments=option.arguments,
            ),
        )
        if await self._loop._gate_hard_deny(action) is Disp.CONTINUE:
            return Disp.FALLTHROUGH, None
        disp, action = await self._loop._gate_risk_confirm(action)
        if disp is Disp.HALT:
            return Disp.HALT, None
        return Disp.FALLTHROUGH, await self._loop._emit(action)

    async def pick_alternative(self, option_id: str) -> ConversationState:
        """Select a pending alternative without bypassing action gates."""
        emitted: Event | None = None
        async with self._loop._lock, _optional_async_fence(self._loop._control_fence):
            state = await self._loop.get_state()
            if state.execution_status != ConversationStatus.AWAITING_USER_DECISION:
                return state
            events = await self._loop._events()
            alternative = next(
                (event for event in reversed(events) if isinstance(event, AlternativesEvent)),
                None,
            )
            if alternative is None:
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="alternatives_missing",
                    )
                )
                return await self._loop.get_state()
            if not event_matches_current_workspace_view(events, alternative):
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="stale_alternative_selection",
                    )
                )
                return await self._loop.get_state()
            if option_id == _CONTINUE_OPTION_ID:
                return await self._continue_anyway()
            option = next(
                (candidate for candidate in alternative.options if candidate.id == option_id),
                None,
            )
            if option is None:
                return await self._reject_unknown_option(option_id)
            if option.tool_name.strip():
                disp, emitted = await self._prepare_runnable_option(alternative, option)
                if disp is Disp.HALT:
                    return await self._loop.get_state()
            else:
                await self._record_label_option(option)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"alternative_picked:{option.id}",
                )
            )
        if emitted is not None:
            await self._loop._execute_and_observe(cast("ActionEvent", emitted))
        return await self._loop.run()
