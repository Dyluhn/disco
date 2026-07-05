"""Turn-taking control: the actionless/stuck/circuit-breaker valves and the
virtual meta-tool handlers (notify_user / remember / serve / delegate_explore /
ask_user / questions_v2 / clarify / propose_plan_update / no-op).

Extracted from engine.py as TWO co-located back-ref collaborators (they both
poke the shared `self._loop._invisible_steps` counter, so they ship together):

  * `Valve`  — the spin/loop guards: the actionless valve ladder, the F4
    bootstrap one-shot, the stuck-escape gate, the circuit breaker, the
    soft plan-step-lag nudge, and the bookkeeping-streak guard.
  * `MetaToolHandlers` — the run-loop intercepts for the virtual tools.

Method bodies are byte-identical to the former AgentLoop methods with `self.`
rewritten to `self._loop.`; same-collaborator sibling calls stay direct, and the
shared `_post_noop_valve` / `_refuse_fresh_session` cross-references go through
the loop (`self._loop._post_noop_valve()`, `self._loop._valve.refuse_fresh_session`).
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    AlternativeOption,
    AlternativesEvent,
    ConversationStatus,
    DeliverableEvent,
    Event,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from ..llm import OperatingMode
from . import signals, view_render
from .bootstrap import _detect_project_bootstrap
from .boundaries import AgentStep
from .control import Disp
from .messages import _stuck_escape_reminder
from .observe import _FANOUT_INPUT_MAX_CHARS
from .stuck import (
    F6_FILE_MUTATING_TOOLS,
    barren_streak_no_progress,
    repeated_verify_no_progress,
)
from .tool_specs import _ask_user_tool_singleton

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

# plan_step-spam guard (issue C). Soft nudge first, hard halt well below the
# 15-31× spam observed live; thresholds are conservative so a weak model doing a
# small legitimate bookkeeping burst is never penalized.
_BOOKKEEPING_STREAK_NUDGE_AT = 3
_BOOKKEEPING_STREAK_HALT_AT = 6
# Slack added to the active plan's step count when sizing the HALT cap, so a
# model that legitimately marks each plan step done (one `plan_step` per step)
# plus a couple of over-corrections/verifications isn't penalized.
_BOOKKEEPING_PLAN_SLACK = 2

# C8 (T11): bound the autonomous `propose_plan_update` loop. When the proposed
# plan's steps are byte-identical to the immediately-prior plan for >= this many
# consecutive auto-approved revisions, feed the existing bookkeeping-stuck valve.
_PROPOSE_PLAN_UPDATE_REPEAT_CAP = 3

# D2: the reserved AlternativesEvent option id for "Continue anyway" — the bypass
# the user can always pick at the circuit-breaker gate to reset the failure streak
# and let the agent keep going. The frontend renders it as a distinct button;
# pick_alternative special-cases it (reset streak + resume) rather than running a tool.
_CONTINUE_OPTION_ID = "__continue__"



def _rewrite_directive_marker_active(events: list[Event], path: str) -> bool:
    marker_detail = f"rewrite_directive:{path}"
    marker_index: int | None = None
    for i, event in enumerate(events):
        if isinstance(event, StatusEvent) and event.detail == marker_detail:
            marker_index = i
    if marker_index is None:
        return False

    matching_actions: set[str] = set()
    for event in events[marker_index + 1 :]:
        if isinstance(event, ActionEvent):
            tc = event.tool_call
            if (
                tc.tool_name in F6_FILE_MUTATING_TOOLS
                and tc.arguments.get("path") == path
            ):
                matching_actions.add(event.id)
        elif (
            isinstance(event, ObservationEvent)
            and event.action_id in matching_actions
            and event.tool_result.success
        ):
            return False
    return True


def _coerce_choice_label(opt: object, _depth: int = 0) -> list[str]:
    """Normalize ONE raw `clarify` choice-option into zero or more clean label
    strings. Defensive against the model's shape drift — exactly the discipline
    `plans.py:alternatives_from_args` applies to `ask_user` options, which the
    clarify handler historically lacked.

    Observed live malformations (build `clarify`, real models) that the naive
    `str(o)` mangled into garbage or empties:
      * a `{"id": "...", "question"/"label": "..."}` object  → `str(dict)`
        rendered the whole Python-repr as one radio label.
      * a NESTED list `["A", ["B", "C"]]`                    → `str(list)`
        rendered the whole repr as one radio label.
      * empty placeholders `["", "", ""]`                     → blank radios
        ("empty bubble cards" with nothing to pick).

    This returns FLAT, human-readable, non-empty labels; dicts contribute their
    best text field; nested lists are flattened; empties are dropped."""
    if _depth > 4:
        return []
    if isinstance(opt, str):
        s = opt.strip()
        return [s] if s else []
    if isinstance(opt, bool):
        return [str(opt)]
    if isinstance(opt, (int, float)):
        return [str(opt)]
    if isinstance(opt, dict):
        # Prefer a human-readable field; the model reuses the multi-question
        # item shape ({id, question}) and the ask_user option shape
        # ({title, description}) interchangeably inside `options`.
        for key in ("label", "title", "text", "value", "name", "question", "option"):
            v = opt.get(key)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
        # No known text key — fall back to the single string value if unambiguous.
        str_vals = [v.strip() for v in opt.values() if isinstance(v, str) and v.strip()]
        return [str_vals[0]] if len(str_vals) == 1 else []
    if isinstance(opt, (list, tuple)):
        out: list[str] = []
        for sub in opt:
            out.extend(_coerce_choice_label(sub, _depth + 1))
        return out
    return []


def _normalize_clarify_options(raw: object) -> list[str]:
    """Flatten a raw `options` value into clean, de-duplicated label strings.
    Empty / unrecoverable options are dropped — a `choice` left with too few
    real options is downgraded to free text by the caller (no blank radios)."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for opt in raw:
        for label in _coerce_choice_label(opt):
            if label not in seen:
                seen.add(label)
                out.append(label)
    return out


_QUESTIONS_V2_REQUIRED_OPTIONS = (
    "Explore a few options",
    "Decide for me",
    "Other",
)


def _normalize_questions_v2_options(raw: object) -> list[str]:
    options = _normalize_clarify_options(raw)
    seen = {o.casefold() for o in options}
    for required in _QUESTIONS_V2_REQUIRED_OPTIONS:
        if required.casefold() not in seen:
            options.append(required)
            seen.add(required.casefold())
    return options


def _questions_v2_used_since_last_plan(events: list[Event]) -> bool:
    from ..events import QuestionsV2Event

    for event in reversed(events):
        if isinstance(event, PlanEvent):
            return False
        if isinstance(event, QuestionsV2Event):
            return True
    return False


# WALK-19 — the corrective reminder injected when the no-progress breaker first
# trips. Failure-independent: the model's edits are "succeeding" but the app is
# unchanged, so the message reframes toward a hypothesis + a DIFFERENT outcome.
_NO_PROGRESS_REMINDER = (
    "<system-reminder>\n"
    "Your recent edits keep applying successfully but the app/verify outcome "
    "has NOT changed across several different attempts — you are likely editing "
    "code that does not affect what you're observing (wrong file, wrong layer, a "
    "cached build, or the symptom has a different root cause). STOP making more "
    "varied edits. In the SAME turn as your next tool call, state a one-line "
    "hypothesis for WHY the outcome is unchanged — the call itself must be a "
    "DIFFERENT diagnostic step (read the actual served output / console errors, "
    "check the build is rebuilding, or inspect a different layer), not another "
    "edit. Stating the hypothesis without a tool call does not count. If you "
    "cannot make the observed result change, call `finish` and state what is "
    "blocked.\n"
    "</system-reminder>"
)

