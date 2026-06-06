"""The agent loop — agent-loop-contract.md §2, §4, §5, §7, §8.

A bounded iteration over `Agent.step()`. The loop's correctness *is* the
product's reliability (BoD §1.1). It is transport-agnostic and drivable headless:
it produces events and consumes control operations, nothing more.

Key invariants (principles §1):
- One action per iteration, observed before the next.
- Status is reconstructed from the log, never held privately.
- A per-conversation FIFO lock guards every status read/mutation; stepping holds
  it EXCEPT during tool execution, so pause/steer/confirm land between steps and
  concurrent user input is never dropped.
- `max_iterations` is the ultimate backstop beneath stuck detection.
"""

from __future__ import annotations

import asyncio

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from ..llm import (
    Difficulty,
    LLMContextWindowExceeded,
    LLMError,
    LLMRouter,
    OperatingMode,
    OverflowSignal,
)
from ..state import ConversationState
from ..store.base import EventStore
from ..view import Condenser, Summarizer, View
from .boundaries import Agent, ConfirmationPolicy, SecurityAnalyzer, StopHook, ToolExecutor
from .stuck import StuckDetector, StuckThresholds

_DEFAULT_VETO_FEEDBACK = "The goal does not appear complete yet. Continue working toward it."


def _describe_llm_error(e: LLMError) -> str:
    """Render a model/provider error for the user WITHOUT flattening its reason.

    Reactive error surfacing: when the assigned model rejects the input or the
    provider fails, the user must see the ACTUAL provider message — not a generic
    "model call failed". We keep the typed classification (the exception class the
    adapter mapped to) AND the real reason (its message), plus provider/model
    context when the typed error carries it."""
    reason = str(e) or "(provider returned no message)"
    loc = " / ".join(p for p in (getattr(e, "provider", ""), getattr(e, "model", "")) if p)
    head = f"{type(e).__name__} [{loc}]" if loc else type(e).__name__
    return f"{head}: {reason}"

# Statuses at which the loop yields control back to the caller at a checkpoint.
_TERMINAL_FOR_NOW = frozenset(
    {
        ConversationStatus.PAUSED,
        ConversationStatus.IDLE,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
    }
)

# Injected when the planner answers in prose / tries to act instead of calling the
# plan tool: in PLANNING mode the only valid move is to PROPOSE a structured plan.
_PLAN_NUDGE = (
    "Do not take any action yet. First propose a plan by calling the `submit_plan` "
    "tool with a short summary and a list of concrete, ordered steps."
)
# Safety cap on consecutive plan nudges, so a misbehaving planner can't spin (no
# ActionEvent is emitted while nudging → max_iterations doesn't trip on its own).
_MAX_PLAN_NUDGES = 3


