"""Private legacy delegation surface for the removable ``AgentLoop`` facade."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal

from .engine_contracts import (
    ConversationStatus,
    signals,
)

if TYPE_CHECKING:
    from . import view_render
    from .boundaries import (
        Agent,
        ConfirmationPolicy,
        SecurityAnalyzer,
        StreamHook,
        ToolExecutor,
    )
    from .conversation_controls import ConversationControls as _ConversationControls
    from .engine_contracts import (
        ActionEvent,
        AgentStep,
        AlternativesEvent,
        Condenser,
        ControlFenceFactory,
        ConversationState,
        Disp,
        DoDEvaluator,
        DoDPredicate,
        Driver,
        Event,
        EventStore,
        FinishGate,
        HostVerifier,
        LLMMessage,
        LLMRouter,
        MetaToolHandlers,
        ModelExecutionPolicy,
        Observer,
        OperatingMode,
        PlanEvent,
        Planner,
        PlanStepConditions,
        RecitationRegrounder,
        SealabilityProbe,
        StatusEvent,
        StopHook,
        StuckDetector,
        Summarizer,
        TerminalCommitHook,
        ToolResult,
        Valve,
        VerifierJudge,
        VerifierVerdictEvent,
        View,
        WorkflowRun,
    )
    from .loop_runtime import LoopRuntime as _LoopRuntime
    from .plan_controls import PlanApprovalController as _PlanApprovalController
    from .planning_gates import PlanningGateController as _PlanningGateController
    from .replanning import ReplanningController as _ReplanningController
    from .transition import TransitionCoordinator as _TransitionCoordinator


class _AgentLoopCompatibility:
    """Type declarations and one-hop delegates retained until PKG-13."""

    conversation_id: str
    store: EventStore
    agent: Agent
    executor: ToolExecutor
    analyzer: SecurityAnalyzer
    policy: ConfirmationPolicy
    condenser: Condenser
    summarizer: Summarizer
    mode: OperatingMode
    max_iterations: int
    stream_sink: Callable[[dict], None] | None
    _autonomous: bool
    _quiet: bool
    _strict_appkit_active_reader: Callable[[], bool] | None
    _declared_delivery_kind: Literal["app", "files"] | None
    _delivery_contract_resolver: Callable[[str], Awaitable[None]] | None
    _finish_alias: str | None
    _workflow_run: WorkflowRun | None
    _terminal_commit_hook: TerminalCommitHook
    _control_fence: ControlFenceFactory
    _model_policy: ModelExecutionPolicy
    _driver_context_window_value: int | None
    _router: LLMRouter
    _planning_tools: frozenset[str]
    _plan_tool: str
    _execution_mode: OperatingMode
    _plan_nudges: int
    _revision_force_submit_enabled: bool
    _plan_explore_reads: int
    _execution_nudges: int
    _browser_verify_refusals: int
    _identical_plan_revisions: int
    _finish_verify_refusals: int
    _finish_verify_strips: int
    _finish_seal_refusals: int
    _workflow_output_contract_refusals: int
    _fanout_count: int
    _fanout_max: int
    _ACTIONLESS_BREAK_CAP: int
    _invisible_steps: int
    _max_consecutive_noops: int
    _circuit_breaker_threshold: int
    _auto_continue_cap: int
    _stop_hooks: list[StopHook]
    _stuck: StuckDetector
    _recit: RecitationRegrounder
    _view: view_render.ViewBuilder
    _plan_cond: PlanStepConditions
    _observe: Observer
    _driver: Driver
    _finish: FinishGate
    _valve: Valve
    _meta: MetaToolHandlers
    _planner: Planner
    _veto_feedback: str
    _dod_evaluator_factory: Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None
    _host_verifier: HostVerifier | None
    _host_verify_timeout_s: float
    _host_verifier_verdict_hook: Callable[[VerifierVerdictEvent], Awaitable[None]] | None
    _host_verify_authoritative: bool
    _verifier_judge: VerifierJudge | None
    _verifier_judge_timeout_s: float
    _finish_sealability_probe: SealabilityProbe | None
    _finish_seal_timeout_s: float
    _dod_refusals: int
    _dictated_content_refusals: int
    _lock: asyncio.Lock
    _pause_requested: asyncio.Event
    _retry_interrupt: asyncio.Event
    _recitation_cadence: int
    _recitation_step_count: int
    _recitation_last_signature: str | None
    _hs03_reground_cadence: int
    _plan_step_predicates: dict[tuple[int, int], DoDPredicate]
    _bootstrap_emitted: bool
    _hs03_reground_post_resume_emitted: bool
    _hs03_reground_last_action_count: int
    _runtime: _LoopRuntime
    _planning_gates: _PlanningGateController
    _transition: _TransitionCoordinator
    _conversation_controls: _ConversationControls
    _plan_controls: _PlanApprovalController
    _replanning: _ReplanningController

    if TYPE_CHECKING:

        @staticmethod
        def _current_agent_view_id() -> str | None: ...

        async def _emit(self, event: Event) -> Event: ...

        async def get_state(self) -> ConversationState: ...

        async def _run_drive(self) -> ConversationState: ...

        async def run(self) -> ConversationState: ...

        async def send_message(
            self,
            text: str,
            *,
            steer: bool = False,
        ) -> ConversationState: ...

        async def steer(self, text: str) -> ConversationState: ...

        async def confirm(self) -> ConversationState: ...

        async def reject(
            self,
            reason: str = "rejected by user",
        ) -> ConversationState: ...

        async def approve_plan(self) -> ConversationState: ...

        async def pick_alternative(self, option_id: str) -> ConversationState: ...

        async def enter_planning(self, text: str = "") -> ConversationState: ...

        async def pause(self) -> ConversationState: ...

        async def resume(self) -> ConversationState: ...

        async def cancel(self) -> ConversationState: ...

    _STREAMING_WRITE_TOOLS = ("file_write", "file_append", "write_file", "file_edit")
    _STREAM_FLUSH_CHARS = 24
    _MEMORY_PATH = ".disco/MEMORY.md"
    _LEGACY_MEMORY_PATH = ".pmx/MEMORY.md"
    _has_unprocessed_user_message = staticmethod(signals.has_unprocessed_user_message)

    @staticmethod
    def _plan_verification_predicates(plan: PlanEvent | None) -> list[DoDPredicate]:
        """Revision-scoped model predicates retained on an approved PlanEvent."""
        if plan is None:
            return []
        return [step.done_condition for step in plan.steps if step.done_condition is not None]

    @property
    def _assist(self) -> bool:
        return self._runtime._assist

    def _driver_context_window(self) -> int | None:
        return self._runtime._driver_context_window()

    def _build_stream_hook(self) -> StreamHook | None:
        return self._runtime._build_stream_hook()

    async def _events(self) -> list[Event]:
        return await self._runtime._events()

    async def _prepare_executor(self, events: list[Event] | None = None) -> None:
        return await self._runtime._prepare_executor(events)

    async def _assert_current_agent_view(self) -> None:
        return await self._runtime._assert_current_agent_view()

    async def _event_by_id(self, event_id: str) -> Event | None:
        return await self._runtime._event_by_id(event_id)

    def _recent(self, events: list[Event]) -> list[Event]:
        return self._runtime._recent(events)

    def _workflow_router_phase_active(self) -> bool:
        return self._runtime._workflow_router_phase_active()

    def _effective_mode(self, events: list[Event]) -> OperatingMode:
        return self._runtime._effective_mode(events)

    def _reconcile_mode_from_events(self, events: list[Event]) -> OperatingMode:
        return self._runtime._reconcile_mode_from_events(events)

    def _readonly_tool_names(self) -> frozenset[str] | None:
        return self._runtime._readonly_tool_names()

    def _tools_for_step(self, *, suppress_meta_tools: bool = False) -> list:
        return self._runtime._tools_for_step(suppress_meta_tools=suppress_meta_tools)

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        return self._runtime._plan_from_args(arguments, events)

    def _alternatives_from_args(
        self, arguments: dict, events: list[Event]
    ) -> AlternativesEvent | None:
        return self._runtime._alternatives_from_args(arguments, events)

    async def _workspace_snapshot_message(self, events: list[Event]) -> LLMMessage | None:
        return await self._runtime._workspace_snapshot_message(events)

    def _recitation_signature(self, events: list[Event]) -> str | None:
        return self._runtime._recitation_signature(events)

    def _should_emit_recitation(self, events: list[Event]) -> bool:
        return self._runtime._should_emit_recitation(events)

    def _gate_recitation(
        self, view: View, events: list[Event], *, context_pack_active: bool = False
    ) -> View:
        return self._runtime._gate_recitation(
            view,
            events,
            context_pack_active=context_pack_active,
        )

    def _should_emit_reground(self, events: list[Event]) -> bool:
        return self._runtime._should_emit_reground(events)

    async def _maybe_emit_reground(self, events: list[Event]) -> list[Event]:
        return await self._runtime._maybe_emit_reground(events)

    async def _materialize_view(self, events: list[Event]) -> View:
        return await self._runtime._materialize_view(events)

    async def _materialize_current_view(self) -> tuple[View, list[Event]]:
        return await self._runtime._materialize_current_view()

    def _f8_shrink_file_write_args(
        self, messages: list[LLMMessage], events: list[Event]
    ) -> list[LLMMessage]:
        return self._runtime._f8_shrink_file_write_args(messages, events)

    async def _hard_reset(self, events: list[Event]) -> bool:
        return await self._runtime._hard_reset(events)

    async def _write_pmx_memory_fact(self, scope: str, fact: str) -> None:
        return await self._runtime._write_pmx_memory_fact(scope, fact)

    async def _execute_and_observe(self, action: ActionEvent) -> None:
        return await self._runtime._execute_and_observe(action)

    async def _run_fanout(
        self, args: dict, events: list[Event], *, call_id: str = ""
    ) -> ToolResult:
        return await self._runtime._run_fanout(args, events, call_id=call_id)

    async def _maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        return await self._runtime._maybe_emit_plan_step_done_condition_note(action)

    async def _finish_dod_gate_passed(self) -> bool:
        return await self._runtime._finish_dod_gate_passed()

    async def _stop_allowed(
        self,
        state: ConversationState,
        events: list[Event],
    ) -> bool:
        return await self._runtime._stop_allowed(state, events)

    async def _post_noop_valve(self) -> Disp:
        return await self._runtime._post_noop_valve()

    async def _maybe_apply_read_churn_valve(self, action: ActionEvent) -> Disp:
        return await self._runtime._maybe_apply_read_churn_valve(action)

    async def _land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
        extra_meta: dict[str, str | int] | None = None,
    ) -> None:
        return await self._runtime._land_blocked(
            reason=reason,
            guidance=guidance,
            legacy_status=legacy_status,
            legacy_detail=legacy_detail,
            extra_meta=extra_meta,
        )

    async def _maybe_synthesize_finish_after_actionless_pauses(
        self,
        state: ConversationState,
        events: list[Event],
    ) -> Disp:
        return await self._runtime._maybe_synthesize_finish_after_actionless_pauses(
            state,
            events,
        )

    async def _route_plan_approval_gate(self, plan: PlanEvent) -> Disp:
        return await self._runtime._route_plan_approval_gate(plan)

    def _harvest_prose_plan(self, events: list[Event]) -> PlanEvent | None:
        return self._runtime._harvest_prose_plan(events)

    async def _gate_planning_mode(
        self,
        step: AgentStep,
        events: list[Event],
    ) -> Disp:
        return await self._planning_gates._gate_planning_mode(step, events)

    async def _gate_out_of_phase_submit_plan(
        self,
        step: AgentStep,
        events: list[Event],
    ) -> Disp:
        return await self._planning_gates._gate_out_of_phase_submit_plan(
            step,
            events,
        )

    async def _gate_stuck_escape_tool_quarantine(
        self,
        step: AgentStep,
        events: list[Event],
    ) -> Disp:
        return await self._planning_gates._gate_stuck_escape_tool_quarantine(
            step,
            events,
        )

    async def _gate_hard_deny(self, action: ActionEvent) -> Disp:
        return await self._planning_gates._gate_hard_deny(action)

    async def _gate_risk_confirm(
        self,
        action: ActionEvent,
    ) -> tuple[Disp, ActionEvent]:
        return await self._planning_gates._gate_risk_confirm(action)

    async def _plan_approval_status(
        self,
        plan: PlanEvent,
        events: list[Event],
    ) -> StatusEvent:
        return await self._plan_controls._plan_approval_status(plan, events)

    async def _seed_context_from_plan(self) -> None:
        return await self._plan_controls._seed_context_from_plan()

    async def _maybe_reenter_planning_for_followup(
        self,
        events: list[Event],
    ) -> bool:
        return await self._replanning._maybe_reenter_planning_for_followup(events)

    async def _enter_revision_planning(self, text: str) -> None:
        return await self._replanning._enter_revision_planning(text)

    async def _gate_midstep_steer_replan(self, step: AgentStep) -> Disp:
        return await self._replanning._gate_midstep_steer_replan(step)


_COMPATIBILITY_METHODS = (
    "_plan_verification_predicates",
    "_assist",
    "_driver_context_window",
    "_build_stream_hook",
    "_events",
    "_prepare_executor",
    "_assert_current_agent_view",
    "_event_by_id",
    "_recent",
    "_workflow_router_phase_active",
    "_effective_mode",
    "_reconcile_mode_from_events",
    "_readonly_tool_names",
    "_tools_for_step",
    "_plan_from_args",
    "_alternatives_from_args",
    "_workspace_snapshot_message",
    "_recitation_signature",
    "_should_emit_recitation",
    "_gate_recitation",
    "_should_emit_reground",
    "_maybe_emit_reground",
    "_materialize_view",
    "_materialize_current_view",
    "_f8_shrink_file_write_args",
    "_hard_reset",
    "_write_pmx_memory_fact",
    "_execute_and_observe",
    "_run_fanout",
    "_maybe_emit_plan_step_done_condition_note",
    "_finish_dod_gate_passed",
    "_stop_allowed",
    "_post_noop_valve",
    "_maybe_apply_read_churn_valve",
    "_land_blocked",
    "_maybe_synthesize_finish_after_actionless_pauses",
    "_route_plan_approval_gate",
    "_harvest_prose_plan",
    "_gate_planning_mode",
    "_gate_out_of_phase_submit_plan",
    "_gate_stuck_escape_tool_quarantine",
    "_gate_hard_deny",
    "_gate_risk_confirm",
    "_plan_approval_status",
    "_seed_context_from_plan",
    "_maybe_reenter_planning_for_followup",
    "_enter_revision_planning",
    "_gate_midstep_steer_replan",
)


def _install_agent_loop_compatibility(target: type[Any]) -> None:
    """Attach the fixed legacy private surface without changing public ancestry."""
    namespace = vars(_AgentLoopCompatibility)
    for name in _COMPATIBILITY_METHODS:
        setattr(target, name, namespace[name])