_BLOCKED_LANDING_META_KEY = "blocked_landing"
_BLOCKED_DETAIL_PREFIX = "blocked:"
_ACTIONLESS_AUTO_RESUME_MARKER = "AUTO-RESUME-ONCE(actionless)"
_ACTIONLESS_AUTO_RESUME_SEGMENT_CAP = 3


def _no_progress_marker_seq(events: list[Event]) -> int | None:
    """Seq of the most recent `no_progress` marker since the last USER message,
    else None — mirrors `signals.stuck_escape_seq`. One nudge per user turn;
    a second no-progress trip after the model acted on the nudge halts."""
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "no_progress":
            return e.seq
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return None
    return None


def _canonical_deployment_url(raw: str) -> str:
    """Return a served `deployment_url` ONLY when it is reachable from OUTSIDE the
    sandbox; otherwise return "".

    F3 (live build stress-test): when the agent serves a site it reports the
    address it bound INSIDE the sandbox (observed: ``http://127.0.0.1:8000/``).
    That loopback/private address is reachable only from inside the sandbox —
    never from the host browser, the operator, or the verifier. Worse, :8000 also
    happens to be the Disco agent-server's own port, so following the URL verbatim
    hits the wrong server. Surfacing it as the deliverable's "Deployed" URL is a
    false affordance.

    The honest, host-reachable address is the preview-proxy route
    (``/conversations/{cid}/preview-app/``), which every consumer already falls
    back to when there is no canonical URL (frontend DeliverablePanel/PreviewPane,
    operator `view`, the verify runner's URL-less app branch). So we keep the URL
    only for an absolute http(s) URL whose host is publicly reachable (a real
    deploy target / tunnel hostname or a global IP); a loopback / unspecified /
    private / link-local / reserved host is dropped to "".
    """
    url = (raw or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname
    if host.lower() in {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }:
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A real DNS hostname (deploy target / tunnel) — keep it.
        return url
    # A bare IP literal: keep only a globally-routable (public) address.
    return url if ip.is_global else ""


class Valve:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def _blocked_meta(
        self,
        *,
        reason: str,
        legacy_status: ConversationStatus,
        legacy_detail: str | None,
    ) -> dict[str, str | bool]:
        meta: dict[str, str | bool] = {
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
            guidance.strip()
            or "the run stopped before it could safely complete the current task."
        )
        return (
            f"I'm blocked because {reason}. Where it stands: {where} "
            "What I tried: I continued the current plan until the loop breaker "
            "stopped the run. What should I do next?"
        )

    @staticmethod
    def _ensure_question(content: str, fallback: str) -> str:
        text = content.strip()
        if not text:
            return fallback
        if "?" in text:
            return text
        if len(text.split()) < 8:
            return fallback
        question = "What should I do next?"
        return f"{text}\n\n{question}"

    async def _blocked_model_message(self, *, reason: str, guidance: str) -> str:
        fallback = self._fallback_blocked_message(reason=reason, guidance=guidance)
        try:
            events = await self._loop._events()
            view = await self._loop._materialize_view(events)
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
            return self._ensure_question(question or step.thought, fallback)
        return self._ensure_question(step.thought, fallback)

    async def land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
        required_explanation: str = "",
    ) -> None:
        """Explain a breaker halt and park at the free-form user-question gate.

        The old PAUSED/STUCK detail is retained in event metadata for analytics,
        while StatusEvent.detail stays the pending question id required by the
        existing AskPanel reconstruction path.
        """
        clean_reason = (reason or legacy_detail or legacy_status.value).strip()
        meta = self._blocked_meta(
            reason=clean_reason,
            legacy_status=legacy_status,
            legacy_detail=legacy_detail,
        )
        # COUNTER MARKER: the ladder/valve signals (actionless pause counts,
        # stuck-escape scans, REL-RC-P) key on the legacy status events. Emit the
        # legacy status FIRST so every counter keeps working, then supersede it
        # with the explanation + landing below (latest-status readers see the
        # landing; counters see the marker). Autonomous flavor re-lands the
        # legacy status at the END as its terminal state — skip the duplicate.
        if not getattr(self._loop, "_autonomous", False):
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
        content = await self._blocked_model_message(
            reason=clean_reason,
            guidance=guidance,
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
        if getattr(self._loop, "_autonomous", False):
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
        await self._loop._emit(
            StatusEvent(status=status, detail=detail, meta=landing_meta)
        )

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
            isinstance(event, StatusEvent) and event.detail == "plan_approved"
            for event in events
        )

    def _actionless_ladder_still_front(self, events: list[Event]) -> bool:
        """True when autonomous supervision can still consume PAUSED(actionless)."""
        if not self._loop._autonomous or self._loop.mode == OperatingMode.PLANNING:
            return False
        if not self._has_plan_approval(events):
            return False
        if (
            self._actionless_auto_resume_total(events)
            >= _ACTIONLESS_AUTO_RESUME_SEGMENT_CAP
        ):
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
        await self._loop._emit(
            StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
        )
        return True

    async def actionless_valve(self, events: list[Event], noops: int) -> bool:
        """Shared circuit-breaker ladder for steps that consumed a model turn
        without doing real work — the tool-less noop path AND the non-blocking
        intercepts (notify_user / remember / serve). Returns True when the run
        was landed (PAUSED/FINISHED) — the caller must
        `return await self.get_state()`. Emits the warning nudge in place and
        returns False otherwise.

        The Phase-B re-run (2026-06-10) is why the intercepts must share this:
        their bare `continue` skipped the noop bookkeeping entirely, so ~50
        serve spams and ~20 prose messages sailed past every DC-05a cap until
        the stuck detector fired ~95 events later."""
        incomplete, _ = signals.plan_is_incomplete(events)
        # BW-01 — while a plan REVISION is pending (the build re-entered PLANNING
        # via request_plan and has NOT been re-approved), every terminal decision
        # below would key off the STALE prior-revision plan (still marked complete)
        # and force-FINISH a build the user is actively re-planning. Suppress the
        # stale-plan FINISH decisions for this turn — both the completed_via_notify
        # done-build finish (3 no-ops) AND the generic noop_limit→FINISHED (6) — so
        # a pending re-plan can never read as a COMPLETED build. At the 3-no-op cap
        # the run falls through to the plan-nudge; at the 6-no-op CEILING it does
        # NOT fall through to an infinite nudge (which would HANG) — it emits a
        # NON-FINISH PAUSED(actionless) halt instead (see below). Releases
        # automatically once the revised plan is approved (then plan_approved.seq >
        # planning.seq → this is False again and finish is allowed).
        pending_revision = signals.in_planning_for_revision(events)
        # BW-01 follow-up 2 — bounded halt for a pending re-plan that spins in
        # PLANNING on tool-less turns. The ceiling branch below keys off the noop
        # counter, but a BLANK tool-less planning turn moves NEITHER
        # consecutive_noops NOR _invisible_steps (the planning-mode gate emits only
        # the ENVIRONMENT plan-nudge and never bumps the invisible-step counter),
        # so a model emitting prose/blank/nothing in planning racks up turns while
        # `noops` stays 0 → the run hangs (codex P1). This stateless, noop-
        # INDEPENDENT bound counts the planning turns since the latest `planning`
        # marker (no later PlanEvent/plan_approved) and HALTS at the same ceiling
        # the noop ladder uses — a NON-FINISH PAUSED(actionless), never a stale-plan
        # FINISH. It is gated on `pending_revision` so a normal first build is
        # untouched, and releases automatically once a revised plan is submitted/
        # approved (then planning_turns_since_replan == 0 again).
        if (
            pending_revision
            and signals.planning_turns_since_replan(events)
            >= self._loop._max_consecutive_noops
        ):
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
        if noops >= self._loop._ACTIONLESS_BREAK_CAP:
            # B5 — completion BEFORE pause. A model that signals "done" via
            # notify_user (×N) instead of finish() trips the actionless valve.
            # If the build's definition-of-done is actually met (a plan exists
            # and EVERY step is marked done), the run is finished — land a clean
            # FINISHED rather than PAUSED so a completed build doesn't read as
            # paused. Conservative: `plan_steps_complete` is False for the
            # no-plan / partially-done cases, so a genuine stall still PAUSES
            # below (the thrash guard is untouched). AND require real productive
            # work since approval — marking every step done (plan_step OR the
            # declarative update_plan_progress) + spamming notify_user must NOT
            # FINISH an empty workspace; a "done" plan with zero state-changing
            # actions is a hallucinated completion, not a build.
            if (
                not pending_revision
                and signals.plan_steps_complete(events)
                and signals.productive_actions_since_approval(events) > 0
            ):
                # W-32 — DO NOT force-finish directly. The old code emitted
                # StatusEvent(FINISHED) here, BYPASSING every gate a real
                # `finish()` must clear (execution-nudge, browser-verify, DoD).
                # That let an UNVERIFIED "done" — signaled by notify_user-spam,
                # with mid-action narration ("I need to press Enter to submit
                # the Terminal command…") still in flight — land a FINISHED
                # chip. Route through the SAME gate machinery a finish() call
                # uses: only land FINISHED if they all pass; otherwise the gate
                # injected its own "verify then finish" reminder and we CONTINUE
                # (return False) — never PAUSE/STUCK — so the model verifies.
                if await self._completed_via_notify_finish(events):
                    return True
                return False
            # Bug 6 — HONEST unverifiable-static finish at the actionless valve. The
            # finish-gate honest path (finish.py) only runs when the model REACHES the
            # finish gate; here the model churns on an unsatisfiable browser-verify plan
            # step on a browserless backend and would otherwise PAUSE actionless. When
            # the build is substantively complete (a delivered index.html + a passing
            # non-browser validation, only verify-only steps remaining) and browser
            # verification is GENUINELY unavailable with NO real web-failure evidence,
            # FINISH honestly instead of pausing. Every other case (missing/broken
            # deliverable, real failure, non-verify work remaining, zero productive
            # work) returns False and falls through to the pause/stuck ladder below —
            # so W-45 and APPROVE_PLAN_NO_EXECUTION are preserved. Suppressed during a
            # pending re-plan (BW-01) like the completion branch above.
            if (
                not pending_revision
                and await self._loop._finish.maybe_honest_unverifiable_static_actionless_finish(
                    events
                )
            ):
                return True
            if incomplete:
                # M3 / REL-RC-P — the first actionless pause remains the useful
                # parked stop. The second is persisted as another
                # PAUSED(actionless), then immediately routes through the existing
                # synthetic-finish valve when the event-derived counter says the
                # repeated-pause finish conditions are met. This replaces the old
                # in-loop-memory STUCK(actionless_loop) branch, which reset across
                # resume/recreate and prevented REL-RC-P from ever seeing pause #2.
                return await self._pause_actionless(
                    "The agent produced 3 consecutive responses"
                    " without doing any real work while plan steps"
                    " remain undone — pausing instead of burning"
                    " tokens. Resume to continue."
                )

        if noops >= self._loop._max_consecutive_noops:
            if pending_revision:
                # BW-01 follow-up — the original fix suppressed the stale-plan
                # FINISH here, but `in_planning_for_revision` is True for ANY
                # pending plan (a re-plan, the first plan, or a no-PlanEvent run)
                # where the model never submits — so simply skipping this terminal
                # let the build HANG on an infinite nudge (no FINISH, no halt).
                # Refinement: still never FINISH off a stale plan, but emit a
                # NON-FINISH HALT at the ceiling so the run can't spin forever —
                # PAUSED(actionless), matching the 3-no-op actionless pause, so the
                # user can resume/redirect. Releases automatically once the revised
                # plan is approved (then this is False again and finish is allowed).
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
            # The model is spinning without acting and won't stop — end
            # cleanly rather than burn. (A real run resumes on a user steer;
            # the prompt steers toward finish/act.)
            actions_since = signals.actions_since_last_resume(events)
            if incomplete and actions_since == 0:
                await self.land_blocked(
                    reason="noop_limit",
                    guidance=(
                        "Plan steps remain undone and no real work happened in "
                        "this run segment."
                    ),
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

        if noops == self._loop._max_consecutive_noops - 1:
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
        disp = await finish.run_finish_verify_gates(finish_step, events)
        if disp is Disp.CONTINUE:
            return False
        if disp is Disp.HALT:
            # W-45: a verify gate halted the run STUCK (repeated same-fingerprint
            # failure with no progress) and emitted the terminal status itself.
            return True
        # (3) external Definition-of-Done gate (no spec → no-op pass-through).
        if not await finish.finish_dod_gate_passed():
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
        events = await self._loop._events()
        noops = signals.consecutive_noops(events) + self._loop._invisible_steps
        if await self.actionless_valve(events, noops):
            return Disp.HALT
        return Disp.CONTINUE

    async def refuse_fresh_session(self, step: AgentStep, msg: str) -> Disp:
        """Shared fresh-session refusal: a virtual tool (remember / serve / ask /
        propose_plan_update) was called before any real action this session.
        Count the invisible step, surface actionable feedback, and route through
        the actionless valve. Returns HALT if the valve trips, else CONTINUE."""
        self._loop._invisible_steps += 1
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=msg),
            )
        )
        return await self.post_noop_valve()

    async def gate_f4_bootstrap(self, events: list[Event]) -> list[Event]:
        # F4 — gated bootstrap observation. On the first model turn
        # of a session (no real actions yet, in execution mode) and
        # with assist=ON, emit a one-shot <system-reminder> listing
        # the build/test/entry commands detected from workspace
        # manifests (package.json, pyproject.toml, Makefile,
        # Cargo.toml, go.mod). The observation is gated THREE ways:
        #
        #   * self._assist — capable-model default keeps the path
        #     byte-identical to today (the function below is never
        #     called);
        #   * not self._bootstrap_emitted — fires EXACTLY once per
        #     conversation; a resume/steer is a clean slate for the
        #     agent's state but the model already saw the bootstrap
        #     in turn 1, so re-emitting it would be a redundant
        #     context cost;
        #   * _actions_since_last_resume(events) == 0 — turn-1
        #     detection. Same predicate `fresh_session` uses a few
        #     lines down. If the model has already taken a real
        #     action we are past turn 1 and the hint is no longer
        #     load-bearing.
        #
        # We re-poll events after the emit so the (d) view
        # materialization below includes the bootstrap on the
        # very first step. The detector itself is a pure function
        # over manifest files; it never raises, never blocks.
        if (
            self._loop._assist
            and not self._loop._bootstrap_emitted
            and self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
        ):
            _sbx = getattr(self._loop.executor, "sandbox", None)
            _workspace = (
                getattr(_sbx, "workspace_path", None) if _sbx is not None else None
            )
            _bootstrap = (
                _detect_project_bootstrap(_workspace) if _workspace else None
            )
            if _bootstrap:
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=_bootstrap),
                    )
                )
                events = await self._loop._events()  # include the bootstrap in (d)'s view
            # Mark fired regardless of whether anything was detected
            # (an empty workspace is a valid one-time fact too — the
            # detector ran, there was nothing to surface, we don't
            # re-run on later turns).
            self._loop._bootstrap_emitted = True
        return events

    async def gate_stuck(self, events: list[Event]) -> Disp:
        # (c) stuck detection BEFORE more work (§6). ESCAPE-then-halt: a
        # repeating action→error loop first gets ONE reframe attempt (a strong
        # "stop repeating, try a different approach" reminder + a temperature
        # bump to break the self-imitation chain) BEFORE we declare STUCK. Only
        # if it repeats AGAIN after acting on the reframe do we halt — so a
        # transient rut doesn't dead-end a run the model could escape.
        escape_seq = signals.stuck_escape_seq(events)
        acted_since_escape = escape_seq is not None and any(
            isinstance(e, ActionEvent) and e.seq is not None and e.seq > escape_seq
            for e in events
        )
        stuck_result = self._loop._stuck.evaluate(self._loop._recent(events))
        directive = stuck_result.rewrite_directive
        if directive is not None and not _rewrite_directive_marker_active(
            events, directive.path
        ):
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"rewrite_directive:{directive.path}",
                )
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            f"You have made {directive.failures} failed patch attempts "
                            f"on `{directive.path}`. Stop patching it line-by-line. "
                            "Rewrite the ENTIRE file cleanly in one `file_write` call "
                            "(write the full corrected content), then re-verify."
                        ),
                    ),
                )
            )
            return Disp.CONTINUE
        if stuck_result.is_stuck:
            if escape_seq is None:
                # First time in this user turn: drop a `stuck_escape` MARKER
                # (a status event) AND inject a C7 escape reminder (from the
                # rotating pool, with a per-attempt serialization nonce) so
                # the model's next step sees fresh anti-imitation text —
                # the harness-doesn't-nudge rule (c97c1b3) is preserved by
                # keeping the reminder INSIDE this escape branch (no
                # reminder outside the existing stuck-escape path). The
                # attempt count is the count of escape markers emitted
                # BEFORE this one (so attempt 0 → pool[0], attempt 1 →
                # pool[1], etc., rotating modulo len(pool) on later
                # escapes within the same conversation). The next step's
                # temperature is bumped (escape_temp below) — the reminder
                # is the in-context half, the temperature is the
                # sampling-variance half, and together they break the
                # self-imitation chain the way neither could alone.
                attempt = signals.stuck_escape_attempt_count(events)
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=_stuck_escape_reminder(attempt),
                        ),
                    )
                )
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape")
                )
                return Disp.CONTINUE
            if acted_since_escape:
                # The high-temp retry happened and it's STILL stuck → halt now.
                # W-31: NAME the breaker that fired (`detail`) so logs/UI don't
                # surface an undifferentiated STUCK — every sibling gate stamps a
                # detail (stuck_escape / recovery_requested / …); match that.
                detail = stuck_result.reason or "stuck"
                await self.land_blocked(
                    reason=detail,
                    guidance=(
                        "The stuck detector fired again after the one allowed "
                        "stuck-escape retry."
                    ),
                    legacy_status=ConversationStatus.STUCK,
                    legacy_detail=detail,
                )
                return Disp.HALT
            # else: escape just marked, model hasn't retried yet → fall through
            # and let it act this iteration (with the bumped temperature below).
        return Disp.FALLTHROUGH

    async def gate_fresh_read_autoground(self, events: list[Event]) -> Disp:
        """[REL-RC-B] Break a FRESH_READ_REQUIRED edit loop deterministically. In a revision the
        model edits a file it built in a prior turn; the file was un-grounded by that write and is
        too large to be re-grounded by the (truncated) workspace-snapshot pin, so the read-before-
        write gate refuses every edit and the model loops without re-reading → circuit breaker →
        STUCK. On the 2nd same-path FRESH_READ_REQUIRED (and only ONCE per path per revision — the
        durable `auto_ground_read:{path}` StatusEvent marker survives the injected read's own success
        Observation), the HARNESS injects ONE REAL file_read of that path. A genuine read sets
        read_since_write + records the sha AND puts the current bytes before the model, so the next
        edit is grounded for real and targets real text — preserving the read-before-write safety
        (it satisfies the contract by ACTUALLY reading) for every genuinely-unread file. A file that
        STILL fails after one real read (truly huge/un-coverable) falls through to gate_circuit_
        breaker and STUCKs cleanly — the marker prevents re-arming. Runs BEFORE the circuit breaker."""
        target = signals.fresh_read_autoground_target(events)
        if target is None:
            return Disp.FALLTHROUGH
        # Durable semantic sentinel (NOT volatile ActionEvent.meta): one auto-read per path/revision.
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING, detail=f"auto_ground_read:{target}"
            )
        )
        # Emit the ActionEvent FIRST (execute_and_observe requires it already on the log for strict
        # action/observation tool-pairing), then run the REAL file_read → it emits the paired Obs.
        action = ActionEvent(
            thought=(
                f"(auto-ground) The read-before-write gate has refused edits to {target} twice; "
                "reading its current content so the next edit is grounded against real text."
            ),
            tool_call=ToolCall(tool_name="file_read", arguments={"path": target}),
        )
        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        return Disp.CONTINUE

    async def gate_circuit_breaker(self, events: list[Event]) -> Disp:
        # (c.2) CIRCUIT BREAKER (Cluster 2). StuckDetector only catches
        # IDENTICAL action→error repeats; a model that tries N DIFFERENT
        # things that all fail would otherwise grind to max_iterations.
        # After `_circuit_breaker_threshold` consecutive failures, if the
        # model hasn't itself escalated (it would have halted at a gate
        # already), the HARNESS hands off to the user instead of grinding:
        # it halts at AWAITING_USER_DECISION with a summary of what failed.
        # The user's next message resets the streak (see _count_recent_failures).
        fails = signals.count_recent_failures(events)
        recent_errors = [
            e.error for e in reversed(events) if isinstance(e, AgentErrorEvent)
        ][:fails]
        recovery_requested = signals.recovery_requested_since_reset(events)
        if fails >= self._loop._circuit_breaker_threshold and not recovery_requested:
            # D2 — FIRST time we hit the wall: don't dump a dead-end message.
            # Ask the agent to DIAGNOSE the failures and propose 2-3 CONCRETE
            # recovery options via `ask_user` (model-generated, runnable, from
            # its own failure context — not a canned list). The marker
            # (StatusEvent detail) guards against re-asking before it steps; the
            # NEXT iteration falls through so the agent actually proposes.
            errs = "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
            if self._loop._autonomous:
                cb_content = (
                    "<system-reminder>\n"
                    f"You've failed {fails} times in a row:\n{errs}\n\n"
                    "STOP repeating the same approach — no human is available to "
                    "help. Your NEXT tool call must be a DIFFERENT technical path "
                    "(a different library, API, command, or algorithm); put your "
                    "one-sentence diagnosis in that same turn. Diagnosing or "
                    "narrating without the new tool call does not count. If it is "
                    "genuinely impossible, call `finish` and state clearly in the "
                    "summary what is blocked and why.\n"
                    "</system-reminder>"
                )
            else:
                cb_content = (
                    "<system-reminder>\n"
                    f"You've failed {fails} times in a row:\n{errs}\n\n"
                    "STOP retrying blindly. Call `ask_user` NOW with: a "
                    "1–2 sentence DIAGNOSIS of what is actually blocking you "
                    "as the `question`, and 2–3 concrete recovery `options`, "
                    "each a SPECIFIC tool action that CHANGES the approach "
                    "(not a repeat of what just failed). The user will pick "
                    "one — or let you continue.\n"
                    "</system-reminder>"
                )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=cb_content),
                )
            )
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING, detail="recovery_requested"
                )
            )
            return Disp.CONTINUE
        if fails > self._loop._circuit_breaker_threshold:
            if self._loop._autonomous:
                # No human to hand off to → clean forfeit (STUCK), not an
                # indefinite AWAITING_USER_DECISION stall. Bounded: we only
                # reach here after the threshold + a failed recovery attempt,
                # so this is NOT an infinite-continue token burn. The failure
                # stays visible (STUCK + the error note) for a later human.
                await self.land_blocked(
                    reason="circuit_breaker",
                    guidance=(
                        f"Autonomous run hit {fails} consecutive failures after "
                        "a recovery attempt. Recent errors:\n"
                        + "\n".join(f"  - {err[:200]}" for err in recent_errors[:4])
                    ),
                    legacy_status=ConversationStatus.STUCK,
                    legacy_detail="circuit_breaker",
                )
                return Disp.HALT
            # Recovery was already requested AND it failed again → hand off. The
            # model never volunteered clickable options, so the HARNESS now
            # SYNTHESIZES an AlternativesEvent (failure summary + the structural
            # "Continue anyway" / steer escapes) so the UI renders the recovery
            # GATE — not just dead-end prose. Picking continue resets the streak
            # (pick_alternative special-cases _CONTINUE_OPTION_ID); steering is
            # the manual escape. detail=the alt id so the View resolves the gate.
            summary = (
                f"I've hit {fails} failures in a row and couldn't find a way "
                "through. Pausing for your direction. The recent errors were:\n"
                + "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
            )
            failed_action_id = next(
                (e.id for e in reversed(events) if isinstance(e, ActionEvent)), ""
            )
            alt = AlternativesEvent(
                failed_action_id=failed_action_id,
                summary=summary,
                options=[
                    AlternativeOption(
                        id=_CONTINUE_OPTION_ID,
                        title="Continue anyway",
                        description=(
                            "Reset the failure streak and let the agent try another "
                            "approach with its own judgment."
                        ),
                        tool_name="",  # not a tool — the loop intercepts this id
                        arguments={},
                    )
                ],
            )
            await self._loop._emit(alt)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.AWAITING_USER_DECISION,
                    detail=alt.id,
                )
            )
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def gate_no_progress(self, events: list[Event]) -> Disp:
        # (c.4) WALK-19 — SEMANTIC no-progress breaker. Failure-INDEPENDENT and
        # NOT assist-gated: catches the "successful but useless varied edits"
        # loop (writes apply, dev server 200, no errors — the black-screen-game)
        # that every failure-keyed breaker (stuck/circuit) sails past, grinding
        # to max_iterations. Escalate-then-halt like gate_stuck: a corrective
        # nudge first, then — if the SAME outcome persists after the model acted
        # on the nudge — halt STUCK rather than burn the rest of the budget.
        recent = self._loop._recent(events)
        if not (
            repeated_verify_no_progress(recent)
            or barren_streak_no_progress(recent)
        ):
            return Disp.FALLTHROUGH
        marker_seq = _no_progress_marker_seq(events)
        if marker_seq is None:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_NO_PROGRESS_REMINDER),
                )
            )
            await self._loop._emit(
                StatusEvent(status=ConversationStatus.RUNNING, detail="no_progress")
            )
            return Disp.CONTINUE
        acted_since = any(
            isinstance(e, ActionEvent) and e.seq is not None and e.seq > marker_seq
            for e in events
        )
        if acted_since:
            # Nudged, the model made MORE varied edits, still the same symptom →
            # halt visibly (STUCK) instead of looping to the iteration ceiling.
            await self.land_blocked(
                reason="no_progress",
                guidance=(
                    "The verifier outcome stayed the same after the corrective "
                    "no-progress nudge and another edit attempt."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="no_progress",
            )
            return Disp.HALT
        # Marker present but the model hasn't acted on the nudge yet → let it act.
        return Disp.FALLTHROUGH

    async def gate_bookkeeping_streak(self, events: list[Event]) -> tuple[Disp, list[Event]]:
        # (c.3) plan_step-spam guard (issue C). A soft nudge once at the
        # streak threshold; a hard STUCK halt at the cap (the model is doing
        # nothing but shuffling the plan tracker — every other valve misses
        # this). STUCK (not a silent proceed) keeps the failure VISIBLE, which
        # matters most for weak local models. Autonomous mode turns STUCK into
        # a clean forfeit (issue A).
        _bk_streak = signals.bookkeeping_streak_len(events)
        if _bk_streak == _BOOKKEEPING_STREAK_NUDGE_AT:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            f"You've called plan-tracking tools {_bk_streak} times "
                            "in a row without doing any real work (no file edit, "
                            "shell command, or other state-changing action). Marking "
                            "steps does NOT advance the task. Take a REAL action now "
                            "to make progress, or call `finish` if the work is "
                            "already complete.\n"
                            "</system-reminder>"
                        ),
                    ),
                )
            )
            events = await self._loop._events()
        else:
            # Spam cap scales with the active plan's step count (T7 / E3):
            # a model finishing an N-step plan may legitimately emit
            # ~N `plan_step` calls (one per step) plus a couple of
            # over-corrections. Capping at N + slack lets the legit
            # burst complete; the original `_BOOKKEEPING_STREAK_HALT_AT`
            # floor still catches spam on a small/no-plan run.
            _plan_steps = signals.active_plan_step_count(events)
            _bk_halt_cap = max(
                _BOOKKEEPING_STREAK_HALT_AT,
                _plan_steps + _BOOKKEEPING_PLAN_SLACK,
            )
            if _bk_streak >= _bk_halt_cap:
                await self.land_blocked(
                    reason="bookkeeping_only",
                    guidance=(
                        "The agent kept updating the plan checklist without doing "
                        "real work such as editing files, running commands, or "
                        "otherwise changing state."
                    ),
                    legacy_status=ConversationStatus.STUCK,
                    legacy_detail="bookkeeping_only",
                )
                return Disp.HALT, events
        return Disp.FALLTHROUGH, events


