"""Internal AgentLoop collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine_contracts import (
    _MIDSTEP_STEER_REFUSAL,
    ActionEvent,
    AgentErrorEvent,
    AgentStep,
    ConversationState,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    OperatingMode,
    StatusEvent,
    signals,
)

if TYPE_CHECKING:
    from .engine import AgentLoop


class ReplanningController:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def enter_planning(self, text: str = "") -> ConversationState:
        """(Re-)enter PLANNING mode — the entry point for the first plan AND for
        re-planning after a build, so focused, diff-style changes are articulated
        and re-approved rather than free-form steered. `text` is the user's
        instruction for what to (re)plan. Reopens from FINISHED/STUCK."""
        async with self._loop._lock:
            if text.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                    )
                )
            self._loop.mode = OperatingMode.PLANNING
            self._loop._plan_explore_reads = 0  # (B2/B6) fresh planning segment
            events = await self._loop._events()
            latest_status = next(
                (event for event in reversed(events) if isinstance(event, StatusEvent)),
                None,
            )
            if latest_status is None or latest_status.detail != "planning":
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="planning")
                )
            # (B2/B6) On a revision (prior plan_approved) with a concrete new
            # instruction, frame the turn so the model RE-plans instead of
            # free-building against the OLD plan (logic in Planner).
            await self._loop._planner.emit_replan_framing_if_revision(text)
        return await self._loop.get_state()

    async def _maybe_reenter_planning_for_followup(self, events: list[Event]) -> bool:
        """Bug 12 (§11.4) — when a CHANGE/REVISION follow-up arrives on an
        approved/finished build (FINISHED→followup OR a mid-run RUNNING→steer),
        re-enter PLANNING so the next model turn runs with planning tools only and
        a write is REJECTED by `_gate_planning_mode` until a revised plan is
        submitted + approved. Without this the loop stays in execution mode and the
        model free-builds against the STALE approved plan (NO_REPLAN_AFTER_REVISION).

        Supplies the same `planning` marker `enter_planning()`/`request_plan()` emit
        (so `signals.in_planning_for_revision` + the actionless valve + the
        post-restart mode reconstruction all engage); the existing revision
        machinery (`Planner.plan_from_args` → revision = prev+1) does the rest.

        Lock-free (caller holds `self._lock`). Does NOT re-append the user message —
        it is already in the log. Returns True iff it re-entered planning.

        Conservative: fires ONLY for a plan-gated conversation already in execution
        mode, with a fresh unprocessed user turn whose intent is a CHANGE
        (`signals.is_revision_intent`). A pure Q&A follow-up ("what font did you
        use?") is exempt — it is answered without a forced re-plan."""
        self._loop._reconcile_mode_from_events(events)
        # Already (re)planning → nothing to do (also guards against re-firing on
        # the same follow-up once we've emitted the planning marker below).
        if self._loop.mode == OperatingMode.PLANNING:
            return False
        # Plan-gated only: a plan must have been approved at some point. Non-build
        # surfaces (Research) never emit plan_approved, so this never fires there.
        if not any(isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events):
            return False
        # A reply to the shared blocked lander is the answer to the agent's
        # pending question, not a host-side revision steer. Deliver it in the
        # next model context and let the model call propose_plan_update if it
        # decides the answer changes scope.
        if signals.latest_user_answers_blocked_question(events):
            return False
        # PRIMARY (live WS/kernel steer): a durable, sequence-stable
        # `revision_steer_pending` marker the kernel ingress appended. Consumed
        # UNCONDITIONALLY here, BEFORE the unprocessed-text predicate — an in-flight
        # ActionEvent/ObservationEvent (the seq N+k write) invalidates
        # `has_unprocessed_user_message`, so the text path below MISSES the steer (the
        # live NO_REPLAN race). The sequence-stable marker cannot be masked that way.
        if signals.pending_revision_steer(events):
            await self._loop._enter_revision_planning(signals.latest_user_text(events) or "")
            return True
        # FALLBACK (non-kernel / direct send_message ingress): text-based detection.
        # First use the usual unprocessed-user predicate. If pickup activity after a
        # terminal/idle status has already masked that predicate, fall back to the
        # terminal-idle scoped signal so INACTIVE_TIMEOUT/IDLE follows the same
        # revised-plan gate as FINISHED/STUCK/ERROR.
        text = signals.latest_unprocessed_user_text(events)
        if text is None:
            text = signals.latest_terminal_idle_followup_user_text(events)
        if text is None:
            return False
        if not signals.is_revision_intent(text):
            return False  # pure Q&A — answerable without a forced re-plan
        await self._loop._enter_revision_planning(text)
        return True

    async def _enter_revision_planning(self, text: str) -> None:
        """Shared re-plan transition (lock-free; caller holds self._lock): flip to
        PLANNING + reset the planning segment + emit the `planning` marker + revision
        framing. ONE transition shared by the steer-INGEST path (send_message), the
        top-of-loop follow-up check, and the mid-step gate — so they cannot diverge."""
        self._loop.mode = OperatingMode.PLANNING
        self._loop._plan_explore_reads = 0  # (B2/B6) fresh planning segment
        await self._loop._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
        await self._loop._planner.emit_replan_framing_if_revision(text)

    async def _gate_midstep_steer_replan(self, step: AgentStep) -> Disp:
        """Bug 12 (§11.4) — close the IN-FLIGHT steer write-through race. The
        top-of-loop re-plan check (`_run_drive`) runs BEFORE `drive_step()`; a
        change/revision steer that lands WHILE the model is mid-turn is therefore
        missed by it, and the in-flight step may be a WRITE against the OLD plan.

        This apply-time gate runs AFTER `drive_step()` returns, just before the
        ActionEvent is built/executed. It RE-POLLS the log for a fresh unprocessed
        CHANGE follow-up since the last approval — for ANY tool, read/think/explore
        INCLUDED. The re-poll-on-reads matters: if it only fired for mutating
        tools, a READ in flight when the steer lands would proceed and emit its
        ActionEvent AFTER the steer, burying the steer's unprocessed marker — so
        neither the next top-of-loop check NOR a later apply-gate would see it, and
        a subsequent write would slip through on the stale plan (codex-found
        residual window).

        On detecting a pending change steer it RE-ENTERS PLANNING immediately
        (sets `mode=PLANNING` + the `planning` marker + replan framing) regardless
        of the current tool. THEN, for the CURRENT tool:
          * MUTATING (anything the planning gate would reject) → REJECT it
            recoverably (record the ActionEvent so the assistant tool_call stays
            PAIRED with a tool-role result, then a paired AgentErrorEvent the View
            keeps) — no write lands on the stale plan.
          * an ALLOWED read/think/explore tool → let it PROCEED (harmless); the
            invariant is preserved because PLANNING is now set, so EVERY subsequent
            tool this run — including the model's next write — is rejected by
            `_gate_planning_mode` until a revised plan is approved.

        A pure Q&A follow-up is exempt (`signals.is_revision_intent`). Lock-free
        (caller holds `self._lock`). Returns CONTINUE when it deferred a mutating
        call, else FALLTHROUGH (a harmless read OR the common no-steer case — zero
        behavior change for a normal execution turn)."""
        if self._loop.mode == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH  # the planning gate already governs writes
        tc = step.tool_call
        if tc is None:
            return Disp.FALLTHROUGH
        # Re-poll for a steer that may have landed DURING the just-finished
        # drive_step — for ANY tool, so a read-in-flight re-enters planning BEFORE
        # its ActionEvent buries the steer marker (no read-then-write window).
        fresh = await self._loop._events()
        if not await self._loop._maybe_reenter_planning_for_followup(fresh):
            return Disp.FALLTHROUGH  # no pending change follow-up (or Q&A) — proceed
        # PLANNING is now re-entered. A read/think/explore tool (anything the
        # planning gate allows) may proceed harmlessly — _gate_planning_mode now
        # governs every subsequent tool. A MUTATING tool is deferred here so it
        # never lands on the stale plan; the model must submit a revised plan.
        if tc.tool_name in self._loop._driver.planning_allowed_tool_names():
            return Disp.FALLTHROUGH
        action = ActionEvent(
            thought=step.thought,
            tool_call=tc,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        await self._loop._emit(action)
        await self._loop._emit(
            AgentErrorEvent(
                error=_MIDSTEP_STEER_REFUSAL.format(tool=tc.tool_name),
                action_id=action.id,
                tool_call_id=tc.call_id,
            )
        )
        return Disp.CONTINUE
