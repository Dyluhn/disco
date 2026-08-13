"""AgentLoop construction split into bounded, explicitly typed phases."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal

from ..dod_evaluator import DoDEvaluator
from ..events import VerifierVerdictEvent
from ..llm import LLMRouter, ModelExecutionPolicy, OperatingMode
from ..store.base import EventStore
from ..view import Condenser, Summarizer
from ..workflow import WorkflowRun
from . import view_render
from .boundaries import (
    Agent,
    ConfirmationPolicy,
    HostVerifier,
    SealabilityProbe,
    SecurityAnalyzer,
    StopHook,
    ToolExecutor,
    VerifierJudge,
)
from .conversation_controls import ConversationControls as _ConversationControls
from .driver import Driver
from .engine_contracts import (
    _DEFAULT_MODEL_POLICY,
    _DEFAULT_VETO_FEEDBACK,
    _FANOUT_MAX_PER_RUN,
    _HS03_REGROUND_INTERVAL,
    _RECITATION_CADENCE_DEFAULT,
    ControlFenceFactory,
    TerminalCommitHook,
    _new_retry_interrupt,
)
from .finish import FinishGate, host_verify_authoritative_enabled
from .loop_runtime import LoopRuntime as _LoopRuntime
from .observe import Observer
from .plan_conditions import PlanStepConditions
from .plan_controls import PlanApprovalController as _PlanApprovalController
from .planning_gates import PlanningGateController as _PlanningGateController
from .plans import Planner
from .recitation import RecitationRegrounder
from .replanning import ReplanningController as _ReplanningController
from .stuck import StuckDetector, StuckThresholds
from .transition import TransitionCoordinator as _TransitionCoordinator
from .turn_control import MetaToolHandlers, Valve

if TYPE_CHECKING:
    from .loop_facade_compat import _AgentLoopCompatibility as AgentLoop


def _initialize_core(
    loop: AgentLoop,
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
    max_iterations: int,
    stop_hooks: list[StopHook] | None,
    stuck_thresholds: StuckThresholds | None,
    planning_tools: frozenset[str],
    plan_tool: str,
    execution_mode: OperatingMode,
    autonomous: bool,
    model_policy: ModelExecutionPolicy,
    driver_context_window: int | None,
    finish_alias: str | None,
    workflow_run: WorkflowRun | None,
    terminal_commit_hook: TerminalCommitHook,
    control_fence: ControlFenceFactory,
    quiet: bool,
    strict_appkit_active: Callable[[], bool] | None,
    declared_delivery_kind: Literal["app", "files"] | None,
    delivery_contract_resolver: Callable[[str], Awaitable[None]] | None,
) -> None:
    # Autonomous mode (issue A): no human is available to answer questions or
    # approve plans (headless / unattended runs). Default False = today's
    # interactive behavior, fully unchanged. When True: ask_user/clarify are
    # withheld from the tool schema, the plan is auto-approved inline, and the
    # circuit-breaker's "hand off to the user" becomes a clean forfeit (STUCK)
    # instead of an indefinite AWAITING_USER_DECISION stall.
    loop._autonomous = autonomous
    loop._quiet = quiet
    # H301 — strict AppKit has a narrower immutable-DoD contract than an
    # ordinary/custom Build. The runtime callback reads the shared live AppKit
    # phase, so a confirmed CUSTOM_BUILD widening restores ordinary predicates
    # without reconstructing the loop. A configured callback fails closed.
    loop._strict_appkit_active_reader = strict_appkit_active
    loop._declared_delivery_kind = declared_delivery_kind
    loop._delivery_contract_resolver = delivery_contract_resolver
    loop._finish_alias = finish_alias
    loop._workflow_run, loop._terminal_commit_hook = workflow_run, terminal_commit_hook
    loop._control_fence = control_fence
    # Execution policy (T1): the SINGLE source of truth for model-tier execution.
    # `_assist` is a read-only property delegating to `_model_policy.assist`.
    loop._model_policy = model_policy
    loop._driver_context_window_value = (
        driver_context_window
        if isinstance(driver_context_window, int) and driver_context_window > 0
        else None
    )
    loop.conversation_id = conversation_id
    loop.store = store
    loop.agent = agent
    loop.executor = executor
    loop._router = router  # held for wiring symmetry; the Agent wraps it
    loop.analyzer = analyzer
    loop.policy = policy
    loop.condenser = condenser
    loop.summarizer = summarizer
    loop.mode = mode
    loop.max_iterations = max_iterations
    # Plan-mode wiring (Build). Defaults are inert: with no planning_tools the
    # mode filter and the plan intercept never fire, so Research and existing
    # tests behave exactly as before.
    loop._planning_tools = planning_tools  # tools visible ONLY while planning
    loop._plan_tool = plan_tool  # the structured-plan signal, intercepted
    loop._execution_mode = execution_mode  # the mode an approved plan runs in
    loop._plan_nudges = 0  # consecutive nudges while planning (safety cap)
    # Forced-submit recovery for revision re-plans (default ON; kill switch).
    loop._revision_force_submit_enabled = os.environ.get(
        "DISCO_REVISION_FORCE_SUBMIT", "1"
    ).strip().lower() not in ("0", "false", "no", "off")
    loop._plan_explore_reads = 0  # (B2/B6) consecutive PLANNING reads w/o a plan
    loop._execution_nudges = 0  # consecutive "you must act" nudges in execution
    loop._browser_verify_refusals = 0  # consecutive browser-verification refusals
    loop._identical_plan_revisions = 0  # C8 (T11): consecutive identical-steps
    # propose_plan_update auto-approvals in
    # autonomous mode. Increments when the
    # new plan's steps match the immediately
    # prior plan's; resets on a different
    # (incl. appended) plan. The cap lives
    # at module-level so tests can pin it.
    loop._finish_verify_refusals = 0  # compatibility telemetry; finish checks are advisory
    loop._finish_verify_strips = 0  # compatibility telemetry for malformed checks
    loop._finish_seal_refusals = 0  # REL-27 sealability refusals (cap-3 loud release)
    loop._workflow_output_contract_refusals = 0
    # C20 — `delegate_explore` count, per run segment. Reset in run() so a
    # resume/steer gets a fresh budget (mirror `_finish_verify_refusals`).
    # The cap is module-level so tests can pin it. The cap bounds the
    # fan-out — a model cannot recurse (the helper is read-only and the
    # call site is always the loop's intercept path).
    loop._fanout_count = 0
    loop._fanout_max = _FANOUT_MAX_PER_RUN
    # Actionless-step breaker cap (DEFECT-4)
    loop._ACTIONLESS_BREAK_CAP = 3
    # Steps consumed with NOTHING persisted to the log (empty-args serve,
    # blank remember fact, empty-thought noop, duplicate deliverable) —
    # invisible to every event-derived detector incl. the stuck detector,
    # so an instance counter is the only honest way to see the spin.
    # Reset when a real ActionEvent is built (site h) and at run() entry.
    loop._invisible_steps = 0
    # GAP B backstop: max tool-less prose turns in a row before the loop ends
    # the run (a talking-without-acting model can't advance max_iterations).
    loop._max_consecutive_noops = 6
    # Circuit breaker (Cluster 2): after this many consecutive failures the
    # harness hands off to the user (AWAITING_USER_DECISION) instead of
    # grinding. Set above StuckDetector's identical-repeat threshold (3) so
    # identical loops still STUCK first; this catches the DISTINCT-failure case.
    loop._circuit_breaker_threshold = 4
    # Auto-continue cap: when the agent declares finished but the plan
    # isn't done, the loop re-kicks itself this many times before landing
    # FINISHED with partial-plan detail. The user shouldn't have to poke
    # the model to keep going; this is the harness driving the loop forward.
    loop._auto_continue_cap = 3
    loop._stop_hooks = list(stop_hooks or [])
    loop._stuck = StuckDetector(stuck_thresholds, assist=model_policy.assist)


def _initialize_legacy_collaborators(loop: AgentLoop, veto_feedback: str) -> None:
    # C6/HS-03/C5 collaborator: recitation cadence, scheduled re-grounding,
    # and the MEMORY write-through mirror. Reads the loop's cadence counters.
    loop._recit = RecitationRegrounder(loop)
    # View materialization collaborator (microcompact → condense → recap
    # gate → F8 → snapshot append). Reads condenser/summarizer + self._recit.
    loop._view = view_render.ViewBuilder(loop)
    # C18 advisory plan-step done-condition collaborator.
    loop._plan_cond = PlanStepConditions(loop)
    # Execute-and-observe (§4.1) + §8 hard-reset + C20 fan-out collaborator.
    loop._observe = Observer(loop)
    # The model-driving step collaborator (tool visibility + agent.step +
    # requery ladder + transient backoff + context-window hard-reset).
    loop._driver = Driver(loop)
    # The finish pipeline collaborator (verify-on-finish, C1c DoD gate,
    # execution nudge, browser-verify gate, auto-continue finalizer).
    loop._finish = FinishGate(loop)
    # Turn-taking control collaborators (co-located: both poke the shared
    # _invisible_steps counter). Valve = spin/stuck/circuit-breaker guards;
    # MetaToolHandlers = the virtual meta-tool run-loop intercepts.
    loop._valve = Valve(loop)
    loop._meta = MetaToolHandlers(loop)
    # Plan / alternatives builders (submit_plan → PlanEvent + C18 harvest;
    # ask_user options → AlternativesEvent).
    loop._planner = Planner(loop)
    loop._veto_feedback = veto_feedback


def _initialize_verification_and_control(
    loop: AgentLoop,
    *,
    dod_evaluator_factory: (
        Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None
    ),
    host_verifier: HostVerifier | None,
    host_verify_timeout_s: float,
    host_verifier_verdict_hook: Callable[[VerifierVerdictEvent], Awaitable[None]] | None,
    host_verify_authoritative: bool | None,
    verifier_judge: VerifierJudge | None,
    verifier_judge_timeout_s: float,
    finish_sealability_probe: SealabilityProbe | None,
    finish_seal_timeout_s: float,
) -> None:
    # C1c — DoD evaluator gate (wires the C1b fresh-context judge into the
    # finish branch). The factory returns a fully-configured evaluator; the
    # default builds one over the executor's sandbox workspace_root. Tests
    # inject a fake-seamed evaluator via the same hook. None means "use the
    # default factory" (the evaluator is still wired when a DoD spec exists).
    # When no DoD spec exists for the conversation, the gate is a no-op
    # (legacy byte-identical path) — see _finish_dod_gate_passed.
    loop._dod_evaluator_factory = dod_evaluator_factory
    loop._host_verifier = host_verifier
    loop._host_verify_timeout_s = float(host_verify_timeout_s)
    loop._host_verifier_verdict_hook = host_verifier_verdict_hook
    loop._host_verify_authoritative = (
        host_verify_authoritative_enabled()
        if host_verify_authoritative is None
        else bool(host_verify_authoritative)
    )
    loop._verifier_judge = verifier_judge
    loop._verifier_judge_timeout_s = float(verifier_judge_timeout_s)
    loop._finish_sealability_probe = finish_sealability_probe  # REL-27
    loop._finish_seal_timeout_s = float(finish_seal_timeout_s)
    # Consecutive DoD-refusal streak (telemetry; the gate has no cap — the
    # loop's max_iterations + the user's kill switch are the ultimate exit,
    # same as the browser-verify and execution-nudge gates).
    loop._dod_refusals = 0
    # REL-RC-O — consecutive dictated-content finish refusals. The actual
    # literals are event-derived; this counter only bounds refuse/continue.
    loop._dictated_content_refusals = 0
    loop._lock = asyncio.Lock()
    # WALK-18 — cooperative pause flag. pause() SETS it WITHOUT taking
    # self._lock (so a pause lands while the in-flight turn holds the lock,
    # unlike cancel() which must take the lock to emit IDLE); the run() loop
    # observes it at its next step boundary, emits PAUSED, and returns.
    loop._pause_requested = asyncio.Event()
    loop._retry_interrupt = _new_retry_interrupt()
    # Watch-it-write sink (optional). The runtime wires this to the store's
    # ephemeral broadcast; when set, the driver's streamed tool-call arg
    # fragments are decoded into growing file-content frames and published
    # live (display-only, never persisted). None → no streaming (tests, CLI).
    loop.stream_sink = None


def _initialize_cadence(
    loop: AgentLoop,
    *,
    recitation_cadence: int,
    reground_cadence: int,
) -> None:
    # C6 — recitation cadence state. Per-run counters / fingerprints
    # used to decide whether the view.py tail-recap is appended on this
    # iteration. Reset in run() at the start of every run segment so a
    # resume/steer starts a fresh cadence. The signature is a stable
    # hash of (plan + per-step checklist) — DRIFT fires when the plan
    # is replaced OR the agent marks a step done/active.
    loop._recitation_cadence = max(1, int(recitation_cadence))
    loop._recitation_step_count = 0
    loop._recitation_last_signature = None
    # HS-03 — scheduled facts re-grounding cadence. Mirrors the
    # C6 seam (testable, defaulted to the production constant).
    # The instance attribute is what the predicate inspects so a
    # test can pin a small cadence without monkey-patching the
    # module constant. Clamped to >= 1 (a cadence of 0 would
    # mean "every step" modulo zero, undefined; 1 is the
    # degenerate "fire on every step" case which the brief
    # explicitly disclaims as not the intent).
    loop._hs03_reground_cadence = max(1, int(reground_cadence))
    # C18 — per-(plan_revision, step_index) advisory done-condition
    # predicates, populated in `_plan_from_args` from the optional
    # `done_condition` field on each `PlanStepInput`. The map is keyed
    # by (plan_revision, 1-based step index) so a re-plan (new
    # revision) cleanly supersedes the prior predicate set without a
    # stale match. Lookup happens in
    # `_maybe_emit_plan_step_done_condition_note` whenever a
    # `plan_step(idx, 'done')` ActionEvent is observed. Empty by
    # default — steps without a predicate behave exactly as today
    # (back-compat). Cleared on `approve_plan` reset (fresh segment).
    loop._plan_step_predicates = {}
    # F4 — gated bootstrap observation. Fires at most ONCE per
    # conversation when assist is ON and we're about to make the first
    # model call. The flag persists across run() segments so a resume
    # never re-emits it (the model already saw it; re-firing would
    # be a redundant system reminder and a context-window penalty).
    # Assist OFF (default) leaves this flag dead — the gate is closed
    # before the detector is ever called.
    loop._bootstrap_emitted = False
    # HS-03 — scheduled facts re-grounding. Two pieces of state,
    # both assist-gated. Both are reset in run() so a fresh
    # run segment (which is what a resume/restart starts) gets a
    # fresh budget — the brief asks for "once right after a
    # resume" and that "once" is per-resume, not per-conversation.
    #   * _hs03_reground_post_resume_emitted: ONE-SHOT guard for
    #     the post-resume recap. Mirrors _bootstrap_emitted's
    #     shape (a boolean, flipped after the first eligible
    #     step), but RESET in run() (where _bootstrap_emitted is
    #     NOT reset) — a resume IS the moment we want to re-fire.
    #   * _hs03_reground_last_action_count: PER-BOUNDARY guard
    #     keyed off the action count. A boundary is
    #     `actions_since_last_resume % N == 0` (N ==
    #     _HS03_REGROUND_INTERVAL). The guard compares the
    #     CURRENT count to the last one and skips emit when
    #     equal — so two consecutive steps at the same boundary
    #     (e.g. count 12 on two steps in a row, which can happen
    #     if a step is a no-op) never produce two recap messages.
    #     Reset in run() so a fresh segment starts the count
    #     from -1 (no boundary yet).
    loop._hs03_reground_post_resume_emitted = False
    loop._hs03_reground_last_action_count = -1


def _initialize_extracted_collaborators(loop: AgentLoop) -> None:
    loop._runtime = _LoopRuntime(loop)
    loop._planning_gates = _PlanningGateController(loop)
    loop._transition = _TransitionCoordinator(loop)
    loop._conversation_controls = _ConversationControls(loop)
    loop._plan_controls = _PlanApprovalController(loop)
    loop._replanning = _ReplanningController(loop)


# fmt: off
def initialize_agent_loop(loop: AgentLoop, conversation_id: str, store: EventStore, agent: Agent, executor: ToolExecutor, router: LLMRouter, analyzer: SecurityAnalyzer, policy: ConfirmationPolicy, condenser: Condenser, summarizer: Summarizer, *, mode: OperatingMode, max_iterations: int=500, stop_hooks: list[StopHook] | None=None, stuck_thresholds: StuckThresholds | None=None, veto_feedback: str=_DEFAULT_VETO_FEEDBACK, planning_tools: frozenset[str]=frozenset(), plan_tool: str='submit_plan', execution_mode: OperatingMode=OperatingMode.LONG_HORIZON, autonomous: bool=False, model_policy: ModelExecutionPolicy=_DEFAULT_MODEL_POLICY, driver_context_window: int | None=None, finish_alias: str | None=None, recitation_cadence: int=_RECITATION_CADENCE_DEFAULT, reground_cadence: int=_HS03_REGROUND_INTERVAL, dod_evaluator_factory: Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None=None, host_verifier: HostVerifier | None=None, host_verify_timeout_s: float=30.0, host_verifier_verdict_hook: Callable[[VerifierVerdictEvent], Awaitable[None]] | None=None, host_verify_authoritative: bool | None=None, verifier_judge: VerifierJudge | None=None, verifier_judge_timeout_s: float=30.0, finish_sealability_probe: SealabilityProbe | None=None, finish_seal_timeout_s: float=150.0, workflow_run: WorkflowRun | None=None, terminal_commit_hook: TerminalCommitHook=None, control_fence: ControlFenceFactory=None, quiet: bool=False, strict_appkit_active: Callable[[], bool] | None=None, declared_delivery_kind: Literal["app", "files"] | None=None, delivery_contract_resolver: Callable[[str], Awaitable[None]] | None=None) -> None:
    _initialize_core(loop, conversation_id, store, agent, executor, router, analyzer, policy, condenser, summarizer, mode=mode, max_iterations=max_iterations, stop_hooks=stop_hooks, stuck_thresholds=stuck_thresholds, planning_tools=planning_tools, plan_tool=plan_tool, execution_mode=execution_mode, autonomous=autonomous, model_policy=model_policy, driver_context_window=driver_context_window, finish_alias=finish_alias, workflow_run=workflow_run, terminal_commit_hook=terminal_commit_hook, control_fence=control_fence, quiet=quiet, strict_appkit_active=strict_appkit_active, declared_delivery_kind=declared_delivery_kind, delivery_contract_resolver=delivery_contract_resolver)
    _initialize_legacy_collaborators(loop, veto_feedback)
    _initialize_verification_and_control(loop, dod_evaluator_factory=dod_evaluator_factory, host_verifier=host_verifier, host_verify_timeout_s=host_verify_timeout_s, host_verifier_verdict_hook=host_verifier_verdict_hook, host_verify_authoritative=host_verify_authoritative, verifier_judge=verifier_judge, verifier_judge_timeout_s=verifier_judge_timeout_s, finish_sealability_probe=finish_sealability_probe, finish_seal_timeout_s=finish_seal_timeout_s)
    _initialize_cadence(loop, recitation_cadence=recitation_cadence, reground_cadence=reground_cadence)
    _initialize_extracted_collaborators(loop)
# fmt: on