class MetaToolHandlers:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def handle_notify_user(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        msg = str(step.tool_call.arguments.get("message") or "").strip() or step.thought
        if msg.strip():
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=msg),
                )
            )
        else:
            self._loop._invisible_steps += 1
        # Non-blocking, but NOT exempt from the actionless valve —
        # a bare `continue` here let prose spam bypass every cap
        # (Phase-B re-run, 2026-06-10).
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_remember(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        # Durable memory: emit a PINNED KnowledgeEvent so the fact
        # survives condensation and is re-injected into context every
        # step. Non-blocking — like notify_user, the agent keeps
        # working right after. A blank fact is ignored (no-op).
        #
        # FRESH-SESSION BACKSTOP (Phase-B re-run #6, 2026-06-11):
        # remember is withheld from the offered set until the
        # session's first real action, but a weak model can
        # hallucinate calls to unoffered tools — re-run #6's model
        # remember-spammed duplicate CSV facts right after its
        # first-move finish was refused. Unlike notify_user (which
        # degrades into the bounded prose channel), an executed
        # remember POLLUTES pinned knowledge and reads as success,
        # so the model keeps picking it. Refuse with actionable
        # feedback: pinned facts must come from THIS session's work.
        if (
            self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
        ):
            return await self._loop._valve.refuse_fresh_session(
                step,
                "remember refused: no real work has happened "
                "yet in this session. Facts worth pinning come "
                "from real observations — execute the next plan "
                "step with real tool calls first, then remember "
                "what you learned.",
            )
        fact = str(step.tool_call.arguments.get("fact") or "").strip()
        if fact:
            import hashlib
            scope = str(step.tool_call.arguments.get("scope") or "").strip()
            normalized = fact
            fact_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            seen = {
                (e.scope, hashlib.sha256(e.snippet.strip().encode("utf-8")).hexdigest())
                for e in events if isinstance(e, KnowledgeEvent)
            }
            if (scope, fact_hash) in seen:
                action = ActionEvent(
                    thought=step.thought,
                    tool_call=step.tool_call,
                    self_assessed_risk=step.self_assessed_risk,
                    llm_response_id=step.llm_response_id,
                )
                res = ToolResult(
                    call_id=step.tool_call.call_id,
                    tool_name="remember",
                    success=True,
                    content="Already recorded — not stored again.",
                )
                await self._loop._emit(action)
                await self._loop._emit(ObservationEvent(tool_result=res, action_id=action.id))
            else:
                await self._loop._emit(
                    KnowledgeEvent(source=EventSource.AGENT, scope=scope, snippet=fact)
                )
                # C5 — write-through mirror: persist the fact to
                # `<workspace>/.pmx/MEMORY.md` so the standing
                # memory survives a hard filesystem reset (a
                # box wipe / fresh backend). The in-View
                # KnowledgeEvent channel remains the
                # authoritative in-session source — `.pmx/`
                # is a write-through mirror, not a divergent
                # second store. Best-effort: a sandbox write
                # failure is logged but never blocks the
                # in-View emission (the agent still has the
                # fact in-context for THIS run).
                try:
                    await self._loop._write_pmx_memory_fact(scope, fact)
                except Exception:  # noqa: BLE001 — mirror is best-effort
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "pmx MEMORY write-through failed (in-View fact survives)",
                        exc_info=True,
                    )
        else:
            # Blank fact persists NOTHING — count it or it's an
            # unbounded silent token burn.
            self._loop._invisible_steps += 1
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def _serve_path_missing(self, path: str) -> bool:
        """CD-TOOLS-5 OUTPUT-TRUTH: True iff the deliverable path is VERIFIABLY absent in the
        workspace. Fail-OPEN (return False) when there's no sandbox or the existence check errors —
        serve is the handoff softguard, not a hard gate, and the verify gate is the real proof; an
        unverifiable check must never block a legitimate handoff."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return False
        try:
            return not await sbx.file_exists(path)
        except Exception:  # noqa: BLE001 — unverifiable → don't block the handoff
            return False

    async def handle_serve(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        # Finished-artifact HANDOFF: emit a DeliverableEvent the UI renders
        # as Open-the-app / Download-the-files. Non-blocking — the agent
        # serves, verifies, then finishes. A missing path/title or a
        # re-serve of an already-handed-off artifact is ignored (no-op)
        # rather than emitting a useless handoff — and every ignored
        # form is COUNTED, because each is invisible in the event log
        # and was the unbounded serve-spam vector (Phase-B, 2026-06-10).
        #
        # POST-RESUME SERVE GATE (Phase-B re-run #3, 2026-06-10): the
        # model's FIRST post-resume turn was serve(path=".") on an empty
        # restored workspace — a handoff with zero work behind it, which
        # then seeded a prose/noop streak into the valve. A serve is only
        # meaningful after at least one real action this session (since
        # the last resume, or since start). Refuse with ACTIONABLE
        # feedback (B4: the model must see why, or it just retries).
        if signals.actions_since_last_resume(events) == 0:
            return await self._loop._valve.refuse_fresh_session(
                step,
                "serve refused: no real work has happened yet in "
                "this session — the sandbox is fresh and nothing "
                "is running. Execute the next plan step with real "
                "tool calls (write files, run commands, start your "
                "server), then serve the result.",
            )
        if not step.tool_call.arguments:
            _LOG.debug("Skipping deliverable emission: empty payload from agent")
            self._loop._invisible_steps += 1
        else:
            title = str(step.tool_call.arguments.get("title") or "").strip()
            path = str(step.tool_call.arguments.get("path") or "").strip()
            kind = str(step.tool_call.arguments.get("kind") or "app").strip()
            # F3: a sandbox-internal/loopback serve address (e.g. 127.0.0.1:8000) is
            # NOT reachable from the host — drop it so consumers fall back to the
            # host-reachable preview-app proxy instead of a false "Deployed" link.
            url = _canonical_deployment_url(str(step.tool_call.arguments.get("url") or ""))
            if kind not in ("app", "files"):
                kind = "app"
            if not (title and path):
                self._loop._invisible_steps += 1
            elif any(
                isinstance(e, DeliverableEvent)
                and e.path == path
                and e.artifact_kind == kind
                for e in events
            ):
                # Same artifact already handed off — an identical
                # card adds nothing for the user; re-emitting it is
                # the few-shot spam prompt for the next one.
                _LOG.debug("Skipping duplicate deliverable: %s (%s)", path, kind)
                self._loop._invisible_steps += 1
            elif await self._serve_path_missing(path):
                # CD-TOOLS-5 OUTPUT-TRUTH: never hand off a deliverable whose path does not exist
                # in the workspace — that is a false "Open / Download" card for nothing. Refuse with
                # actionable feedback (counted + valve-routed) instead of emitting a fake handoff.
                # (serve is the SHOW handoff, not verification — the verify gate still proves it works.)
                return await self._loop._valve.refuse_fresh_session(
                    step,
                    f"serve refused: the deliverable path {path!r} does not exist in the "
                    "workspace yet. Create it (write the file / build the app at that path), "
                    "then serve it.",
                )
            else:
                await self._loop._emit(
                    DeliverableEvent(
                        source=EventSource.AGENT,
                        title=title,
                        path=path,
                        artifact_kind=kind,  # type: ignore[arg-type]
                        deployment_url=url,
                    )
                )
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_delegate_explore(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        # C20 — `delegate_explore`: a bounded, read-only Explore/Plan
        # helper the loop dispatches+joins. The driver calls it; the
        # loop:
        #   1. enforces the per-run-segment cap (_FANOUT_MAX_PER_RUN;
        #      past the cap → refuse with a system-reminder, no
        #      dispatch, no observation);
        #   2. emits the ActionEvent (so the audit trail sees the
        #      proposed call);
        #   3. dispatches the subagent (default: a thin deterministic
        #      stub — the test seam; production wires a real LLM
        #      round-trip with read-only tools only — see
        #      `_run_fanout` for the override hook);
        #   4. folds the subagent's response back as a paired
        #      ObservationEvent (success=True on a clean dispatch,
        #      success=False with error="cap_exceeded" on refusal)
        #      so the driver sees the result on its next turn.
        # The helper is READ-ONLY: a subagent cannot mutate
        # workspace state, cannot run shell, cannot write files
        # (enforced upstream by the tools the helper is offered —
        # file_read/file_list/search/extract; the engine also
        # cannot recurse through `delegate_explore` because the
        # cap applies to nested calls too). NON-BLOCKING: the
        # driver keeps working right after — the same shape as
        # notify_user/remember/serve (the actionless valve still
        # applies if a fan-out produces no real work).
        if self._loop._fanout_count >= self._loop._fanout_max:
            # Cap exceeded — refuse with feedback. The cap is
            # per-run-segment, so a fresh `run()` resets it.
            # We do NOT raise / halt / STUCK (this is a soft
            # "no more fan-outs this segment" gate, not a
            # stuck-detector); we emit a system-reminder +
            # ActionEvent + AgentErrorEvent (paired by
            # call_id) so the driver sees the refusal on its
            # next turn and falls back to direct tools. The
            # refusal is invisible to the actionless valve
            # (an error-paired action doesn't extend the
            # noop streak).
            action = ActionEvent(
                thought=step.thought,
                tool_call=step.tool_call,
                self_assessed_risk=step.self_assessed_risk,
                llm_response_id=step.llm_response_id,
            )
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"delegate_explore refused: the per-run-segment "
                        f"cap ({self._loop._fanout_max}) has been reached "
                        f"(used {self._loop._fanout_count}/{self._loop._fanout_max} "
                        f"this segment). Fall back to direct read-only "
                        f"tools (file_read, file_list, search, extract) "
                        f"for the rest of this run segment; a fresh run "
                        f"segment resets the budget.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=(
                        action.tool_call.call_id if action.tool_call else None
                    ),
                )
            )
            return Disp.CONTINUE
        # Under the cap → record the proposed action, dispatch
        # the helper, and fold the result back. The dispatch
        # is `await`ed so the ActionEvent and ObservationEvent
        # land in the same turn (the driver sees both on its
        # next step).
        self._loop._fanout_count += 1
        action = ActionEvent(
            thought=step.thought,
            tool_call=step.tool_call,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        await self._loop._emit(action)
        # Length-bound the helper's input BEFORE dispatching —
        # the bound is a property of the fan-out (regardless
        # of what `_run_fanout` does — a test seam, a real
        # LLM round-trip, a future override). Without this
        # bound a driver could grow the helper's prompt
        # unboundedly within a single segment; the result
        # is folded back into the View, which the condenser
        # would later have to manage.
        _fanout_args = dict(action.tool_call.arguments or {})
        _trunc_marker = "…[truncated]"
        for _k in ("question", "context"):
            _v = str(_fanout_args.get(_k) or "")
            if len(_v) > _FANOUT_INPUT_MAX_CHARS:
                _fanout_args[_k] = (
                    _v[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)]
                    + _trunc_marker
                )
        result = await self._loop._run_fanout(
            _fanout_args,
            events,
            call_id=(
                action.tool_call.call_id if action.tool_call else ""
            ),
        )
        await self._loop._emit(
            ObservationEvent(tool_result=result, action_id=action.id)
        )
        # Fan-out is non-blocking — the driver keeps working
        # right after. The actionless valve still applies if
        # the helper returned empty (a degenerate fan-out is
        # still a no-op step, like remember/serve).
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_workflow_control(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller dispatches by workflow-control name
        workflow_run = getattr(self._loop, "_workflow_run", None)
        if workflow_run is None:
            return Disp.FALLTHROUGH

        name = step.tool_call.tool_name
        args = step.tool_call.arguments or {}
        meta = {
            "workflow_control": name,
            "workflow_run_id": getattr(workflow_run, "run_id", ""),
        }
        if name == "skip":
            reason = str(args.get("reason") or "").strip() or "workflow skipped"
            await self._loop._valve.land_terminal_with_explanation(
                reason=reason,
                guidance=(
                    "The workflow control tool `skip` was called. The run is "
                    "ending honestly without producing the contracted output."
                ),
                status=ConversationStatus.FINISHED,
                detail="workflow_skipped",
                meta=meta,
            )
            return Disp.HALT

        if name == "needs_input":
            reason = str(args.get("reason") or "").strip() or "workflow needs input"
            required_action = str(args.get("required_action") or "").strip()
            if not required_action:
                required_action = "Provide the missing workflow input or approval."
            guidance = f"{reason}\nRequired action: {required_action}"
            await self._loop._valve.land_blocked(
                reason=reason,
                guidance=guidance,
                legacy_status=ConversationStatus.PAUSED,
                legacy_detail="workflow_needs_input",
                required_explanation=required_action,
            )
            return Disp.HALT

        return Disp.FALLTHROUGH

    async def handle_noop_step(self, step: AgentStep, events: list[Event]) -> Disp:
        if step.thought.strip():
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=step.thought),
                )
            )
        else:
            # Nothing persisted — invisible to every event-derived
            # detector, so the instance counter has to carry it.
            self._loop._invisible_steps += 1
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_truncated_step(self, step: AgentStep, events: list[Event]) -> Disp:
        """W-31 — the model's message was cut off mid-sentence
        (`finish_reason=="length"`) with no tool call. Persist the partial text
        as the assistant's turn (so the model sees its own fragment), then inject
        an ENVIRONMENT reminder telling it to CONTINUE from where it stopped —
        not restart. The reminder is a non-USER message, so it does NOT reset the
        stuck detector (a truncation storm still trips gate_stuck). Falls through
        the same no-progress valve as a no-op so a run can never spin forever on
        repeated truncations."""
        if step.thought.strip():
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=step.thought),
                )
            )
        else:
            # Nothing persisted — invisible to every event-derived detector, so
            # the instance counter has to carry it (same as handle_noop_step).
            self._loop._invisible_steps += 1
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Your previous message was cut off mid-sentence — it hit "
                        "the output length limit. Continue it from exactly where "
                        "it stopped; do NOT restart or repeat what you already "
                        "wrote. Be more concise this time, and if you were about "
                        "to take an action, take it now with a tool call.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def gate_ask_fresh_session(self, step: AgentStep, events: list[Event]) -> Disp:
        if (
            step.tool_call is not None
            and step.tool_call.tool_name
            in ("ask_user", "questions_v2", "clarify", "propose_plan_update")
            and self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
            # In autonomous mode, ask_user/questions_v2/clarify are owned by the headless-
            # stall guard below (a clean "no user — decide yourself" nudge);
            # don't pre-empt them here with the interactive "do work, then ask"
            # message, which tells the model it can ask when it can't. (g.5)
            # still governs propose_plan_update on a fresh session.
            and not (
                self._loop._autonomous
                and step.tool_call.tool_name in ("ask_user", "questions_v2", "clarify")
            )
        ):
            return await self._loop._valve.refuse_fresh_session(
                step,
                f"{step.tool_call.tool_name} refused: no real "
                "work has happened yet in this session. Attempt "
                "the next plan step with real tool calls first — "
                "if it fails or something is genuinely unclear, "
                "you can then ask or propose a plan change with "
                "the evidence in hand.",
            )
        return Disp.FALLTHROUGH

    async def gate_autonomous_ask_stall(self, step: AgentStep, events: list[Event]) -> Disp:
        if (
            self._loop._autonomous
            and step.tool_call is not None
            and step.tool_call.tool_name in ("ask_user", "questions_v2", "clarify")
        ):
            asked = str(
                step.tool_call.arguments.get("question") or ""
            ).strip() or step.thought.strip()
            stall_action = ActionEvent(
                thought=step.thought,
                tool_call=step.tool_call,
                self_assessed_risk=step.self_assessed_risk,
                llm_response_id=step.llm_response_id,
            )
            await self._loop._emit(stall_action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "Autonomous mode is ON — there is no user available "
                        f"to answer. `{step.tool_call.tool_name}` is "
                        "unavailable in this mode. Make the best decision you "
                        "can from the information you already have, log the "
                        "assumptions in the submit_plan.context preamble, and "
                        "continue working toward the goal."
                        + (f"\nYour question was: {asked}" if asked else "")
                        + "\n</system-reminder>"
                    ),
                    action_id=stall_action.id,
                    tool_call_id=(
                        stall_action.tool_call.call_id
                        if stall_action.tool_call
                        else None
                    ),
                )
            )
            return Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def handle_propose_plan_update(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        new_plan = self._loop._plan_from_args(step.tool_call.arguments, events)
        await self._loop._emit(new_plan)
        if self._loop._autonomous:
            # No human to approve a mid-run plan revision → auto-approve
            # inline, emitting exactly what approve_plan() would (mode flip
            # + RUNNING/plan_approved), so the event log is identical whether
            # a human or the harness approved. Without this, an autonomous
            # run that follows the loop's OWN "call propose_plan_update to
            # revise the plan" guidance (the auto-continue nudge below) would
            # halt at AWAITING_PLAN_APPROVAL forever — a headless stall.
            #
            # C8 (T11): bound the propose_plan_update loop. A weak model
            # in autonomous mode can hammer the same plan revision over
            # and over, never realizing there's no human to approve it.
            # Compare new_plan.steps to the immediately-prior plan's steps
            # (ignore summary; an appended/added step counts as DIFFERENT
            # because list lengths differ). Increment the consecutive-
            # identical counter on a match, reset on a diff. At >= the
            # cap, feed the existing bookkeeping-stuck valve (emit the
            # same `bookkeeping_only` warning + STUCK status the (c.3)
            # valve emits) — do NOT invent a new halt path. Only in
            # autonomous mode; interactive path is byte-identical.
            prior_plan: PlanEvent | None = None
            for e in reversed(events):
                if e is new_plan:
                    continue  # skip the just-emitted new_plan
                if isinstance(e, PlanEvent):
                    prior_plan = e
                    break
            if (
                prior_plan is not None
                and [s.title for s in prior_plan.steps]
                == [s.title for s in new_plan.steps]
            ):
                self._loop._identical_plan_revisions += 1
            else:
                self._loop._identical_plan_revisions = 0
            if (
                self._loop._identical_plan_revisions
                >= _PROPOSE_PLAN_UPDATE_REPEAT_CAP
            ):
                # Reuse the existing bookkeeping-stuck valve: same
                # bookkeeping breaker reason through the shared blocked lander.
                await self._loop._valve.land_blocked(
                    reason="bookkeeping_only",
                    guidance=(
                        "Autonomous mode proposed the same plan revision repeatedly "
                        "without doing real work."
                    ),
                    legacy_status=ConversationStatus.STUCK,
                    legacy_detail="bookkeeping_only",
                )
                return Disp.HALT
            self._loop.mode = self._loop._execution_mode
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING, detail="plan_approved"
                )
            )
            # C1c: arm the DoD gate (write-once → a mid-run revision's re-arm is swallowed;
            # this is the hook point where the monotonic steer-scope extension will land).
            await self._loop._arm_dod_from_plan()
            await self._loop._seed_context_from_plan()
            return Disp.CONTINUE
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                detail=new_plan.id,
            )
        )
        return Disp.HALT

    async def handle_questions_v2(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        from ..events import QuestionsV2Event as _QuestionsV2Event
        from ..events import QuestionsV2Item

        if self._loop.mode != OperatingMode.PLANNING:
            action = ActionEvent(
                thought=step.thought,
                tool_call=step.tool_call,
                self_assessed_risk=step.self_assessed_risk,
                llm_response_id=step.llm_response_id,
            )
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "questions_v2 refused: structured intake is only available "
                        "before submit_plan, while the run is still in PLANNING. "
                        "If execution is blocked on human input, use ask_user; if "
                        "the plan itself is wrong, use propose_plan_update.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=step.tool_call.call_id,
                )
            )
            return Disp.CONTINUE

        if _questions_v2_used_since_last_plan(events):
            action = ActionEvent(
                thought=step.thought,
                tool_call=step.tool_call,
                self_assessed_risk=step.self_assessed_risk,
                llm_response_id=step.llm_response_id,
            )
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "questions_v2 refused: you already used the one allowed "
                        "structured intake round for this planning pass. Do not "
                        "ask another batch before submit_plan. Use the user's "
                        "answers, choose reasonable defaults for anything still "
                        "ambiguous, and log those assumptions in submit_plan.context.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=step.tool_call.call_id,
                )
            )
            return Disp.CONTINUE

        question = str(
            step.tool_call.arguments.get("question")
            or step.tool_call.arguments.get("summary")
            or ""
        ).strip() or step.thought.strip()
        raw_items = (
            step.tool_call.arguments.get("questions")
            or step.tool_call.arguments.get("items")
            or []
        )
        source_items = raw_items if isinstance(raw_items, list) else []
        items: list[QuestionsV2Item] = []
        used_ids: set[str] = set()
        for it in source_items:
            if len(items) >= 4:
                break
            if not isinstance(it, dict):
                continue
            qtext = str(it.get("question") or it.get("label") or "").strip()
            if not qtext:
                continue
            qid = str(it.get("id") or f"q{len(items) + 1}").strip() or f"q{len(items) + 1}"
            if qid in used_ids:
                qid = f"q{len(items) + 1}"
            used_ids.add(qid)
            items.append(
                QuestionsV2Item(
                    id=qid,
                    question=qtext,
                    options=_normalize_questions_v2_options(it.get("options")),
                    allow_free_text=True,
                )
            )

        if not items:
            q_event = MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(
                    role="assistant",
                    content=(
                        question or "The agent needs clarification before planning."
                    ),
                ),
            )
            await self._loop._emit(q_event)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.AWAITING_USER_QUESTION,
                    detail=q_event.id,
                )
            )
            return Disp.HALT

        intake_event = _QuestionsV2Event(
            question=question or "A few details before I propose a plan.",
            items=items,
        )
        await self._loop._emit(intake_event)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=intake_event.id,
            )
        )
        return Disp.HALT

    async def handle_clarify(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        # clarify → ClarifyEvent with typed questions. The planner
        # calls this when SEVERAL specifics are missing and
        # guessing would produce a bad plan. The loop emits a
        # ClarifyEvent (carrying the structured question items)
        # and halts at AWAITING_USER_QUESTION. The user answers
        # each question; the answers are re-injected as a user
        # message that resumes planning.
        from ..events import ClarifyEvent as _ClarifyEvent
        from ..events import ClarifyQuestionItem

        question = str(
            step.tool_call.arguments.get("question") or ""
        ).strip() or step.thought.strip()
        raw_items = step.tool_call.arguments.get("questions") or []
        items: list[ClarifyQuestionItem] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            qid = str(it.get("id") or f"q{len(items) + 1}").strip()
            qtext = str(it.get("question") or "").strip()
            if not qtext:
                continue
            qtype = str(it.get("type") or "short_text").strip()
            if qtype not in ("short_text", "long_text", "choice"):
                qtype = "short_text"
            # Normalize options robustly — the model emits choice options as
            # nested lists / {id,question} dicts / empty placeholders, NOT the
            # flat strings the schema asks for. `str(o)` rendered those as
            # garbage radio labels (malformed) or blank radios (empty cards).
            qopts = _normalize_clarify_options(it.get("options"))
            # A `choice` with fewer than two real options can't be a meaningful
            # pick — render a free-text box instead of one/zero blank radios so
            # the user always has something usable to answer with.
            if qtype == "choice" and len(qopts) < 2:
                qtype = "short_text"
                qopts = []
            items.append(
                ClarifyQuestionItem(
                    id=qid, question=qtext, type=qtype, options=qopts
                )
            )
        if not items:
            # No valid questions → fall back to free-form ask_user
            q_event = MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(
                    role="assistant",
                    content=(
                        question or "The agent needs clarification before planning."
                    ),
                ),
            )
            await self._loop._emit(q_event)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.AWAITING_USER_QUESTION,
                    detail=q_event.id,
                )
            )
            return Disp.HALT
        clarify_event = _ClarifyEvent(
            question=question or "The agent needs clarification before planning.",
            items=items,
        )
        await self._loop._emit(clarify_event)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=clarify_event.id,
            )
        )
        return Disp.HALT

    async def handle_ask_user(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        alt = self._loop._alternatives_from_args(step.tool_call.arguments, events)
        if alt is not None:
            # ask_user WITH options → AlternativesEvent + gate
            await self._loop._emit(alt)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.AWAITING_USER_DECISION,
                    detail=alt.id,
                )
            )
            return Disp.HALT
        # ask_user WITHOUT options → free-form question. Emit it as
        # an assistant message (from the tool's `question` arg or
        # the step's thought, whichever has content) and halt at the
        # two-way Ask-gate (AWAITING_USER_QUESTION) — the UI renders
        # an AskPanel with a focused answer box, not muted prose. The
        # status's detail carries the question message's id so the
        # surface can resolve it. The user's reply (send_message /
        # steer) IS the resume signal.
        question = str(
            step.tool_call.arguments.get("question") or ""
        ).strip() or step.thought.strip()
        question_id: str | None = None
        if question:
            q_event = MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=question),
            )
            question_id = q_event.id
            await self._loop._emit(q_event)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=question_id or "free_form_question",
            )
        )
        return Disp.HALT
