"""Internal Loop turn-control collaborator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..events import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from ..llm import OperatingMode
from . import signals
from .boundaries import AgentStep
from .control import Disp
from .turn_control_support import (
    _ACTIONLESS_AUTO_RESUME_MARKER,
    _ACTIONLESS_AUTO_RESUME_SEGMENT_CAP,
    _plan_done_and_verified,
)
from .valve_landing import _ValveHost

if TYPE_CHECKING:
    pass

_LOG = logging.getLogger("disco.loop")


class ActionlessValveMixin(_ValveHost):
    @staticmethod
    def _actionless_auto_resume_total(events: list[Event]) -> int:
        return sum(
            1
            for event in events
            if isinstance(event, MessageEvent)
            and event.source == EventSource.ENVIRONMENT
            and event.message is not None
            and _ACTIONLESS_AUTO_RESUME_MARKER in (event.message.content or "")
        )

    @staticmethod
    def _has_plan_approval(events: list[Event]) -> bool:
        return any(
            isinstance(event, StatusEvent) and event.detail == "plan_approved" for event in events
        )

    def _actionless_ladder_still_front(self, events: list[Event]) -> bool:
        """True when autonomous supervision can still consume PAUSED(actionless)."""
        if not self._loop._autonomous or self._loop.mode == OperatingMode.PLANNING:
            return False
        if not self._has_plan_approval(events):
            return False
        if self._actionless_auto_resume_total(events) >= _ACTIONLESS_AUTO_RESUME_SEGMENT_CAP:
            return False
        pause_count = signals.actionless_pause_count_current_execution_segment(events)
        if pause_count == 0:
            return True
        return (
            pause_count == 1
            and signals.productive_action_since_approval(events)
            and not signals.synthetic_finish_attempted_for_current_pause(events)
        )

    async def _pause_actionless(self, content: str) -> bool:
        """Land an actionless breaker.

        Autonomous build runs keep the historical PAUSED(actionless) marker only
        while the external auto-resume/synthetic-finish ladder can still consume
        it. Interactive runs, and autonomous runs after that ladder is exhausted,
        explain the block and park at AWAITING_USER_QUESTION.
        """
        events = await self._loop._events()
        if not self._actionless_ladder_still_front(events):
            await self.land_blocked(
                reason="actionless",
                guidance=content,
                legacy_status=ConversationStatus.PAUSED,
                legacy_detail="actionless",
            )
            return True
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=content),
            )
        )
        await self._loop._emit(StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
        return True

    async def _halt_pending_revision_stall(
        self,
        events: list[Event],
        pending_revision: bool,
    ) -> bool:
        if not pending_revision or (
            signals.planning_turns_since_replan(events) < self._loop._max_consecutive_noops
        ):
            return False
        await self.land_blocked(
            reason="actionless",
            guidance=(
                "The agent stayed in planning for several turns without "
                "submitting a revised plan while a re-plan is pending."
            ),
            legacy_status=ConversationStatus.PAUSED,
            legacy_detail="actionless",
        )
        return True

    async def _handle_actionless_break_cap(
        self,
        events: list[Event],
        noops: int,
        *,
        incomplete: bool,
        pending_revision: bool,
    ) -> bool | None:
        if noops < self._loop._ACTIONLESS_BREAK_CAP:
            return None
        completed = (
            not pending_revision
            and signals.plan_steps_complete(events)
            and (
                signals.productive_actions_since_approval(events) > 0
                or _plan_done_and_verified(events)
            )
        )
        if completed:
            return await self._completed_via_notify_finish(events)
        if (
            not pending_revision
            and await self._loop._finish.verification.maybe_honest_unverifiable_static_actionless_finish(
                events
            )
        ):
            return True
        if not incomplete:
            return None
        return await self._pause_actionless(
            "The agent produced 3 consecutive responses"
            " without doing any real work while plan steps"
            " remain undone — pausing instead of burning"
            " tokens. Resume to continue."
        )

    async def _handle_actionless_noop_limit(
        self,
        events: list[Event],
        noops: int,
        *,
        incomplete: bool,
        pending_revision: bool,
    ) -> bool | None:
        if noops < self._loop._max_consecutive_noops:
            return None
        if pending_revision:
            await self.land_blocked(
                reason="actionless",
                guidance=(
                    "The agent produced repeated responses without submitting "
                    "a revised plan while a re-plan is pending."
                ),
                legacy_status=ConversationStatus.PAUSED,
                legacy_detail="actionless",
            )
            return True
        if incomplete and signals.actions_since_last_resume(events) == 0:
            await self.land_blocked(
                reason="noop_limit",
                guidance="Plan steps remain undone and no real work happened in this run segment.",
                legacy_status=ConversationStatus.PAUSED,
                legacy_detail="noop_limit",
            )
        else:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            f"You have produced {noops} turns in a row without "
                            "performing real work. Ending the run. To continue, "
                            "the user can send a new instruction; otherwise call "
                            "a tool to act or `finish` to complete.\n"
                            "</system-reminder>"
                        ),
                    ),
                )
            )
            await self._loop._emit(
                StatusEvent(status=ConversationStatus.FINISHED, detail="noop_limit")
            )
        return True

    async def _warn_actionless_limit(self, noops: int) -> None:
        if noops != self._loop._max_consecutive_noops - 1:
            return
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You've sent several messages without acting. Call a "
                        "tool to make progress, or `finish` if the task is "
                        "complete.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )

    async def actionless_valve(self, events: list[Event], noops: int) -> bool:
        """Apply the shared actionless circuit-breaker ladder in its fixed order."""
        incomplete, _ = signals.plan_is_incomplete(events)
        pending_revision = signals.in_planning_for_revision(events)
        if await self._halt_pending_revision_stall(events, pending_revision):
            return True
        disposition = await self._handle_actionless_break_cap(
            events,
            noops,
            incomplete=incomplete,
            pending_revision=pending_revision,
        )
        if disposition is not None:
            return disposition
        disposition = await self._handle_actionless_noop_limit(
            events,
            noops,
            incomplete=incomplete,
            pending_revision=pending_revision,
        )
        if disposition is not None:
            return disposition
        await self._warn_actionless_limit(noops)
        return False

    async def _completed_via_notify_finish(self, events: list[Event]) -> bool:
        """W-32 — gate the actionless-valve's `completed_via_notify` FINISH through
        the SAME finish gates a real `finish()` clears, so a "done" signaled via
        notify_user-spam can no longer BYPASS browser-verify / DoD (the root cause:
        the branch used to emit StatusEvent(FINISHED) directly). Returns True iff
        every applicable gate passed and a clean FINISHED was emitted; False iff a
        gate REFUSED — in which case it has already injected its own "verify then
        finish" reminder and the caller must CONTINUE (no pause/stuck) so the model
        verifies before finishing again."""
        finish = self._loop._finish
        # Synthetic finish step: the model never called finish() (it narrated
        # "done" via notify_user), but the gates key off the EVENT LOG, not the
        # step — the only field they read is step.thought (empty here, so nothing
        # extra is surfaced). normalize_finish_step is intentionally NOT run: it
        # asserts a real finish tool_call + runs the agent-attached `verify`
        # command, neither of which exists on a notify-spam "finish"; its DoD half
        # is invoked directly below instead.
        finish_step = AgentStep(finished=True)
        # (1) execution-nudge gate. Inert for this branch (we already required
        # productive_actions_since_approval > 0 ⇒ productive_action_since_approval
        # is True ⇒ FALLTHROUGH), but routed for parity + so the path is gated, not
        # bypassed, if that precondition ever changes. CONTINUE = nudged (keep
        # working); HALT = the gate's own cap-release already landed FINISHED.
        disp = await finish.gate_execution_nudge(finish_step, events)
        if disp is Disp.CONTINUE:
            return False
        if disp is Disp.HALT:
            return True
        events = await self._loop._events()
        if not await finish.dictated_content_gate_passed(events):
            return False
        # (2) render-verify gates — THE W-32 catch, via the SAME shared sequence
        # handle_finish_path runs: host+browser app-verify (per the authoritative
        # flag) THEN the P10 export-render gate. Routing the shared helper (not a bare
        # gate_browser_verify) is what keeps this notify path from drifting: it now
        # runs host-verify (shadow telemetry / authoritative gating) and the deck/doc
        # export gate too, both of which it silently skipped before. Re-poll first:
        # gate_execution_nudge may have emitted.
        events = await self._loop._events()
        disp = await finish.verification.run_finish_verify_gates(finish_step, events)
        if disp is Disp.CONTINUE:
            return False
        if disp is Disp.HALT:
            # W-45: a verify gate halted the run STUCK (repeated same-fingerprint
            # failure with no progress) and emitted the terminal status itself.
            return True
        # (3) external Definition-of-Done gate (no spec → no-op pass-through).
        if not await finish.verification.finish_dod_gate_passed():
            return False
        # (4) REL-27 — a notify-signaled "done" is an affirmative delivery claim
        # and clears the same seal gate a real finish() clears (refusal reminder
        # already injected; False ⇒ CONTINUE so the model can act on it).
        if not await finish.verification.seal_gate_allows_finish():
            return False
        # All gates passed → land a clean FINISHED (same message + detail as the
        # pre-W-32 force-finish, now EARNED rather than bypassed).
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "All plan steps are complete — the agent"
                        " signaled completion without calling finish."
                        " Marking the build finished."
                    ),
                ),
            )
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.FINISHED,
                detail="completed_via_notify",
            )
        )
        return True

    async def post_noop_valve(self) -> Disp:
        """Shared actionless-valve tail for the non-blocking virtual-tool arms
        (notify_user / remember / serve / delegate_explore / plan-nudge /
        execution-nudge / no-op / ask-fresh-session). Re-polls the event log,
        adds the invisible-step counter, and halts the run if the actionless
        valve trips. Byte-identical to the 4-line tail it replaces."""
        # A completion-shaped actionless trip routes through the normal finish
        # gates. A gate refusal can itself call this tail. Treat that nested call
        # as already accounted by the outer valve; otherwise a missing handoff
        # recursively re-enters until RecursionError instead of returning one
        # actionable refusal to the model.
        if self._post_noop_active:
            return Disp.CONTINUE
        self._post_noop_active = True
        try:
            events = await self._loop._events()
            noops = signals.consecutive_noops(events) + self._loop._invisible_steps
            if await self.actionless_valve(events, noops):
                return Disp.HALT
            return Disp.CONTINUE
        finally:
            self._post_noop_active = False
