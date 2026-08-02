"""Internal Loop turn-control collaborator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from ..events import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from . import view_render
from .control import Disp
from .tool_specs import _ask_user_tool_singleton
from .turn_control_support import (
    _BLOCKED_LANDING_META_KEY,
    _ensure_question,
)

if TYPE_CHECKING:
    from .ports import (
        ConversationModePort,
        FinishVerificationPort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
    )

    class ValveLoopFacet(

        ConversationModePort,

        FinishVerificationPort,

        GateCounterPort,

        LoopEventPort,

        PlanLifecyclePort,

        ToolExecutionPort,

        TurnControlPort,

        Protocol,

    ):

        """Shared loop capability for the Valve composition.


        This declaration is inherited by every mixin in the

        composition, so it carries the union of what they reach:

        conversation mode, finish verification, gate counters, the event log,
    the plan lifecycle, tool execution, turn control.

        """

_LOG = logging.getLogger("disco.loop")


class _ValveHost:
    _loop: ValveLoopFacet

    async def post_noop_valve(self) -> Disp:
        raise NotImplementedError

    async def land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
        required_explanation: str = "",
        deterministic: bool = False,
        extra_meta: dict[str, str | int] | None = None,
    ) -> None:
        raise NotImplementedError


class ValveLandingMixin(_ValveHost):
    def _blocked_meta(
        self,
        *,
        reason: str,
        legacy_status: ConversationStatus,
        legacy_detail: str | None,
    ) -> dict[str, str | bool | int]:
        # int in the value union: landing callers may merge diagnostic labels
        # (e.g. driver_error_http_status) via land_blocked's extra_meta.
        meta: dict[str, str | bool | int] = {
            _BLOCKED_LANDING_META_KEY: True,
            "blocked_reason": reason,
            "legacy_status": legacy_status.value,
        }
        if legacy_detail:
            meta["legacy_detail"] = legacy_detail
        return meta

    def _blocked_prompt(self, *, reason: str, guidance: str) -> str:
        guidance_line = f"\nContext: {guidance.strip()}" if guidance.strip() else ""
        return (
            "<system-reminder>\n"
            f"You are blocked because {reason}.\n"
            "Produce a SHORT user-facing message explaining: where the build "
            "stands, what you tried, what is blocking progress, and ONE concrete "
            "question or decision you need from the user. Do not continue the "
            "task. Do not call work tools. If you call a tool, call only "
            "`ask_user` with one free-form `question` and no options."
            f"{guidance_line}\n"
            "</system-reminder>"
        )

    def _fallback_blocked_message(self, *, reason: str, guidance: str) -> str:
        where = (
            guidance.strip() or "the run stopped before it could safely complete the current task."
        )
        return (
            f"I'm blocked because {reason}. Where it stands: {where} "
            "What I tried: I continued the current plan until the loop breaker "
            "stopped the run. What should I do next?"
        )

    async def _blocked_model_message(self, *, reason: str, guidance: str) -> str:
        fallback = self._fallback_blocked_message(reason=reason, guidance=guidance)
        try:
            events = await self._loop._events()
            view, _current_events = await self._loop._materialize_current_view()
            step = await self._loop.agent.step(
                view,
                [_ask_user_tool_singleton()],
                mode=self._loop.mode,
                overflow_signal=view_render.overflow_signal(events),
                on_stream=None,
                temperature=None,
                assist=self._loop._assist,
                attempt=1,
            )
        except Exception as exc:  # noqa: BLE001 - fallback must always explain the block
            _LOG.warning("blocked lander model turn failed: %s", exc)
            return fallback

        if step.finished or step.truncated:
            return fallback
        if step.tool_call is not None:
            if step.tool_call.tool_name != "ask_user":
                return fallback
            question = str(step.tool_call.arguments.get("question") or "").strip()
            return _ensure_question(question or step.thought, fallback)
        return _ensure_question(step.thought, fallback)

    async def land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
        required_explanation: str = "",
        deterministic: bool = False,
        extra_meta: dict[str, str | int] | None = None,
    ) -> None:
        """Explain a breaker halt and park at the free-form user-question gate.

        The old PAUSED/STUCK detail is retained in event metadata for analytics,
        while StatusEvent.detail stays the pending question id required by the
        existing AskPanel reconstruction path.

        `extra_meta` (mirroring land_terminal_with_explanation's optional
        `meta`) is merged into every landing event's non-semantic meta — a
        labeling channel only; it never alters the emitted statuses, details,
        or message content.
        """
        clean_reason = (reason or legacy_detail or legacy_status.value).strip()
        meta = self._blocked_meta(
            reason=clean_reason,
            legacy_status=legacy_status,
            legacy_detail=legacy_detail,
        )
        if extra_meta:
            meta.update(extra_meta)
        autonomous = getattr(self._loop, "_autonomous", False)
        # COUNTER MARKER: the ladder/valve signals (actionless pause counts,
        # stuck-escape scans, REL-RC-P) key on the legacy status events. Emit the
        # legacy status FIRST so every counter keeps working, then supersede it
        # with the explanation + landing below (latest-status readers see the
        # landing; counters see the marker). Autonomous flavor re-lands the
        # legacy status at the END as its terminal state — skip the duplicate.
        if not autonomous:
            await self._loop._emit(
                StatusEvent(
                    status=legacy_status,
                    detail=legacy_detail,
                    meta={**meta, "superseded_by_landing": True},
                )
            )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=self._blocked_prompt(
                        reason=clean_reason,
                        guidance=guidance,
                    ),
                ),
                meta=meta,
            )
        )
        # QUESTION-TEXT ordering is the whole fix: the frontend sets pendingQuestionId
        # ONLY on AWAITING_USER_QUESTION and nulls it on any other status, so a bare
        # PAUSED then — after a seconds-long LLM call — AWAITING left the AskPanel hidden
        # the whole call ("doesn't ask until you refresh/wait"). Interactive path uses the
        # DETERMINISTIC fallback (no model round-trip) so AWAITING emits immediately and
        # the panel is instant; autonomous CONCLUDES (no live panel) so it can still
        # afford the model-authored explanation for the run log.
        content = (
            await self._blocked_model_message(reason=clean_reason, guidance=guidance)
            if autonomous and not deterministic
            else self._fallback_blocked_message(reason=clean_reason, guidance=guidance)
        )
        required = required_explanation.strip()
        if required and required not in content:
            content = f"{content.strip()}\n\nRequired action: {required}"
        q_event = MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content=content),
            meta=meta,
        )
        await self._loop._emit(q_event)
        if autonomous:
            # Two-flavor landing (Dylan, 2026-07-03): headless runs have nobody to
            # answer an open question — after the ladder, the run must CONCLUDE,
            # not hang. Land the legacy terminal status but never bare: the
            # explanation message above always precedes it.
            await self._loop._emit(
                StatusEvent(
                    status=legacy_status,
                    detail=legacy_detail or clean_reason,
                    meta=meta,
                )
            )
            return
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=q_event.id,
                meta=meta,
            )
        )

    async def land_terminal_with_explanation(
        self,
        *,
        reason: str,
        guidance: str = "",
        status: ConversationStatus,
        detail: str,
        meta: dict[str, str | bool] | None = None,
        required_explanation: str = "",
    ) -> None:
        """Emit the same explanation shape as a blocked landing, then conclude.

        This is the terminal half of the two-flavor blocked lander factored as a
        hook for workflow controls that must not park on an open question.
        """

        clean_reason = (reason or detail or status.value).strip()
        landing_meta = self._blocked_meta(
            reason=clean_reason,
            legacy_status=status,
            legacy_detail=detail,
        )
        if meta:
            landing_meta.update(meta)
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=self._blocked_prompt(
                        reason=clean_reason,
                        guidance=guidance,
                    ),
                ),
                meta=landing_meta,
            )
        )
        content = await self._blocked_model_message(
            reason=clean_reason,
            guidance=guidance,
        )
        required = required_explanation.strip()
        if required and required not in content:
            content = f"{content.strip()}\n\nRequired action: {required}"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=content),
                meta=landing_meta,
            )
        )
        await self._loop._emit(StatusEvent(status=status, detail=detail, meta=landing_meta))
