"""Internal AgentLoop collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import Event, agent_view_consistent_events
from .engine_contracts import (
    _ACTIVE_AGENT_VIEW_ID,
    _LOG,
    ActionEvent,
    AgentViewSuperseded,
    AlternativesEvent,
    Any,
    ConversationState,
    ConversationStatus,
    Disp,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    OperatingMode,
    PlanEvent,
    ToolResult,
    View,
    _admit_run_intent_for_view,
    _optional_async_fence,
    _read_churn_nudge_message,
    _route_event,
    cast,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
    route_plan_approval_gate,
    signals,
    view_render,
)

if TYPE_CHECKING:
    from .boundaries import StreamHook
    from .loop_facade_compat import _AgentLoopCompatibility as AgentLoop


class LoopRuntime:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @property
    def _assist(self) -> bool:
        """Read-only shim for the legacy weak-model gate. All collaborators
        that read `self._loop._assist` continue to work without changes because
        this property exposes the same name they already access. The SINGLE
        source of truth is `_model_policy.assist`."""
        return self._loop._model_policy.assist

    def _driver_context_window(self) -> int | None:
        """Return the immutable driver window used to compose this loop.

        View rendering must not re-probe independently after the executor and
        condenser have already fixed their budgets; one loop therefore owns one
        resolved value for its lifetime.
        """

        return self._loop._driver_context_window_value

    def _build_stream_hook(self) -> StreamHook | None:
        """Delegate to the driver while preserving the test seam."""
        return self._loop._driver.build_stream_hook()

    async def _emit(self, event: Event) -> Event:
        view_id = _ACTIVE_AGENT_VIEW_ID.get()
        if (
            view_id is not None
            and event.agent_view_id is None
            and event.source is not EventSource.USER
        ):
            event = cast("Event", event.model_copy(update={"agent_view_id": view_id}))
        return await _route_event(
            self._loop.store, self._loop.conversation_id, event, self._loop._terminal_commit_hook
        )

    async def _events(self) -> list[Event]:
        return cast(
            "list[Event]",
            agent_view_consistent_events(
                await self._loop.store.get_events(self._loop.conversation_id)
            ),
        )

    async def _prepare_executor(self, events: list[Event] | None = None) -> None:
        """Reconcile an optional dynamic executor before advertise/effect."""
        prepare = getattr(self._loop.executor, "prepare_for_events", None)
        if callable(prepare):
            await cast(Any, prepare)(events if events is not None else await self._loop._events())

    async def _assert_current_agent_view(self) -> None:
        events = await self._loop.store.get_events(self._loop.conversation_id)
        intent = latest_workspace_run_intent(events)
        view_id = _ACTIVE_AGENT_VIEW_ID.get()
        if intent is not None and (
            view_id is None or current_workspace_agent_view_id(events) != view_id
        ):
            raise AgentViewSuperseded("model view was superseded before provider use")

    async def get_state(self) -> ConversationState:
        return await self._loop.store.get_state(self._loop.conversation_id)

    async def _event_by_id(self, event_id: str) -> Event | None:
        for e in await self._loop._events():
            if e.id == event_id:
                return e
        return None

    def _recent(self, events: list[Event]) -> list[Event]:
        return events[-self._loop._stuck.required_scan_window() :]

    def _workflow_router_phase_active(self) -> bool:
        scope = getattr(self._loop.executor, "_scope", None)
        return getattr(scope, "preset", None) == "workflow_router"

    def _effective_mode(self, events: list[Event]) -> OperatingMode:
        return signals.effective_mode(
            events,
            execution=self._loop._execution_mode,
            default=self._loop.mode,
        )

    def _reconcile_mode_from_events(self, events: list[Event]) -> OperatingMode:
        mode = self._loop._effective_mode(events)
        if self._loop.mode != mode:
            _LOG.warning(
                "Reconciling loop mode for %s from %s to %s using event-log markers",
                self._loop.conversation_id,
                self._loop.mode.value,
                mode.value,
            )
            self._loop.mode = mode
        return mode

    def _readonly_tool_names(self) -> frozenset[str] | None:
        """Delegates to Driver.readonly_tool_names (used by the Observer F9 gate)."""
        return self._loop._driver.readonly_tool_names()

    def _tools_for_step(self, *, suppress_meta_tools: bool = False) -> list:
        """Delegates to Driver.tools_for_step. Kept as an instance method for tests."""
        return self._loop._driver.tools_for_step(suppress_meta_tools=suppress_meta_tools)

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Delegates to Planner.plan_from_args (planning gate + propose intercept)."""
        return self._loop._planner.plan_from_args(arguments, events)

    def _alternatives_from_args(
        self, arguments: dict, events: list[Event]
    ) -> AlternativesEvent | None:
        """Delegates to Planner.alternatives_from_args (the ask_user intercept)."""
        return self._loop._planner.alternatives_from_args(arguments, events)

    async def _workspace_snapshot_message(self, events: list[Event]) -> LLMMessage | None:
        """Delegates to view_render.workspace_snapshot_message over the executor's
        sandbox. Kept as an instance method for tests + the ViewBuilder."""
        return await view_render.workspace_snapshot_message(
            getattr(self._loop.executor, "sandbox", None), events
        )

    def _recitation_signature(self, events: list[Event]) -> str | None:
        return self._loop._recit.recitation_signature(events)

    def _should_emit_recitation(self, events: list[Event]) -> bool:
        return self._loop._recit.should_emit_recitation(events)

    def _gate_recitation(
        self, view: View, events: list[Event], *, context_pack_active: bool = False
    ) -> View:
        return self._loop._recit.gate_recitation(
            view, events, context_pack_active=context_pack_active
        )

    def _should_emit_reground(self, events: list[Event]) -> bool:
        return self._loop._recit.should_emit_reground(events)

    async def _maybe_emit_reground(self, events: list[Event]) -> list[Event]:
        return await self._loop._recit.maybe_emit_reground(events)

    async def _materialize_view(self, events: list[Event]) -> View:
        """Render exactly the supplied history without changing authority state.

        This remains the deterministic projection seam used by recitation,
        truncation, and context tests.  Durable run-intent admission belongs to
        ``_materialize_current_view`` so rendering a caller-owned history can
        never silently replace it with unrelated store contents.
        """
        return await self._loop._view.build(events)

    async def _materialize_current_view(self) -> tuple[View, list[Event]]:
        """Admit and render the latest durable model view under the control fence."""

        # Admission is an authority transition, so serialize it with workspace
        # effects, host mutations, and control decisions. Re-read after taking
        # the fence: a competing effect may have completed while we waited, and
        # the new view must include its paired Action/result rather than split it.
        async with _optional_async_fence(self._loop._control_fence):
            events = await self._loop.store.get_events(self._loop.conversation_id)
            events, view_id = await _admit_run_intent_for_view(
                self._loop.store, self._loop.conversation_id, events
            )
            _ACTIVE_AGENT_VIEW_ID.set(view_id)
            await self._loop._prepare_executor(events)
            consistent_events = cast("list[Event]", agent_view_consistent_events(events))
            view, consistent_events = await self._loop._view.build_with_horizon(consistent_events)
            await self._loop._assert_current_agent_view()
        return view, consistent_events

    def _f8_shrink_file_write_args(
        self, messages: list[LLMMessage], events: list[Event]
    ) -> list[LLMMessage]:
        """Delegates to view_render.f8_shrink_file_write_args (pure transform).
        Kept as an instance method for tests + the ViewBuilder."""
        return view_render.f8_shrink_file_write_args(messages, events)

    async def _hard_reset(self, events: list[Event]) -> bool:
        """Delegates to Observer.hard_reset (self._observe). Kept as an instance
        method for the driver-step call site."""
        return await self._loop._observe.hard_reset(events)

    async def _write_pmx_memory_fact(self, scope: str, fact: str) -> None:
        await self._loop._recit.write_pmx_memory_fact(scope, fact)

    async def _execute_and_observe(self, action: ActionEvent) -> None:
        """Delegates to Observer.execute_and_observe (self._observe). Kept as an
        instance method for run()/confirm()/pick_alternative()/verify + tests."""
        token = _ACTIVE_AGENT_VIEW_ID.set(action.agent_view_id)
        try:
            await self._loop._observe.execute_and_observe(action)
        finally:
            _ACTIVE_AGENT_VIEW_ID.reset(token)

    async def _run_fanout(
        self, args: dict, events: list[Event], *, call_id: str = ""
    ) -> ToolResult:
        """Delegates to Observer.run_fanout. Kept as an instance method so tests
        can override the fan-out seam (`loop._run_fanout = ...`)."""
        return await self._loop._observe.run_fanout(args, events, call_id=call_id)

    async def _maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        """Delegates to PlanStepConditions (self._plan_cond). Kept as an instance
        method for the execute-and-observe call site."""
        await self._loop._plan_cond.maybe_emit_plan_step_done_condition_note(action)

    async def _finish_dod_gate_passed(self) -> bool:
        """Delegates to FinishGate.finish_dod_gate_passed (self._finish). Kept as an
        instance method for tests."""
        return await self._loop._finish.verification.finish_dod_gate_passed()

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
        for hook in self._loop._stop_hooks:
            if not await hook.allow_stop(state, events):
                return False
        return True

    async def _post_noop_valve(self) -> Disp:
        """Delegates to Valve.post_noop_valve (self._valve). Called by FinishGate
        and the planning-mode gate."""
        return await self._loop._valve.post_noop_valve()

    async def _maybe_apply_read_churn_valve(self, action: ActionEvent) -> Disp:
        if self._loop.mode == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH
        if action.tool_call is None or action.tool_call.tool_name != "file_read":
            return Disp.FALLTHROUGH
        events = await self._loop._events()
        if not any(
            isinstance(event, ObservationEvent)
            and event.action_id == action.id
            and event.tool_result.success
            for event in events
        ):
            return Disp.FALLTHROUGH
        state = signals.read_churn_state(events)
        if state is None:
            return Disp.FALLTHROUGH
        if state.warning_count is not None:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=_read_churn_nudge_message(state.path, state.warning_count),
                    ),
                    meta={
                        "diagnostic": signals.READ_CHURN_NUDGE_DIAGNOSTIC,
                        "path": state.path,
                        "count": state.warning_count,
                        "streak": state.warning_count,
                    },
                )
            )
            events = await self._loop._events()
        if state.invisible_noops <= 0:
            return Disp.FALLTHROUGH
        self._loop._invisible_steps = max(self._loop._invisible_steps, state.invisible_noops)
        noops = signals.consecutive_noops(events) + self._loop._invisible_steps
        if await self._loop._valve.actionless_valve(events, noops):
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def _land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
        extra_meta: dict[str, str | int] | None = None,
    ) -> None:
        """Delegate all breaker dead-ends to the shared explain+ask lander."""
        await self._loop._valve.land_blocked(
            reason=reason,
            guidance=guidance,
            legacy_status=legacy_status,
            legacy_detail=legacy_detail,
            extra_meta=extra_meta,
        )

    async def _maybe_synthesize_finish_after_actionless_pauses(
        self, state: ConversationState, events: list[Event]
    ) -> Disp:
        """REL-RC-P: before another resumed model turn, attempt finish when the
        event log shows repeated actionless pauses after productive work."""
        if self._loop.mode == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH
        if not signals.should_synthesize_finish_after_actionless_pauses(events):
            return Disp.FALLTHROUGH
        return await self._loop._finish.synthetic_finish_after_actionless_pauses(state, events)

    async def _route_plan_approval_gate(self, plan: PlanEvent) -> Disp:
        """Route a newly-emitted PlanEvent through the normal approval gate."""
        return await route_plan_approval_gate(self._loop, plan)

    def _harvest_prose_plan(self, events: list[Event]) -> PlanEvent | None:
        steps = signals.harvest_prose_plan_steps(events)
        if not steps:
            return None
        instruction = signals.current_revision_instruction(events) or ""
        summary = signals.latest_prose_plan_summary(events) or instruction[:120] or "Proposed plan"
        return PlanEvent(
            summary=summary,
            steps=steps,
            revision=signals.next_plan_revision(events),
            context=(
                "Synthesized by REL-RC-N from the assistant's latest prose plan "
                "after the force-submit ladder was exhausted."
            ),
        )
