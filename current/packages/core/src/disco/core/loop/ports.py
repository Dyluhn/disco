"""Typed ports over the agent loop, derived from the measured coupling inventory.

Each Protocol is one capability cluster from
``deep-map/18-COUPLING-INVENTORY.md`` §4. A consumer annotates the port(s) it
actually uses instead of the whole loop, so what it can reach is bounded by its
declaration rather than by what the object happens to carry.

Membership is derived from the measured attribute-access matrix, not invented:
`PROTOCOL-WIDTH-DECISION-2026-07-31.md` forbids a cluster wider than its
consumers touch, because that recreates the bag under a new name. Measured mean
is 3.95 of 8 ports per product consumer.

These are structural (PEP 544) declarations only. They add no behaviour, own no
state, and are never instantiated or ``isinstance``-checked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from . import view_render
    from .boundaries import Agent, ConfirmationPolicy, SecurityAnalyzer, StreamHook, ToolExecutor
    from .engine_contracts import (
        ActionEvent,
        AgentStep,
        AlternativesEvent,
        Condenser,
        ControlFenceFactory,
        ConversationState,
        ConversationStatus,
        Disp,
        DoDEvaluator,
        DoDPredicate,
        Driver,
        Event,
        EventStore,
        FinishGate,
        HostVerifier,
        LLMMessage,
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


class LoopEventPort(Protocol):
    """Append to the event log, read it back, and project state from it."""

    async def _emit(self, event: Event) -> Event:
        ...

    async def _events(self) -> list[Event]:
        ...

    store: EventStore

    async def get_state(self) -> ConversationState:
        ...

    async def _event_by_id(self, event_id: str) -> Event | None:
        ...

    async def _materialize_view(self, events: list[Event]) -> View:
        ...

    async def _materialize_current_view(self) -> tuple[View, list[Event]]:
        ...

    async def _assert_current_agent_view(self) -> None:
        ...

    @staticmethod
    def _current_agent_view_id() -> str | None:
        ...

    def _recent(self, events: list[Event]) -> list[Event]:
        ...


class ToolExecutionPort(Protocol):
    """Run a tool, observe the result, and apply execution valves."""

    executor: ToolExecutor

    async def _execute_and_observe(self, action: ActionEvent) -> None:
        ...

    async def _prepare_executor(self, events: list[Event] | None=None) -> None:
        ...

    def _readonly_tool_names(self) -> frozenset[str] | None:
        ...

    def _tools_for_step(self, *, suppress_meta_tools: bool=False) -> list:
        ...

    async def _run_fanout(self, args: dict, events: list[Event], *, call_id: str='') -> ToolResult:
        ...

    _fanout_count: int

    _fanout_max: int

    _valve: Valve

    async def _post_noop_valve(self) -> Disp:
        ...

    async def _maybe_apply_read_churn_valve(self, action: ActionEvent) -> Disp:
        ...

    async def _maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        ...

    @property
    def _STREAMING_WRITE_TOOLS(self) -> tuple[str, ...]: ...

    @property
    def _STREAM_FLUSH_CHARS(self) -> int: ...


class ConversationModePort(Protocol):
    """What mode the conversation is in, and who it is for."""

    mode: OperatingMode

    _autonomous: bool

    @property
    def _assist(self) -> bool:
        ...

    _quiet: bool

    _execution_mode: OperatingMode

    def _effective_mode(self, events: list[Event]) -> OperatingMode:
        ...

    def _reconcile_mode_from_events(self, events: list[Event]) -> OperatingMode:
        ...

    agent: Agent

    conversation_id: str


class PlanLifecyclePort(Protocol):
    """Plan creation, approval, revision and replanning."""

    _planner: Planner

    async def _plan_approval_status(self, plan: PlanEvent, events: list[Event]) -> StatusEvent:
        ...

    async def _seed_context_from_plan(self) -> None:
        ...

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        ...

    def _alternatives_from_args(
        self,
        arguments: dict,
        events: list[Event],
    ) -> AlternativesEvent | None:
        ...

    _plan_step_predicates: dict[tuple[int, int], DoDPredicate]

    _plan_tool: str

    _planning_tools: frozenset[str]

    _plan_cond: PlanStepConditions

    async def _route_plan_approval_gate(self, plan: PlanEvent) -> Disp:
        ...

    def _harvest_prose_plan(self, events: list[Event]) -> PlanEvent | None:
        ...

    async def _gate_planning_mode(self, step: AgentStep, events: list[Event]) -> Disp:
        ...

    async def _gate_out_of_phase_submit_plan(self, step: AgentStep, events: list[Event]) -> Disp:
        ...

    async def _gate_stuck_escape_tool_quarantine(
        self,
        step: AgentStep,
        events: list[Event],
    ) -> Disp:
        ...

    async def _enter_revision_planning(self, text: str) -> None:
        ...

    async def _maybe_reenter_planning_for_followup(self, events: list[Event]) -> bool:
        ...

    async def _gate_midstep_steer_replan(self, step: AgentStep) -> Disp:
        ...

    @staticmethod
    def _plan_verification_predicates(plan: PlanEvent | None) -> list[DoDPredicate]:
        ...

    def _workflow_router_phase_active(self) -> bool:
        ...

    _revision_force_submit_enabled: bool

    _strict_appkit_active_reader: Callable[[], bool] | None

    _workflow_run: WorkflowRun | None

    async def approve_plan(self) -> ConversationState:
        ...

    async def enter_planning(self, text: str='') -> ConversationState:
        ...

    async def pick_alternative(self, option_id: str) -> ConversationState:
        ...


class GateCounterPort(Protocol):
    """Refusal/nudge counters and the caps that bound them."""

    _plan_nudges: int

    _plan_explore_reads: int

    _execution_nudges: int

    _require_productive_action_before_finish: bool

    _invisible_steps: int

    _dod_refusals: int

    _dictated_content_refusals: int

    _finish_verify_refusals: int

    _finish_verify_strips: int

    _finish_seal_refusals: int

    _identical_plan_revisions: int

    _browser_verify_refusals: int

    _workflow_output_contract_refusals: int

    _ACTIONLESS_BREAK_CAP: int

    _max_consecutive_noops: int

    _circuit_breaker_threshold: int

    _auto_continue_cap: int


class FinishVerificationPort(Protocol):
    """Finish admission and the verification evidence behind it."""

    _finish: FinishGate

    _finish_alias: str | None

    async def _finish_dod_gate_passed(self) -> bool:
        ...

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
        ...

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


class ContextGroundingPort(Protocol):
    """What the model is shown: view, condensation, recitation, regrounding."""

    _view: view_render.ViewBuilder

    condenser: Condenser

    summarizer: Summarizer

    _driver: Driver

    def _driver_context_window(self) -> int | None:
        ...

    _driver_context_window_value: int | None

    def _f8_shrink_file_write_args(
        self,
        messages: list[LLMMessage],
        events: list[Event],
    ) -> list[LLMMessage]:
        ...

    async def _workspace_snapshot_message(self, events: list[Event]) -> LLMMessage | None:
        ...

    _recit: RecitationRegrounder

    _recitation_cadence: int

    _recitation_step_count: int

    _recitation_last_signature: str | None

    def _recitation_signature(self, events: list[Event]) -> str | None:
        ...

    def _should_emit_recitation(self, events: list[Event]) -> bool:
        ...

    def _gate_recitation(
        self,
        view: View,
        events: list[Event],
        *,
        context_pack_active: bool=False,
    ) -> View:
        ...

    _hs03_reground_cadence: int

    _hs03_reground_post_resume_emitted: bool

    _hs03_reground_last_action_count: int

    def _should_emit_reground(self, events: list[Event]) -> bool:
        ...

    async def _maybe_emit_reground(self, events: list[Event]) -> list[Event]:
        ...

    @property
    def _MEMORY_PATH(self) -> str: ...

    @property
    def _LEGACY_MEMORY_PATH(self) -> str: ...

    async def _write_pmx_memory_fact(self, scope: str, fact: str) -> None:
        ...

    async def _hard_reset(self, events: list[Event]) -> bool:
        ...

    def _build_stream_hook(self) -> StreamHook | None:
        ...

    stream_sink: Callable[[dict], None] | None


class TurnControlPort(Protocol):
    """Turn admission, pause/resume/cancel, locking and stop hooks."""

    _lock: asyncio.Lock

    _pause_requested: asyncio.Event

    _retry_interrupt: asyncio.Event

    _control_fence: ControlFenceFactory

    _declared_delivery_kind: Literal["app", "files"] | None

    _delivery_contract_resolver: Callable[[str], Awaitable[None]] | None

    _stop_hooks: list[StopHook]

    _stuck: StuckDetector

    _terminal_commit_hook: TerminalCommitHook

    _observe: Observer

    async def _land_blocked(
        self,
        *,
        reason: str,
        guidance: str='',
        legacy_status: ConversationStatus=ConversationStatus.STUCK,
        legacy_detail: str | None=None,
        extra_meta: dict[str, str | int] | None=None,
    ) -> None:
        ...

    async def _maybe_synthesize_finish_after_actionless_pauses(
        self,
        state: ConversationState,
        events: list[Event],
    ) -> Disp:
        ...

    async def _gate_hard_deny(self, action: ActionEvent) -> Disp:
        ...

    async def _gate_risk_confirm(self, action: ActionEvent) -> tuple[Disp, ActionEvent]:
        ...

    _bootstrap_emitted: bool

    max_iterations: int

    async def run(self) -> ConversationState:
        ...

    async def _run_drive(self) -> ConversationState:
        ...

    async def send_message(self, text: str, *, steer: bool=False) -> ConversationState:
        ...

    async def steer(self, text: str) -> ConversationState:
        ...

    async def confirm(self) -> ConversationState:
        ...

    async def reject(self, reason: str='rejected by user') -> ConversationState:
        ...

    async def pause(self) -> ConversationState:
        ...

    async def resume(self) -> ConversationState:
        ...

    async def cancel(self) -> ConversationState:
        ...

    @staticmethod
    def _has_unprocessed_user_message(events: list[Event]) -> bool: ...

    analyzer: SecurityAnalyzer

    policy: ConfirmationPolicy
