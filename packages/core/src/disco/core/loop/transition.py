"""Internal AgentLoop collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine_contracts import (
    _LOG,
    _MIDSTEP_STEER_REFUSAL,
    _TERMINAL_FOR_NOW,
    ActionEvent,
    AgentErrorEvent,
    AgentStep,
    ConversationState,
    ConversationStatus,
    Disp,
    ErrorEvent,
    Event,
    OperatingMode,
    StatusEvent,
    cast,
    is_finish_tool_name,
)

if TYPE_CHECKING:
    from .engine import AgentLoop


class TransitionCoordinator:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def _land_terminal_success(self, event: StatusEvent) -> Event:
        """Publish terminal success through the sole transition authority."""
        if event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal-success authority accepts FINISHED only")
        return await self._loop._runtime._emit(event)

    async def run(self) -> ConversationState:
        """Drive until a terminal-for-now status. Idempotent to call again after
        a pause/confirmation. [CONTRACT] returns the resulting ConversationState."""
        # Fresh run segment → fresh invisible-step accounting (the counter only
        # measures spin WITHIN a segment; a resume/steer is a clean slate).
        self._loop._invisible_steps = 0
        self._loop._finish_verify_refusals = 0  # fresh segment → fresh verify-cap streak
        self._loop._finish_verify_strips = 0
        self._loop._finish_seal_refusals = 0  # REL-27 — fresh segment → fresh seal streak
        self._loop._workflow_output_contract_refusals = 0
        # C1c — fresh segment → fresh DoD-refusal streak (telemetry; the gate
        # has no cap, but a resume/steer should not carry a streak across).
        self._loop._dod_refusals = 0
        # REL-RC-O — fresh segment → fresh dictated-content refusal budget.
        self._loop._dictated_content_refusals = 0
        # C20 — fresh segment → fresh fan-out budget. The cap is per-run-
        # segment so a resume/steer gets a fresh budget (a steered user
        # message is a clean slate; the prior segment's helper round-trips
        # are already visible in the log).
        self._loop._fanout_count = 0
        # C6 — fresh segment → fresh recitation cadence. The step counter
        # restarts at 0 (so the first step after resume is on a cadence
        # boundary and re-emits the recap — the model may have lost
        # context across the pause and the goal needs to be visible).
        # The signature is reset to None so a plan that arrived mid-pause
        # OR a checklist mark the model made pre-pause is detected as
        # drift on the first post-resume step (it differs from None).
        self._loop._recitation_step_count = 0
        self._loop._recitation_last_signature = None
        # HS-03 — fresh segment → fresh re-ground cadence. The brief
        # asks for "ONCE immediately after a restart/resume" and a
        # per-segment cadence thereafter. The compatibility facade resets both
        # flags immediately before entering this coordinator so a
        # resume (or a steer / first start) gets a fresh post-resume
        # one-shot AND a fresh per-boundary counter (the per-boundary
        # counter is what prevents re-emit on consecutive steps at
        # the same boundary; resetting on every segment makes the
        # cadence scoped to the current run, not the conversation).
        # Contrast with _bootstrap_emitted (F4), which persists across
        # run() segments — the F4 bootstrap is "fire once per
        # conversation" (the model already saw it), while HS-03 is
        # "fire once per resume" (a pause may have lost context, so
        # a resume is exactly when the recap matters).
        # C18 — NOTE: `_plan_step_predicates` is intentionally NOT reset
        # here. The map is per-(plan_revision, step_index) and is
        # populated by `_plan_from_args` at submit_plan / re-plan time;
        # a re-plan overwrites by revision so stale entries can't match.
        # Clearing on every run() entry would wipe the predicates
        # between planning and the first post-approve execution call,
        # silently disabling the advisory check.
        state = await self._loop.get_state()
        if state.execution_status in _TERMINAL_FOR_NOW and state.execution_status != (
            ConversationStatus.IDLE
        ):
            # Paused/waiting/finished/stuck/error: a fresh run must be re-armed by
            # a control op (resume/confirm) or a new message. IDLE means "ready".
            if state.execution_status is ConversationStatus.PAUSED:
                return state
            if state.execution_status in (
                ConversationStatus.WAITING_FOR_CONFIRMATION,
                ConversationStatus.AWAITING_PLAN_APPROVAL,
            ) and not self._loop._has_unprocessed_user_message(await self._loop._events()):
                return state
            # AWAITING_USER_DECISION / AWAITING_USER_QUESTION: the agent
            # voluntarily paused for the user (pick-a-card or free-form question).
            # A new user message (steer / send_message) IS the resume signal —
            # treat it like FINISHED/STUCK below (re-kick if there's fresh work).
            # No special control op needed; the user just typing IS the answer.
            # FINISHED/STUCK/ERROR/AWAITING_USER_*/no new work → idle.
            if not self._loop._has_unprocessed_user_message(await self._loop._events()):
                return state
        # Restore/reconcile the in-memory mode from the event log. `self.mode`
        # is only a cache; phase truth is the latest planning/plan_approved
        # marker so preamble and re-kick paths cannot drift.
        self._loop._reconcile_mode_from_events(await self._loop._events())

        # Bug 12 (§11.4) — FINISHED→followup path. A change/revision follow-up on
        # an approved/finished build re-enters PLANNING here (the planning gate
        # keys on self.mode) so the revision goes through a revised plan rather
        # than a free write on the stale approved plan. Runs AFTER the post-restart
        # mode reconstruction above so self.mode reflects reality. Pure Q&A is
        # exempt (answered in execution mode, no forced re-plan). When it re-enters
        # it already emits RUNNING/planning, so skip the plain RUNNING emit (which
        # would shadow the durable `planning` marker).
        async with self._loop._lock:
            _reentered_planning = await self._loop._maybe_reenter_planning_for_followup(
                await self._loop._events()
            )
        if not _reentered_planning:
            await self._loop._emit(StatusEvent(status=ConversationStatus.RUNNING))

        # EXIT INVARIANT (Fix 3) — in-loop defense-in-depth atop the runtime
        # backstop (9c90d7e). run() emits RUNNING above; each of the drive
        # loop's ~15 exits relies on a PRECEDING emit having set a terminal or
        # parked status. A dropped/unparseable model turn can yield a no-event
        # step, and a HALT path can return while still RUNNING — the
        # conversation would then sit RUNNING forever until the supervisor
        # catches it. Funnel every drive-loop exit through one boundary: on a
        # NORMAL return, if the reconstructed status is still RUNNING (i.e. NOT
        # in _TERMINAL_FOR_NOW), terminalize to STUCK with the SAME detail the
        # runtime backstop uses. An exception (-> runtime ERROR) or a
        # CancelledError (deliberate kill) propagates out of _run_drive()
        # untouched — only a clean, silently-non-concluding return is repaired.
        result = await self._loop._run_drive()
        if (await self._loop.get_state()).execution_status not in _TERMINAL_FOR_NOW:
            _LOG.error(
                "run(%s) drive loop returned without reaching a terminal state; "
                "marking STUCK (in-loop exit invariant)",
                self._loop.conversation_id,
            )
            await self._loop._land_blocked(
                reason="loop ended without reaching a terminal state",
                guidance=(
                    "The drive loop returned cleanly while the conversation was "
                    "still RUNNING, so the host could not prove what should happen next."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="loop ended without reaching a terminal state",
            )
            return await self._loop.get_state()
        return result

    async def _prepare_step_boundary(
        self,
    ) -> tuple[Disp, ConversationState, list[Event]]:
        events = await self._loop._events()
        self._loop._reconcile_mode_from_events(events)
        state = ConversationState.reconstruct(
            self._loop.conversation_id,
            events,
            max_iterations=self._loop.max_iterations,
        )
        if self._loop._pause_requested.is_set():
            self._loop._pause_requested.clear()
            self._loop._retry_interrupt.clear()
            await self._loop._emit(StatusEvent(status=ConversationStatus.PAUSED))
            return Disp.HALT, state, events
        if state.execution_status in (
            ConversationStatus.PAUSED,
            ConversationStatus.IDLE,
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
            ConversationStatus.WAITING_FOR_CONFIRMATION,
            ConversationStatus.AWAITING_PLAN_APPROVAL,
        ):
            return Disp.HALT, state, events
        if state.iteration >= self._loop.max_iterations:
            await self._loop._emit(
                ErrorEvent(
                    code="max_iterations",
                    detail=f"reached {self._loop.max_iterations}",
                )
            )
            return Disp.HALT, state, events
        return Disp.FALLTHROUGH, state, events

    async def _run_boundary_valves(
        self,
        state: ConversationState,
        events: list[Event],
    ) -> tuple[Disp, list[Event]]:
        await self._loop._recit.drain_recovered_memory_facts()
        events = await self._loop._valve.gate_f4_bootstrap(events)
        events = await self._loop._maybe_emit_reground(events)
        for gate in (
            self._loop._valve.gate_stuck,
            self._loop._valve.gate_fresh_read_autoground,
            self._loop._valve.gate_circuit_breaker,
            self._loop._valve.gate_no_progress,
        ):
            disp = await gate(events)
            if disp is not Disp.FALLTHROUGH:
                return disp, events
        disp, events = await self._loop._valve.gate_bookkeeping_streak(events)
        if disp is Disp.HALT:
            return disp, events
        if await self._loop._maybe_reenter_planning_for_followup(events):
            events = await self._loop._events()
        disp = await self._loop._maybe_synthesize_finish_after_actionless_pauses(
            state, events
        )
        return disp, events

    async def _step_boundary(
        self,
    ) -> tuple[Disp, ConversationState, list[Event]]:
        disp, state, events = await self._prepare_step_boundary()
        if disp is not Disp.FALLTHROUGH:
            return disp, state, events
        disp, events = await self._run_boundary_valves(state, events)
        return disp, state, events

    async def _drive_candidate(
        self, events: list[Event]
    ) -> tuple[Disp, AgentStep | None, list[Event]]:
        view, events = await self._loop._materialize_current_view()
        step, disp = await self._loop._driver.drive_step(view, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, step, events
        assert step is not None
        disp = await self._loop._gate_stuck_escape_tool_quarantine(step, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, step, events
        disp = await self._loop._gate_planning_mode(step, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, step, events
        disp = await self._loop._gate_out_of_phase_submit_plan(step, events)
        if disp is Disp.CONTINUE:
            return disp, step, events
        return Disp.FALLTHROUGH, step, events

    async def _dispatch_virtual_tools(
        self, step: AgentStep, events: list[Event]
    ) -> tuple[Disp, AgentStep]:
        tool_call = step.tool_call
        if tool_call is None:
            return Disp.FALLTHROUGH, step
        disp = await self._dispatch_consumed_meta_tool(step, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, step
        if tool_call.tool_name in ("skip", "needs_input"):
            disp = await self._loop._meta.handle_workflow_control(step, events)
            if disp is not Disp.FALLTHROUGH:
                return disp, step
        if not is_finish_tool_name(tool_call.tool_name, self._loop._finish_alias):
            return Disp.FALLTHROUGH, step
        requested = step.requested_verification or (
            self._loop._finish_alias is not None
            and tool_call.tool_name == self._loop._finish_alias
        )
        if tool_call.tool_name != "finish" or requested != step.requested_verification:
            step = step.model_copy(
                update={
                    "requested_verification": requested,
                    "tool_call": tool_call.model_copy(update={"tool_name": "finish"}),
                }
            )
        step, disp = await self._loop._finish.normalize_finish_step(step, events)
        return (Disp.CONTINUE if disp is Disp.CONTINUE else Disp.FALLTHROUGH), step

    async def _dispatch_consumed_meta_tool(
        self, step: AgentStep, events: list[Event]
    ) -> Disp:
        assert step.tool_call is not None
        handler = {
            "notify_user": self._loop._meta.handle_notify_user,
            "remember": self._loop._meta.handle_remember,
            "serve": self._loop._meta.handle_serve,
            "delegate_explore": self._loop._meta.handle_delegate_explore,
        }.get(step.tool_call.tool_name)
        if handler is None:
            return Disp.FALLTHROUGH
        disp = await handler(step, events)
        return Disp.HALT if disp is Disp.HALT else Disp.CONTINUE

    async def _dispatch_completion(
        self,
        step: AgentStep,
        state: ConversationState,
        events: list[Event],
    ) -> Disp:
        self._loop._plan_nudges = 0
        if step.truncated and step.tool_call is None:
            disp = await self._loop._meta.handle_truncated_step(step, events)
            if disp is not Disp.FALLTHROUGH:
                return disp
        if step.finished and step.tool_call is None:
            disp = await self._loop._finish.handle_finish_path(step, state, events)
            if disp is not Disp.FALLTHROUGH:
                return disp
        if step.tool_call is None:
            disp = await self._loop._meta.handle_noop_step(step, events)
            return Disp.HALT if disp is Disp.HALT else Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def _dispatch_human_controls(
        self, step: AgentStep, events: list[Event]
    ) -> Disp:
        disp = await self._loop._meta.gate_ask_fresh_session(step, events)
        if disp is not Disp.FALLTHROUGH:
            return disp
        disp = await self._loop._meta.gate_autonomous_ask_stall(step, events)
        if disp is Disp.CONTINUE:
            return disp
        tool_call = step.tool_call
        assert tool_call is not None
        if tool_call.tool_name == "propose_plan_update":
            disp = await self._loop._meta.handle_propose_plan_update(step, events)
            if disp is not Disp.FALLTHROUGH:
                return disp
        if tool_call.tool_name == "questions_v2":
            if await self._loop._meta.handle_questions_v2(step, events) is Disp.HALT:
                return Disp.HALT
        if tool_call.tool_name == "clarify":
            if await self._loop._meta.handle_clarify(step, events) is Disp.HALT:
                return Disp.HALT
        if tool_call.tool_name == "ask_user":
            if await self._loop._meta.handle_ask_user(step, events) is Disp.HALT:
                return Disp.HALT
        return await self._loop._gate_midstep_steer_replan(step)

    async def _build_action(self, step: AgentStep) -> tuple[Disp, ActionEvent | None]:
        self._loop._invisible_steps = 0
        assert step.tool_call is not None
        action = ActionEvent(
            thought=step.thought,
            tool_call=step.tool_call,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        disp = await self._loop._gate_hard_deny(action)
        if disp is Disp.CONTINUE:
            return disp, None
        disp, action = await self._loop._gate_risk_confirm(action)
        return disp, action

    async def _select_action(
        self,
        state: ConversationState,
        events: list[Event],
    ) -> tuple[Disp, ActionEvent | None]:
        disp, step, events = await self._drive_candidate(events)
        if disp is not Disp.FALLTHROUGH:
            return disp, None
        assert step is not None
        disp, step = await self._dispatch_virtual_tools(step, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, None
        disp = await self._dispatch_completion(step, state, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, None
        disp = await self._dispatch_human_controls(step, events)
        if disp is not Disp.FALLTHROUGH:
            return disp, None
        return await self._build_action(step)

    async def _execute_selected_action(self, action: ActionEvent) -> Disp:
        async with self._loop._lock:
            steer_refused = (
                self._loop.mode == OperatingMode.PLANNING
                and action.tool_call is not None
                and action.tool_call.tool_name
                not in self._loop._driver.planning_allowed_tool_names()
            )
        stored = cast("ActionEvent", await self._loop._emit(action))
        if steer_refused and stored.tool_call is not None:
            await self._loop._emit(
                AgentErrorEvent(
                    error=_MIDSTEP_STEER_REFUSAL.format(tool=stored.tool_call.tool_name),
                    action_id=stored.id,
                    tool_call_id=stored.tool_call.call_id,
                )
            )
            return Disp.CONTINUE
        await self._loop._execute_and_observe(stored)
        return await self._loop._maybe_apply_read_churn_valve(stored)

    async def _run_drive(self) -> ConversationState:
        """Sole ordered loop: select under lock, execute outside it."""
        while True:
            async with self._loop._lock:
                disp, state, events = await self._step_boundary()
                if disp is Disp.FALLTHROUGH:
                    disp, action = await self._select_action(state, events)
                else:
                    action = None
                if disp is Disp.HALT:
                    return await self._loop.get_state()
                if disp is Disp.CONTINUE:
                    continue
                assert action is not None
            disp = await self._execute_selected_action(action)
            if disp is Disp.HALT:
                return await self._loop.get_state()