class AgentLoop:
    """[CONTRACT] The orchestrator. See module docstring + §2 state machine."""

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
    ) -> None:
        self.conversation_id = conversation_id
        self.store = store
        self.agent = agent
        self.executor = executor
        self._router = router  # held for wiring symmetry; the Agent wraps it
        self.analyzer = analyzer
        self.policy = policy
        self.condenser = condenser
        self.summarizer = summarizer
        self.mode = mode
        self.max_iterations = max_iterations
        # Plan-mode wiring (Build). Defaults are inert: with no planning_tools the
        # mode filter and the plan intercept never fire, so Research and existing
        # tests behave exactly as before.
        self._planning_tools = planning_tools  # tools visible ONLY while planning
        self._plan_tool = plan_tool  # the structured-plan signal, intercepted
        self._execution_mode = execution_mode  # the mode an approved plan runs in
        self._plan_nudges = 0  # consecutive nudges while planning (safety cap)
        self._stop_hooks = list(stop_hooks or [])
        self._stuck = StuckDetector(stuck_thresholds)
        self._veto_feedback = veto_feedback
        self._lock = asyncio.Lock()

    # ---- emission + small helpers -------------------------------------------

    async def _emit(self, event: Event) -> Event:
        return await self.store.append(self.conversation_id, event)

    async def _events(self) -> list[Event]:
        return await self.store.get_events(self.conversation_id)

    async def get_state(self) -> ConversationState:
        return await self.store.get_state(self.conversation_id)

    async def _event_by_id(self, event_id: str) -> Event | None:
        for e in await self._events():
            if e.id == event_id:
                return e
        return None

    def _recent(self, events: list[Event]) -> list[Event]:
        return events[-self._stuck.t.scan_window :]

    def _overflow_signal(self, events: list[Event]) -> OverflowSignal:
        """Derive the router's overflow inputs from the log: trailing tool errors
        feed router rule 4 (stuck recovery) so a struggling step can auto-escalate
        to overflow *before* STUCK is declared (§6 note)."""
        consecutive = 0
        for e in reversed(events):
            if isinstance(e, AgentErrorEvent):
                consecutive += 1
            elif isinstance(e, ActionEvent | ObservationEvent | MessageEvent):
                break
        return OverflowSignal(difficulty=Difficulty.ROUTINE, consecutive_tool_errors=consecutive)

    @staticmethod
    def _estimate_tokens(view: View) -> int:
        # [VERIFY] cheap heuristic (~4 chars/token); swap for a tokenizer later.
        return sum(len(m.content) for m in view.messages) // 4

    # ---- plan-mode helpers (Build) ------------------------------------------

    def _tools_for_step(self) -> list:
        """Mode-scoped tool visibility. With no planning_tools configured this is a
        pass-through (Research / default). While PLANNING the agent sees ONLY the
        planning tool(s); while executing it sees everything else."""
        tools = self.executor.available_tools()
        if not self._planning_tools:
            return tools
        if self.mode == OperatingMode.PLANNING:
            return [t for t in tools if getattr(t, "name", None) in self._planning_tools]
        return [t for t in tools if getattr(t, "name", None) not in self._planning_tools]

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Build a PlanEvent from a `submit_plan` tool call. Defensive against the
        model's shape drift (steps as dicts or bare strings); revision counts prior
        plans so a re-plan is visibly the next iteration."""
        steps: list[PlanStep] = []
        for s in arguments.get("steps") or []:
            if isinstance(s, dict):
                title = str(s.get("title") or s.get("step") or s.get("name") or "").strip()
                detail = s.get("detail") or s.get("description")
                if title:
                    steps.append(PlanStep(title=title, detail=str(detail) if detail else None))
            elif isinstance(s, str) and s.strip():
                steps.append(PlanStep(title=s.strip()))
        if not steps:
            steps = [PlanStep(title="(the planner returned no concrete steps)")]
        summary = str(arguments.get("summary") or "").strip() or "Proposed plan"
        revision = 1 + sum(1 for e in events if isinstance(e, PlanEvent))
        return PlanEvent(summary=summary, steps=steps, revision=revision)

    @staticmethod
    def initial_mode(
        events: list[Event],
        *,
        planning: OperatingMode = OperatingMode.PLANNING,
        execution: OperatingMode = OperatingMode.LONG_HORIZON,
        default: OperatingMode = OperatingMode.INTERACTIVE,
    ) -> OperatingMode:
        """Reconstruct the loop's operating mode from the log so a rebuilt loop
        (process restart) resumes in the right phase. Reads the last mode marker
        stamped on a StatusEvent.detail by approve_plan / enter_planning."""
        for e in reversed(events):
            if isinstance(e, StatusEvent) and e.detail == "plan_approved":
                return execution
            if isinstance(e, StatusEvent) and e.detail == "planning":
                return planning
        return default

    # ---- view materialization + condensation (§8) ---------------------------

    async def _materialize_view(self, events: list[Event]) -> View:
        view = View.of(events)
        req = self.condenser.should_condense(view, token_count=self._estimate_tokens(view))
        if req is not None:
            tombstone = await self.condenser.condense(events, view, summarizer=self.summarizer)
            if tombstone is not None:
                await self._emit(tombstone)
                view = View.of(await self._events())
            # Soft trigger with no tombstone this step: proceed uncondensed and
            # retry next iteration (§8). Non-fatal.
        return view

    async def _hard_reset(self, events: list[Event]) -> bool:
        """Forget-and-summarize after a context-window error (§8). Returns True
        if a tombstone was appended (progress made)."""
        tombstone = await self.condenser.condense(
            events, View.of(events), summarizer=self.summarizer
        )
        if tombstone is not None:
            await self._emit(tombstone)
            return True
        return False

    # ---- execute-and-observe (§4.1) -----------------------------------------

    async def _execute_and_observe(self, action: ActionEvent) -> None:
        """[CONTRACT] Every executed ActionEvent yields exactly one observation
        event (ObservationEvent on success, AgentErrorEvent on failure),
        correlated by action.id."""
        try:
            result = await self.executor.execute(action.tool_call)
        except LLMContextWindowExceeded:
            raise  # handled by view-materialization hard-reset (§8)
        except Exception as e:  # noqa: BLE001 — any tool/exec failure is an observation
            await self._emit(AgentErrorEvent(error=str(e), action_id=action.id))
            return
        if result.success:
            await self._emit(ObservationEvent(tool_result=result, action_id=action.id))
        else:
            await self._emit(
                AgentErrorEvent(error=result.error or "tool failed", action_id=action.id)
            )

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
        for hook in self._stop_hooks:
            if not await hook.allow_stop(state, events):
                return False
        return True

    # ---- the run loop (§4) --------------------------------------------------

    async def run(self) -> ConversationState:
        """Drive until a terminal-for-now status. Idempotent to call again after
        a pause/confirmation. [CONTRACT] returns the resulting ConversationState."""
        state = await self.get_state()
        if state.execution_status in _TERMINAL_FOR_NOW and state.execution_status != (
            ConversationStatus.IDLE
        ):
            # Paused/waiting/finished/stuck/error: a fresh run must be re-armed by
            # a control op (resume/confirm) or a new message. IDLE means "ready".
            if state.execution_status in (
                ConversationStatus.PAUSED,
                ConversationStatus.WAITING_FOR_CONFIRMATION,
                ConversationStatus.AWAITING_PLAN_APPROVAL,
            ):
                return state
            # FINISHED/STUCK/ERROR with no new work → nothing to do.
            if not self._has_unprocessed_user_message(await self._events()):
                return state
        await self._emit(StatusEvent(status=ConversationStatus.RUNNING))

        while True:
            action_to_execute: ActionEvent | None = None
            async with self._lock:
                events = await self._events()
                state = ConversationState.reconstruct(
                    self.conversation_id, events, max_iterations=self.max_iterations
                )
                status = state.execution_status

                # (a) honor control transitions decided between steps
                if status in (
                    ConversationStatus.PAUSED,
                    ConversationStatus.IDLE,
                    ConversationStatus.FINISHED,
                    ConversationStatus.STUCK,
                    ConversationStatus.ERROR,
                    ConversationStatus.WAITING_FOR_CONFIRMATION,
                    ConversationStatus.AWAITING_PLAN_APPROVAL,
                ):
                    return state

                # (b) iteration ceiling — the ultimate backstop (principle 4)
                if state.iteration >= self.max_iterations:
                    await self._emit(
                        ErrorEvent(
                            code="max_iterations",
                            detail=f"reached {self.max_iterations}",
                        )
                    )
                    return await self.get_state()

                # (c) stuck detection BEFORE more work (§6)
                if self._stuck.is_stuck(self._recent(events)):
                    await self._emit(StatusEvent(status=ConversationStatus.STUCK))
                    return await self.get_state()

                # (d) build the model-facing View, condensing if triggered (§8)
                view = await self._materialize_view(events)

                # (e) ask the agent for ONE action (principle 1). The visible tool
                # set is mode-scoped: while PLANNING the agent sees ONLY the plan
                # tool (so it can't act before approval); while executing it sees
                # everything except the plan tool.
                try:
                    step = await self.agent.step(
                        view,
                        self._tools_for_step(),
                        mode=self.mode,
                        overflow_signal=self._overflow_signal(events),
                    )
                except LLMContextWindowExceeded:
                    if await self._hard_reset(await self._events()):
                        continue  # retry the step on the condensed view
                    await self._emit(
                        ErrorEvent(code="context_window", detail="hard reset made no progress")
                    )
                    return await self.get_state()
                except LLMError as e:
                    # REACTIVE error surfacing: the driver model's call failed (the
                    # provider rejected the input, refused, auth/transient exhausted,
                    # or the assignment was bad). Do NOT swallow it or flatten it into
                    # a generic failure — surface the provider's real content to the
                    # UI via ErrorEvent.detail (the agent server streams every event
                    # to the client). This is conversation-fatal: the brain itself
                    # failed, so there is no observation to feed back. The typed
                    # classification (the exception class) is preserved in the detail.
                    await self._emit(ErrorEvent(code="model_error", detail=_describe_llm_error(e)))
                    return await self.get_state()

                # (e.5) PLAN GATE — in PLANNING mode the agent proposes, it never
                # acts. A `submit_plan` call is intercepted into a PlanEvent and the
                # loop halts for human approval; anything else (prose, a stray tool)
                # is nudged back to planning. The plan tool is NEVER executed.
                if self.mode == OperatingMode.PLANNING:
                    tc = step.tool_call
                    if tc is not None and tc.tool_name == self._plan_tool:
                        plan = self._plan_from_args(tc.arguments, events)
                        await self._emit(plan)
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                                detail=plan.id,
                            )
                        )
                        return await self.get_state()
                    # Safety: a misbehaving planner that keeps ignoring the nudge would
                    # spin (no ActionEvent → no iteration counter, no stuck pattern).
                    # Cap consecutive nudges and bail with a real error.
                    self._plan_nudges += 1
                    if self._plan_nudges >= _MAX_PLAN_NUDGES:
                        await self._emit(
                            ErrorEvent(
                                code="plan_required",
                                detail=(
                                    "the planner did not propose a plan via `submit_plan` "
                                    f"after {_MAX_PLAN_NUDGES} attempts"
                                ),
                            )
                        )
                        return await self.get_state()
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(role="user", content=_PLAN_NUDGE),
                        )
                    )
                    continue
                # Any non-planning step resets the nudge counter so a recovered loop
                # gets a fresh budget the next time it (re-)enters planning.
                self._plan_nudges = 0

                # (f) finish path — subject to stop-hook veto (§7.4)
                if step.finished and step.tool_call is None:
                    if await self._stop_allowed(state, events):
                        # Record the agent's final message (the answer) before
                        # finishing — the deliverable text belongs on the log, not
                        # discarded on the finish signal. (When the model just
                        # answers a question, this IS the response the UI renders.)
                        if step.thought.strip():
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.AGENT,
                                    message=LLMMessage(role="assistant", content=step.thought),
                                )
                            )
                        await self._emit(StatusEvent(status=ConversationStatus.FINISHED))
                        return await self.get_state()
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(role="user", content=self._veto_feedback),
                        )
                    )
                    continue

                # (g) no-op step (thought only) — record and continue
                if step.tool_call is None:
                    await self._emit(
                        MessageEvent(
                            source=EventSource.AGENT,
                            message=LLMMessage(role="assistant", content=step.thought),
                        )
                    )
                    continue

                # (h) build the ActionEvent
                action = ActionEvent(
                    thought=step.thought,
                    tool_call=step.tool_call,
                    self_assessed_risk=step.self_assessed_risk,
                    llm_response_id=step.llm_response_id,
                )

                # (i) RISK GATE — assess, then maybe require confirmation (§5).
                # Audit (security §7): when the analyzer exposes the detailed
                # assessment, stamp it into the action's meta so the security
                # posture (final risk, rationale, contributing analyzers, the
                # self-assessment) is reconstructable from the log. Analyzers that
                # implement only assess() are unaffected.
                detailed = getattr(self.analyzer, "assess_detailed", None)
                if callable(detailed):
                    assessment = detailed(action)
                    risk = assessment.risk
                    audited_meta = {
                        **action.meta,
                        "risk_assessment": assessment.model_dump(mode="json"),
                    }
                    action = action.model_copy(update={"meta": audited_meta})
                else:
                    risk = self.analyzer.assess(action)
                if self.policy.should_confirm(risk):
                    await self._emit(action)  # record the PROPOSED action
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.WAITING_FOR_CONFIRMATION,
                            detail=action.id,
                        )
                    )
                    return await self.get_state()

                action_to_execute = action

            # (j) EXECUTE outside the lock (long-running; lock only guards state)
            await self._emit(action_to_execute)
            await self._execute_and_observe(action_to_execute)
            # loop continues

    @staticmethod
    def _has_unprocessed_user_message(events: list[Event]) -> bool:
        """True if a USER message arrived after the most recent agent activity —
        i.e. there is fresh work (a new goal, or a reopen after FINISHED/STUCK)."""
        last_user = max(
            (
                e.seq or 0
                for e in events
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            default=None,
        )
        if last_user is None:
            return False
        # Progress = the agent acted, spoke, or the loop reached a run/terminal
        # status after the message. A finish-only step leaves no action/message,
        # so the terminal StatusEvent is what marks the goal as processed.
        activity_statuses = {
            ConversationStatus.RUNNING,
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
        }
        last_progress = max(
            (
                e.seq or 0
                for e in events
                if isinstance(e, ActionEvent)
                or (isinstance(e, MessageEvent) and e.source == EventSource.AGENT)
                or (isinstance(e, StatusEvent) and e.status in activity_statuses)
            ),
            default=0,
        )
        return last_user > last_progress

    # ---- control operations (§7) — map from the wire frames -----------------

    async def send_message(self, text: str, *, steer: bool = False) -> ConversationState:
        """Append a USER message. Never dropped; picked up at the next iteration
        (the next View includes it). If the conversation had FINISHED/STUCK, it
        reopens to IDLE (§2). `steer` differs only in UI intent (BoD §13.4)."""
        async with self._lock:
            state = await self.get_state()
            await self._emit(
                MessageEvent(
                    source=EventSource.USER,
                    message=LLMMessage(role="user", content=text),
                    meta={"steer": True} if steer else {},
                )
            )
            if state.execution_status in (
                ConversationStatus.FINISHED,
                ConversationStatus.STUCK,
            ):
                await self._emit(StatusEvent(status=ConversationStatus.IDLE))
        return await self.get_state()

    async def steer(self, text: str) -> ConversationState:
        return await self.send_message(text, steer=True)

    async def confirm(self) -> ConversationState:
        """Phase 2 of the confirmation gate: execute EXACTLY the pending action
        (no re-ask to the model), then resume (§5)."""
        async with self._lock:
            state = await self.get_state()
            if (
                state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
                or state.pending_action_id is None
            ):
                return state
            pending = await self._event_by_id(state.pending_action_id)
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        if isinstance(pending, ActionEvent):
            await self._execute_and_observe(pending)  # outside the lock
        return await self.get_state()

    async def reject(self, reason: str = "rejected by user") -> ConversationState:
        """Deny the pending action: record the denial (so the model sees it next
        View) and resume to RUNNING without executing (§5)."""
        async with self._lock:
            state = await self.get_state()
            if state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION:
                return state
            await self._emit(
                AgentErrorEvent(error=f"Action {reason}.", action_id=state.pending_action_id)
            )
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        return await self.get_state()

    async def approve_plan(self) -> ConversationState:
        """Approve the pending plan: flip into execution mode (full tools restored)
        and resume to RUNNING. The caller then re-runs the loop. The per-action
        risk gate still governs the build that follows (defense in depth)."""
        async with self._lock:
            state = await self.get_state()
            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
                return state
            self.mode = self._execution_mode
            await self._emit(
                StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
            )
        return await self.get_state()

    async def enter_planning(self, text: str = "") -> ConversationState:
        """(Re-)enter PLANNING mode — the entry point for the first plan AND for
        re-planning after a build, so focused, diff-style changes are articulated
        and re-approved rather than free-form steered. `text` is the user's
        instruction for what to (re)plan. Reopens from FINISHED/STUCK."""
        async with self._lock:
            if text.strip():
                await self._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                    )
                )
            self.mode = OperatingMode.PLANNING
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
        return await self.get_state()

    async def pause(self) -> ConversationState:
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.PAUSED))
        return await self.get_state()

    async def resume(self) -> ConversationState:
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        return await self.run()

    async def cancel(self) -> ConversationState:
        """Cooperative stop (distinct from the network-level kill switch, §7.3).
        Emits a terminal IDLE; the loop returns at its next checkpoint."""
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.IDLE, detail="cancelled"))
        return await self.get_state()
