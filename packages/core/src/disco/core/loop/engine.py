"""Compatibility facade and sole ordered AgentLoop entry point."""
# ruff: noqa: E501, I001

from __future__ import annotations

from typing import TYPE_CHECKING

from .conversation_controls import ConversationControls as _ConversationControls
# fmt: off
from .engine_contracts import (
    _ACTIVE_AGENT_VIEW_ID as _ACTIVE_AGENT_VIEW_ID, _BOOKKEEPING_TOOLS as _BOOKKEEPING_TOOLS, _CONTINUE_OPTION_ID as _CONTINUE_OPTION_ID, _DEFAULT_MODEL_POLICY as _DEFAULT_MODEL_POLICY,
    _DEFAULT_VETO_FEEDBACK as _DEFAULT_VETO_FEEDBACK, _DELEGATE_BEHAVIOR as _DELEGATE_BEHAVIOR, _DELEGATE_EXPLORE_DESCRIPTION as _DELEGATE_EXPLORE_DESCRIPTION, _DELEGATE_EXPLORE_SCHEMA as _DELEGATE_EXPLORE_SCHEMA,
    _DELEGATE_EXPLORE_TOOL_SPEC as _DELEGATE_EXPLORE_TOOL_SPEC, _DESIGN_DIRECTION_DECK_HINTS as _DESIGN_DIRECTION_DECK_HINTS, _DESIGN_DIRECTION_SITE_APP_HINTS as _DESIGN_DIRECTION_SITE_APP_HINTS, _DOD_REFUSAL_CAP as _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE as _EXECUTION_NUDGE, _FANOUT_INPUT_MAX_CHARS as _FANOUT_INPUT_MAX_CHARS, _FANOUT_MAX_PER_RUN as _FANOUT_MAX_PER_RUN, _FINALIZE_ONLY_BEHAVIOR as _FINALIZE_ONLY_BEHAVIOR,
    _FINISH_BEHAVIOR as _FINISH_BEHAVIOR, _FINISH_DESCRIPTION as _FINISH_DESCRIPTION, _FINISH_SCHEMA as _FINISH_SCHEMA, _FINISH_TOOL_SPEC as _FINISH_TOOL_SPEC,
    _FINISH_VERIFY_CAP as _FINISH_VERIFY_CAP, _FORCE_SUBMIT_DIRECTIVE as _FORCE_SUBMIT_DIRECTIVE, _HS03_REGROUND_INTERVAL as _HS03_REGROUND_INTERVAL, _INVALID_PLAN_DONE_CONDITION_CAP as _INVALID_PLAN_DONE_CONDITION_CAP,
    _LOG as _LOG, _MIDSTEP_STEER_REFUSAL as _MIDSTEP_STEER_REFUSAL, _NON_PRODUCTIVE_TOOLS as _NON_PRODUCTIVE_TOOLS, _PLAN_NUDGE as _PLAN_NUDGE,
    _PLANNING_TOOL_REFUSAL_ESCALATE_AT as _PLANNING_TOOL_REFUSAL_ESCALATE_AT, _PLANNING_TOOL_REFUSAL_NARROW_AT as _PLANNING_TOOL_REFUSAL_NARROW_AT, _RECITATION_CADENCE_DEFAULT as _RECITATION_CADENCE_DEFAULT, _RECITATION_SENTINEL as _RECITATION_SENTINEL,
    _REMEMBER_BEHAVIOR as _REMEMBER_BEHAVIOR, _REMEMBER_DESCRIPTION as _REMEMBER_DESCRIPTION, _REMEMBER_SCHEMA as _REMEMBER_SCHEMA, _REMEMBER_TOOL_SPEC as _REMEMBER_TOOL_SPEC,
    _REVISION_FORCE_SUBMIT_K as _REVISION_FORCE_SUBMIT_K, _SERVE_DESCRIPTION as _SERVE_DESCRIPTION, _SERVE_SCHEMA as _SERVE_SCHEMA, _SERVE_TOOL_SPEC as _SERVE_TOOL_SPEC,
    _TERMINAL_FOR_NOW as _TERMINAL_FOR_NOW, _WORKFLOW_ROUTER_PLAN_NUDGE as _WORKFLOW_ROUTER_PLAN_NUDGE, _WS_MAX_FILES as _WS_MAX_FILES, _WS_PER_FILE_CHARS as _WS_PER_FILE_CHARS,
    _WS_READ_TIMEOUT_S as _WS_READ_TIMEOUT_S, _WS_TOTAL_CHARS as _WS_TOTAL_CHARS, AbstractAsyncContextManager as AbstractAsyncContextManager, ActionEvent as ActionEvent,
    Agent as Agent, AgentErrorEvent as AgentErrorEvent, AgentStep as AgentStep, AgentViewSuperseded as AgentViewSuperseded,
    AlternativesEvent as AlternativesEvent, Any as Any, Awaitable as Awaitable, Callable as Callable,
    Condenser as Condenser, ConfirmationPolicy as ConfirmationPolicy, ContextVar as ContextVar, ControlFenceFactory as ControlFenceFactory,
    ConversationState as ConversationState, ConversationStatus as ConversationStatus, Coroutine as Coroutine, Disp as Disp,
    DoDEvaluator as DoDEvaluator, DoDPredicate as DoDPredicate, Driver as Driver, EffectCapability as EffectCapability,
    ErrorEvent as ErrorEvent, Event as Event, EventSource as EventSource, EventStore as EventStore,
    FinishGate as FinishGate, HostVerifier as HostVerifier, Iterable as Iterable, LLMMessage as LLMMessage,
    LLMRouter as LLMRouter, MessageEvent as MessageEvent, MetaToolHandlers as MetaToolHandlers, ModelExecutionPolicy as ModelExecutionPolicy,
    ObservationEvent as ObservationEvent, Observer as Observer, OperatingMode as OperatingMode, PlanEvent as PlanEvent,
    Planner as Planner, PlanRevisionWeakeningError as PlanRevisionWeakeningError, PlanStep as PlanStep, PlanStepConditions as PlanStepConditions,
    PlanVerificationTransition as PlanVerificationTransition, RecitationRegrounder as RecitationRegrounder, SealabilityProbe as SealabilityProbe, SecurityAnalyzer as SecurityAnalyzer,
    StatusEvent as StatusEvent, StopHook as StopHook, StuckDetector as StuckDetector, StuckThresholds as StuckThresholds,
    Summarizer as Summarizer, TerminalCommitHook as TerminalCommitHook, ToolBehavior as ToolBehavior, ToolCall as ToolCall,
    ToolExecutor as ToolExecutor, ToolResult as ToolResult, Valve as Valve, VerifierJudge as VerifierJudge,
    VerifierVerdictEvent as VerifierVerdictEvent, View as View, WorkflowRun as WorkflowRun, WorkspaceMutationEvent as WorkspaceMutationEvent,
    _admit_run_intent_for_view as _admit_run_intent_for_view, _app_verify_command as _app_verify_command, _browser_verified as _browser_verified, _delegate_explore_tool_singleton as _delegate_explore_tool_singleton,
    _delegate_explore_tool_spec as _delegate_explore_tool_spec, _design_direction_brief_text as _design_direction_brief_text, _DoDWorkspaceUnavailable as _DoDWorkspaceUnavailable, _finish_alias_tool_spec as _finish_alias_tool_spec,
    _finish_tool_singleton as _finish_tool_singleton, _finish_tool_spec as _finish_tool_spec, _has_any_hint as _has_any_hint, _hint_text as _hint_text,
    _is_web_deliverable as _is_web_deliverable, _last_productive_seq as _last_productive_seq, _latest_browser_error as _latest_browser_error, _new_retry_interrupt as _new_retry_interrupt,
    _optional_async_fence as _optional_async_fence, _out_of_phase_submit_plan_refusal as _out_of_phase_submit_plan_refusal, _planning_tool_refusal_message as _planning_tool_refusal_message, _read_churn_nudge_message as _read_churn_nudge_message,
    _remember_tool_singleton as _remember_tool_singleton, _remember_tool_spec as _remember_tool_spec, _route_event as _route_event, _serve_tool_singleton as _serve_tool_singleton,
    _serve_tool_spec as _serve_tool_spec, _static_verify_command as _static_verify_command, _virtual_behavior as _virtual_behavior, _workflow_finish_tool_spec as _workflow_finish_tool_spec,
    agent_view_consistent_events as agent_view_consistent_events, assert_plan_revision_approvable as assert_plan_revision_approvable, asynccontextmanager as asynccontextmanager, asyncio as asyncio,
    cast as cast, current_workspace_agent_view_id as current_workspace_agent_view_id, event_matches_current_workspace_view as event_matches_current_workspace_view, harvest_revision_plan_after_refusal as harvest_revision_plan_after_refusal,
    host_verify_authoritative_enabled as host_verify_authoritative_enabled, is_finish_tool_name as is_finish_tool_name, latest_workspace_run_intent as latest_workspace_run_intent, logging as logging,
    os as os, planning_tool_refusal_streak as planning_tool_refusal_streak, predicate_fingerprints as predicate_fingerprints, preflight_plan_revision as preflight_plan_revision,
    reject_plan_weakening as reject_plan_weakening, route_plan_approval_gate as route_plan_approval_gate, signals as signals, uuid as uuid,
    validate_plan_conditions as validate_plan_conditions, view_render as view_render, workflow_finish_tool_description as workflow_finish_tool_description, workflow_finish_tool_schema as workflow_finish_tool_schema,
)
# fmt: on
from .loop_initialization import initialize_agent_loop as _initialize_agent_loop
from .loop_runtime import LoopRuntime as _LoopRuntime
from .plan_controls import PlanApprovalController as _PlanApprovalController
from .plan_submission import _handle_submitted_plan as _handle_submitted_plan
from .planning_gates import PlanningGateController as _PlanningGateController
from .replanning import ReplanningController as _ReplanningController
from .transition import TransitionCoordinator as _TransitionCoordinator

