"""Internal AgentLoop collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine_contracts import (
    ActionEvent,
    AgentErrorEvent,
    ConversationState,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    OperatingMode,
    StatusEvent,
    _optional_async_fence,
    event_matches_current_workspace_view,
    signals,
)

if TYPE_CHECKING:
    from .engine import AgentLoop


class ConversationControls:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def send_message(self, text: str, *, steer: bool = False) -> ConversationState:
        """Append a USER message. Never dropped; picked up at the next iteration
        (the next View includes it). If the conversation had FINISHED/STUCK, it
        reopens to IDLE (§2). `steer` differs only in UI intent (BoD §13.4)."""
        self._loop._retry_interrupt.set()
        try:
            async with self._loop._lock:
                state = await self._loop.get_state()
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                        meta={"steer": True} if steer else {},
                    )
                )
                self._loop._reconcile_mode_from_events(await self._loop._events())
                if state.execution_status in (
                    ConversationStatus.FINISHED,
                    ConversationStatus.STUCK,
                ):
                    await self._loop._emit(StatusEvent(status=ConversationStatus.IDLE))
                # DURABLE NO_REPLAN fix — deterministic re-plan at steer INGEST. A
                # scope-adding/revision steer on an APPROVED plan must re-enter PLANNING the
                # MOMENT it arrives, so the next write is gated by `_gate_planning_mode` until
                # a revised plan is approved (Manus-UX: a mid-run scope change surfaces a
                # VISIBLE re-plan boundary, not a silent build on the stale plan). The two
                # polling guards (`_maybe_reenter_planning_for_followup` + the mid-step gate)
                # are POLLING-based and RACE with an in-flight turn whose response buries the
                # unprocessed-user marker; doing it at ingest is race-free. SKIP while a
                # confirmation / plan-approval is pending (those control-pending states are
                # owned by confirm/reject/approve). Q&A is exempt (`is_revision_intent`).
                if (
                    steer
                    and self._loop.mode != OperatingMode.PLANNING
                    and state.execution_status
                    not in (
                        ConversationStatus.WAITING_FOR_CONFIRMATION,
                        ConversationStatus.AWAITING_PLAN_APPROVAL,
                    )
                    and signals.is_revision_intent(text)
                ):
                    evs = await self._loop._events()
                    if not signals.current_blocked_question_landing(evs) and any(
                        isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in evs
                    ):
                        await self._loop._enter_revision_planning(text)
            return await self._loop.get_state()
        finally:
            self._loop._retry_interrupt.clear()

    async def steer(self, text: str) -> ConversationState:
        return await self._loop.send_message(text, steer=True)

    async def confirm(self) -> ConversationState:
        """Phase 2 of the confirmation gate: execute EXACTLY the pending action
        (no re-ask to the model), then resume (§5)."""
        async with self._loop._lock, _optional_async_fence(self._loop._control_fence):
            state = await self._loop.get_state()
            if (
                state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
                or state.pending_action_id is None
            ):
                return state
            pending = await self._loop._event_by_id(state.pending_action_id)
            events = await self._loop._events()
            if not isinstance(pending, ActionEvent) or not event_matches_current_workspace_view(
                events, pending
            ):
                await self._loop._emit(
                    AgentErrorEvent(
                        error="<system-reminder>That proposed action belongs to a superseded "
                        "model view and was not executed. Continue from the latest user "
                        "instruction.</system-reminder>",
                        action_id=state.pending_action_id,
                    )
                )
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="stale_confirmation")
                )
                return await self._loop.get_state()
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    agent_view_id=pending.agent_view_id,
                )
            )
        if isinstance(pending, ActionEvent):
            await self._loop._execute_and_observe(pending)  # outside the lock
        return await self._loop.get_state()

    async def reject(self, reason: str = "rejected by user") -> ConversationState:
        """Deny the pending action: record the denial (so the model sees it next
        View) and resume to RUNNING without executing (§5).

        The rejection is recorded as an `AgentErrorEvent` whose content is wrapped
        in an implicit `<system-reminder>` — the action did not produce an error,
        the human declined it. Paired with the proposed action's call_id so the
        provider adapter sees a properly-correlated tool result."""
        async with self._loop._lock, _optional_async_fence(self._loop._control_fence):
            state = await self._loop.get_state()
            if state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION:
                return state
            pending = (
                await self._loop._event_by_id(state.pending_action_id)
                if state.pending_action_id
                else None
            )
            events = await self._loop._events()
            pending_view_id = (
                pending.agent_view_id
                if isinstance(pending, ActionEvent)
                and event_matches_current_workspace_view(events, pending)
                else None
            )
            call_id: str | None = None
            if isinstance(pending, ActionEvent) and pending.tool_call:
                call_id = pending.tool_call.call_id
            reminder = (
                "<system-reminder>\n"
                f"The user reviewed the proposed action and did not approve it "
                f"({reason}). The action was NOT executed. Pick a different "
                "approach.\n"
                "</system-reminder>"
            )
            await self._loop._emit(
                AgentErrorEvent(
                    error=reminder,
                    action_id=state.pending_action_id,
                    tool_call_id=call_id,
                    agent_view_id=pending_view_id,
                )
            )
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    agent_view_id=pending_view_id,
                )
            )
        return await self._loop.get_state()

    async def pause(self) -> ConversationState:
        """WALK-18 — cooperative pause. Deliberately does NOT take self._lock
        (unlike cancel): the running turn holds the lock across its model step,
        so taking it here would block until the step finishes — exactly the
        "pause does nothing until I steer" bug. Instead set a flag the run()
        loop observes at its next step boundary, where it emits PAUSED."""
        self._loop._pause_requested.set()
        self._loop._retry_interrupt.set()
        return await self._loop.get_state()

    async def resume(self) -> ConversationState:
        self._loop._pause_requested.clear()  # WALK-18 — resume cancels a pending pause
        self._loop._retry_interrupt.clear()
        async with self._loop._lock:
            await self._loop._emit(StatusEvent(status=ConversationStatus.RUNNING))
        return await self._loop.run()

    async def cancel(self) -> ConversationState:
        """Cooperative stop (distinct from the network-level kill switch, §7.3).
        Emits a terminal IDLE; the loop returns at its next checkpoint."""
        self._loop._retry_interrupt.set()
        try:
            async with self._loop._lock:
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.IDLE, detail="cancelled")
                )
            return await self._loop.get_state()
        finally:
            self._loop._retry_interrupt.clear()