if TYPE_CHECKING:
    from .boundaries import StreamHook


class _AgentLoopRuntimeCompatibility:
    """Removable private delegation surface for legacy AgentLoop seams."""

    _runtime: _LoopRuntime

    _STREAMING_WRITE_TOOLS = ("file_write", "file_append", "write_file", "file_edit")
    _STREAM_FLUSH_CHARS = 24
    _MEMORY_PATH = ".disco/MEMORY.md"
    _LEGACY_MEMORY_PATH = ".pmx/MEMORY.md"
    _has_unprocessed_user_message = staticmethod(signals.has_unprocessed_user_message)

    @staticmethod
    def _current_agent_view_id() -> str | None:
        return _ACTIVE_AGENT_VIEW_ID.get()

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
            view, events, context_pack_active=context_pack_active
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

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
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
        self, state: ConversationState, events: list[Event]
    ) -> Disp:
        return await self._runtime._maybe_synthesize_finish_after_actionless_pauses(
            state, events
        )

    async def _route_plan_approval_gate(self, plan: PlanEvent) -> Disp:
        return await self._runtime._route_plan_approval_gate(plan)

    def _harvest_prose_plan(self, events: list[Event]) -> PlanEvent | None:
        return self._runtime._harvest_prose_plan(events)


class AgentLoop:
    """[CONTRACT] The orchestrator and stable compatibility surface."""

    _autonomous: bool
    _quiet: bool
    _strict_appkit_active_reader: Callable[[], bool] | None
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
    _dod_evaluator_factory: (
        Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None
    )
    _host_verifier: HostVerifier | None
    _host_verify_timeout_s: float
    _host_verifier_verdict_hook: (
        Callable[[VerifierVerdictEvent], Awaitable[None]] | None
    )
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

    _compat = _AgentLoopRuntimeCompatibility
    _STREAMING_WRITE_TOOLS = _compat._STREAMING_WRITE_TOOLS
    _STREAM_FLUSH_CHARS = _compat._STREAM_FLUSH_CHARS
    _MEMORY_PATH = _compat._MEMORY_PATH
    _LEGACY_MEMORY_PATH = _compat._LEGACY_MEMORY_PATH
    _has_unprocessed_user_message = staticmethod(signals.has_unprocessed_user_message)
    _current_agent_view_id = staticmethod(_compat._current_agent_view_id)
    _plan_verification_predicates = staticmethod(_compat._plan_verification_predicates)
    _assist = _compat._assist
    _driver_context_window = _compat._driver_context_window
    _build_stream_hook = _compat._build_stream_hook
    _events = _compat._events
    _prepare_executor = _compat._prepare_executor
    _assert_current_agent_view = _compat._assert_current_agent_view
    _event_by_id = _compat._event_by_id
    _recent = _compat._recent
    _workflow_router_phase_active = _compat._workflow_router_phase_active
    _effective_mode = _compat._effective_mode
    _reconcile_mode_from_events = _compat._reconcile_mode_from_events
    _readonly_tool_names = _compat._readonly_tool_names
    _tools_for_step = _compat._tools_for_step
    _plan_from_args = _compat._plan_from_args
    _alternatives_from_args = _compat._alternatives_from_args
    _workspace_snapshot_message = _compat._workspace_snapshot_message
    _recitation_signature = _compat._recitation_signature
    _should_emit_recitation = _compat._should_emit_recitation
    _gate_recitation = _compat._gate_recitation
    _should_emit_reground = _compat._should_emit_reground
    _maybe_emit_reground = _compat._maybe_emit_reground
    _materialize_view = _compat._materialize_view
    _materialize_current_view = _compat._materialize_current_view
    _f8_shrink_file_write_args = _compat._f8_shrink_file_write_args
    _hard_reset = _compat._hard_reset
    _write_pmx_memory_fact = _compat._write_pmx_memory_fact
    _execute_and_observe = _compat._execute_and_observe
    _run_fanout = _compat._run_fanout
    _maybe_emit_plan_step_done_condition_note = (
        _compat._maybe_emit_plan_step_done_condition_note
    )
    _finish_dod_gate_passed = _compat._finish_dod_gate_passed
    _stop_allowed = _compat._stop_allowed
    _post_noop_valve = _compat._post_noop_valve
    _maybe_apply_read_churn_valve = _compat._maybe_apply_read_churn_valve
    _land_blocked = _compat._land_blocked
    _maybe_synthesize_finish_after_actionless_pauses = (
        _compat._maybe_synthesize_finish_after_actionless_pauses
    )
    _route_plan_approval_gate = _compat._route_plan_approval_gate
    _harvest_prose_plan = _compat._harvest_prose_plan
    del _compat

    def __init__(
        self,
        conversation_id: str,
        store: EventStore,
        agent: Agent,
        executor: ToolExecutor,
        router: LLMRouter,
        analyzer: SecurityAnalyzer,
        policy: ConfirmationPolicy,
        condenser: Condenser,
        summarizer: Summarizer,
        *,
        mode: OperatingMode,
        max_iterations: int = 500,
        stop_hooks: list[StopHook] | None = None,
        stuck_thresholds: StuckThresholds | None = None,
        veto_feedback: str = _DEFAULT_VETO_FEEDBACK,
        planning_tools: frozenset[str] = frozenset(),
        plan_tool: str = "submit_plan",
        execution_mode: OperatingMode = OperatingMode.LONG_HORIZON,
        autonomous: bool = False,
        model_policy: ModelExecutionPolicy = _DEFAULT_MODEL_POLICY,
        # The already-resolved context window of this loop's effective driver.
        # Runtime resolves it once (including the per-conversation model pick)
        # and threads the same scalar to the executor, condenser, and view.  A
        # None default preserves legacy/test callers and selects conservative
        # context caps.
        driver_context_window: int | None = None,
        # P6 — the active Build contract's verification finalizer name (e.g.
        # "ready_for_app_verification"). When set, it is advertised + recognized as a
        # per-kind ALIAS of the `finish` virtual tool, routed through the SAME host-truth
        # finish gate (no self-cert). None ⇒ no contract governs ⇒ only plain `finish`
        # (a plain/CUSTOM build NEVER fabricates a finalizer alias).
        finish_alias: str | None = None,
        # C6 — cadence for the plan/objective tail-recap (default: every 5
        # model turns). Set to 1 to recover the old "recite every step"
        # behavior; 0 / negative are clamped to 1. The smolagents math is
        # `(step_number - 1) % interval == 0` (1-indexed: step 1, 1+N,
        # 1+2N, …). 0-indexed: 0, N, 2N, … — equivalent boundary set.
        recitation_cadence: int = _RECITATION_CADENCE_DEFAULT,
        # HS-03 — cadence for the scheduled facts re-grounding (default:
        # every 12 actions since the last resume). Mirrors the
        # `recitation_cadence` seam: a test or operator can pin a
        # smaller / larger interval without code changes. The brief's
        # "e.g. 12" is the production default; tests inject a smaller
        # value to keep the test loop tight. Set to 1 to recover the
        # "re-ground on every step" behavior (NOT recommended — a
        # re-ground on every step would be a token-bloat disaster; the
        # point of the cadence is the throttling). 0 / negative are
        # clamped to 1.
        reground_cadence: int = _HS03_REGROUND_INTERVAL,
        # C1c — DoD evaluator factory (test seam; see _finish_dod_gate_passed).
        # The default (None) builds a DoDEvaluator over the executor's sandbox
        # workspace_root; tests inject a fake-seamed evaluator.
        dod_evaluator_factory: (
            Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None
        ) = None,
        # REL-1c — host-owned verifier shadow seam. None means no host verifier
        # is available and the existing finish flow is byte-identical.
        host_verifier: HostVerifier | None = None,
        host_verify_timeout_s: float = 30.0,
        host_verifier_verdict_hook: (
            Callable[[VerifierVerdictEvent], Awaitable[None]] | None
        ) = None,
        host_verify_authoritative: bool | None = None,
        verifier_judge: VerifierJudge | None = None,
        verifier_judge_timeout_s: float = 30.0,
        # REL-27 finish-time sealability probe (None ⇒ byte-identical legacy;
        # semantics + the 150s>120s timeout floor in seal_gate_allows_finish).
        finish_sealability_probe: SealabilityProbe | None = None,
        finish_seal_timeout_s: float = 150.0,
        workflow_run: WorkflowRun | None = None,
        terminal_commit_hook: TerminalCommitHook = None,
        control_fence: ControlFenceFactory = None,
        quiet: bool = False,
        strict_appkit_active: Callable[[], bool] | None = None,
    ) -> None:
        self.conversation_id: str
        self.store: EventStore
        self.agent: Agent
        self.executor: ToolExecutor
        self.analyzer: SecurityAnalyzer
        self.policy: ConfirmationPolicy
        self.condenser: Condenser
        self.summarizer: Summarizer
        self.mode: OperatingMode
        self.max_iterations: int
        self.stream_sink: Callable[[dict], None] | None
        # Autonomous mode (issue A): no human is available to answer questions or
        # approve plans (headless / unattended runs). Default False = today's
        # interactive behavior, fully unchanged. When True: ask_user/clarify are
        # withheld from the tool schema, the plan is auto-approved inline, and the
        # circuit-breaker's "hand off to the user" becomes a clean forfeit (STUCK)
        # instead of an indefinite AWAITING_USER_DECISION stall.
        # fmt: off
        _initialize_agent_loop(self, conversation_id, store, agent, executor, router, analyzer, policy, condenser, summarizer, mode=mode, max_iterations=max_iterations, stop_hooks=stop_hooks, stuck_thresholds=stuck_thresholds, veto_feedback=veto_feedback, planning_tools=planning_tools, plan_tool=plan_tool, execution_mode=execution_mode, autonomous=autonomous, model_policy=model_policy, driver_context_window=driver_context_window, finish_alias=finish_alias, recitation_cadence=recitation_cadence, reground_cadence=reground_cadence, dod_evaluator_factory=dod_evaluator_factory, host_verifier=host_verifier, host_verify_timeout_s=host_verify_timeout_s, host_verifier_verdict_hook=host_verifier_verdict_hook, host_verify_authoritative=host_verify_authoritative, verifier_judge=verifier_judge, verifier_judge_timeout_s=verifier_judge_timeout_s, finish_sealability_probe=finish_sealability_probe, finish_seal_timeout_s=finish_seal_timeout_s, workflow_run=workflow_run, terminal_commit_hook=terminal_commit_hook, control_fence=control_fence, quiet=quiet, strict_appkit_active=strict_appkit_active)
        # fmt: on

    async def _emit(self, event: Event) -> Event:
        if (
            isinstance(event, StatusEvent)
            and event.status is ConversationStatus.FINISHED
        ):
            return await self._transition._land_terminal_success(event)
        return await self._runtime._emit(event)

    async def get_state(self) -> ConversationState:
        return await self._runtime.get_state()

    async def _gate_planning_mode(self, step: AgentStep, events: list[Event]) -> Disp:
        return await self._planning_gates._gate_planning_mode(step, events)

    async def _gate_out_of_phase_submit_plan(self, step: AgentStep, events: list[Event]) -> Disp:
        return await self._planning_gates._gate_out_of_phase_submit_plan(step, events)

    async def _gate_stuck_escape_tool_quarantine(
        self, step: AgentStep, events: list[Event]
    ) -> Disp:
        return await self._planning_gates._gate_stuck_escape_tool_quarantine(step, events)

    async def _gate_hard_deny(self, action: ActionEvent) -> Disp:
        return await self._planning_gates._gate_hard_deny(action)

    async def _gate_risk_confirm(self, action: ActionEvent) -> tuple[Disp, ActionEvent]:
        return await self._planning_gates._gate_risk_confirm(action)

    async def run(self) -> ConversationState:
        self._hs03_reground_post_resume_emitted = False
        self._hs03_reground_last_action_count = -1
        return await self._transition.run()

    async def _run_drive(self) -> ConversationState:
        return await self._transition._run_drive()

    async def send_message(self, text: str, *, steer: bool = False) -> ConversationState:
        return await self._conversation_controls.send_message(text, steer=steer)

    async def steer(self, text: str) -> ConversationState:
        return await self._conversation_controls.steer(text)

    async def confirm(self) -> ConversationState:
        return await self._conversation_controls.confirm()

    async def reject(self, reason: str = "rejected by user") -> ConversationState:
        return await self._conversation_controls.reject(reason)

    async def _plan_approval_status(self, plan: PlanEvent, events: list[Event]) -> StatusEvent:
        return await self._plan_controls._plan_approval_status(plan, events)

    async def approve_plan(self) -> ConversationState:
        return await self._plan_controls.approve_plan()

    async def _seed_context_from_plan(self) -> None:
        return await self._plan_controls._seed_context_from_plan()

    async def pick_alternative(self, option_id: str) -> ConversationState:
        return await self._plan_controls.pick_alternative(option_id)

    async def enter_planning(self, text: str = "") -> ConversationState:
        return await self._replanning.enter_planning(text)

    async def _maybe_reenter_planning_for_followup(self, events: list[Event]) -> bool:
        return await self._replanning._maybe_reenter_planning_for_followup(events)

    async def _enter_revision_planning(self, text: str) -> None:
        return await self._replanning._enter_revision_planning(text)

    async def _gate_midstep_steer_replan(self, step: AgentStep) -> Disp:
        return await self._replanning._gate_midstep_steer_replan(step)

    async def pause(self) -> ConversationState:
        return await self._conversation_controls.pause()

    async def resume(self) -> ConversationState:
        return await self._conversation_controls.resume()

    async def cancel(self) -> ConversationState:
        return await self._conversation_controls.cancel()
