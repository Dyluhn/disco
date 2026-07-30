"""Internal Loop turn-control collaborator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    AlternativeOption,
    AlternativesEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    ToolCall,
)
from ..llm import OperatingMode
from . import signals
from .bootstrap import _detect_project_bootstrap
from .boundaries import AgentStep
from .control import Disp
from .messages import _stuck_escape_reminder
from .planning_harvest import harvest_revision_plan_after_refusal
from .stuck import (
    RewriteDirective,
    StuckResult,
    VerifierEvidenceInvalid,
    VerifierFailureNoProgress,
    no_progress_detected,
    repeated_failed_verifier_no_progress,
)
from .turn_control_support import (
    _BOOKKEEPING_PLAN_SLACK,
    _BOOKKEEPING_STREAK_HALT_AT,
    _BOOKKEEPING_STREAK_NUDGE_AT,
    _CONTINUE_OPTION_ID,
    _NO_PROGRESS_REMINDER,
    _STUCK_ESCAPE_BLOCKED_TOOLS_BY_REASON,
    _VERIFIER_EVIDENCE_INVALID_DETAIL,
    _VERIFIER_NO_PROGRESS_DETAIL,
    _last_verify_web_app_passed,
    _no_progress_finish_hinted,
    _no_progress_marker_seq,
    _rewrite_directive_marker_active,
    _verifier_no_progress_marker_seq,
)
from .valve_landing import _ValveHost

if TYPE_CHECKING:
    pass

_LOG = logging.getLogger("disco.loop")


class PhaseGateMixin(_ValveHost):
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

    async def refresh_f4_bootstrap(self, events: list[Event]) -> list[Event]:
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
            _workspace = getattr(_sbx, "workspace_path", None) if _sbx is not None else None
            _bootstrap = _detect_project_bootstrap(_workspace) if _workspace else None
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

    async def _emit_rewrite_directive(
        self,
        events: list[Event],
        directive: RewriteDirective,
    ) -> bool:
        if _rewrite_directive_marker_active(events, directive.path):
            return False
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
        return True

    async def _emit_stuck_escape(
        self,
        events: list[Event],
        stuck_result: StuckResult,
    ) -> None:
        attempt = signals.stuck_escape_attempt_count(events)
        blocked_tools = _STUCK_ESCAPE_BLOCKED_TOOLS_BY_REASON.get(
            stuck_result.reason or "", frozenset()
        )
        reminder_meta = {"blocking": f"stuck_escape:{stuck_result.reason}"} if blocked_tools else {}
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=_stuck_escape_reminder(attempt),
                ),
                meta=reminder_meta,
            )
        )
        for tool_name in sorted(blocked_tools):
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"{signals.STUCK_ESCAPE_BLOCK_DETAIL_PREFIX}{tool_name}",
                )
            )
        await self._loop._emit(
            StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape")
        )

    async def _land_repeated_stuck(
        self,
        events: list[Event],
        stuck_result: StuckResult,
    ) -> Disp:
        if signals.in_planning_for_revision(events) and not signals.revision_force_submit(events):
            harvested = await harvest_revision_plan_after_refusal(self._loop)
            if harvested is not None:
                return harvested
        detail = stuck_result.reason or "stuck"
        await self.land_blocked(
            reason=detail,
            guidance="The stuck detector fired again after the one allowed stuck-escape retry.",
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=detail,
        )
        return Disp.HALT

    async def gate_stuck(self, events: list[Event]) -> Disp:
        """Escalate a detected loop once, then land a repeated stuck episode."""
        escape_seq = signals.stuck_escape_seq(events)
        acted_since_escape = escape_seq is not None and any(
            isinstance(event, ActionEvent) and event.seq is not None and event.seq > escape_seq
            for event in events
        )
        stuck_result = self._loop._stuck.evaluate(self._loop._recent(events))
        directive = stuck_result.rewrite_directive
        if directive is not None and await self._emit_rewrite_directive(events, directive):
            return Disp.CONTINUE
        if not stuck_result.is_stuck:
            return Disp.FALLTHROUGH
        if escape_seq is None:
            await self._emit_stuck_escape(events, stuck_result)
            return Disp.CONTINUE
        if acted_since_escape:
            return await self._land_repeated_stuck(events, stuck_result)
        return Disp.FALLTHROUGH

    async def gate_fresh_read_autoground(self, events: list[Event]) -> Disp:
        """[REL-RC-B] Break a FRESH_READ_REQUIRED edit loop deterministically. In a revision the
        model edits a file it built in a prior turn; the file was un-grounded by that write and is
        too large to be re-grounded by the (truncated) workspace-snapshot pin, so the read-before-
        write gate refuses every edit and the model loops without re-reading → circuit breaker →
        STUCK. On the 2nd same-path FRESH_READ_REQUIRED (and only ONCE per path per
        revision — the durable `auto_ground_read:{path}` StatusEvent marker survives
        the injected read's own success Observation), the HARNESS injects ONE REAL
        file_read of that path. A genuine read sets
        read_since_write + records the sha AND puts the current bytes before the model, so the next
        edit is grounded for real and targets real text — preserving the read-before-write safety
        (it satisfies the contract by ACTUALLY reading) for every genuinely-unread file. A file that
        STILL fails after one real read (truly huge/un-coverable) falls through to
        gate_circuit_breaker and STUCKs cleanly — the marker prevents re-arming. Runs
        BEFORE the circuit breaker."""
        target = signals.fresh_read_autoground_target(events)
        if target is None:
            return Disp.FALLTHROUGH
        # Durable semantic sentinel (NOT volatile ActionEvent.meta): one auto-read per
        # path/revision.
        await self._loop._emit(
            StatusEvent(status=ConversationStatus.RUNNING, detail=f"auto_ground_read:{target}")
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
        action = cast(ActionEvent, await self._loop._emit(action))
        await self._loop._execute_and_observe(action)
        return Disp.CONTINUE


class ProgressGateMixin(_ValveHost):
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
        recent_errors = [e.error for e in reversed(events) if isinstance(e, AgentErrorEvent)][
            :fails
        ]
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
                    "narrating without the new tool call does not count. If the "
                    "SAME error keeps recurring, use the `search`/`extract` tools "
                    "to look it up before retrying, and consider whether the "
                    "blocker is the ENVIRONMENT (sandbox / network / a missing "
                    "tool) rather than your code — if so, work AROUND it. If it is "
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
                    "(not a repeat of what just failed). If the SAME error keeps "
                    "recurring, one option should be to `search`/`extract` the "
                    "exact error or API; and weigh whether the blocker is the "
                    "ENVIRONMENT (sandbox / network / a missing tool) rather than "
                    "your code — if so, work AROUND it. The user will pick "
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
                StatusEvent(status=ConversationStatus.RUNNING, detail="recovery_requested")
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

    async def _land_invalid_verifier_evidence(
        self,
        finding: VerifierEvidenceInvalid,
    ) -> Disp:
        guidance = (
            f"{finding.tool_name} executed but returned unusable structured "
            f"verification evidence ({finding.reason}) at event "
            f"{finding.verdict_seq}. A product verdict cannot be trusted "
            "without an explicit boolean `passed` value and, for a failure, a "
            "non-empty failure fingerprint."
        )
        await self.land_blocked(
            reason=_VERIFIER_EVIDENCE_INVALID_DETAIL,
            guidance=guidance,
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=_VERIFIER_EVIDENCE_INVALID_DETAIL,
            deterministic=True,
        )
        return Disp.HALT

    @staticmethod
    def _verifier_guidance(
        finding: VerifierFailureNoProgress,
        *,
        repeated_after_nudge: bool,
    ) -> str:
        if repeated_after_nudge:
            guidance = (
                f"{finding.tool_name} kept the same failed verification "
                "fingerprint after the bounded repair nudge, and no successful "
                "deliverable mutation or changed/pass verdict followed."
            )
        else:
            guidance = (
                f"{finding.tool_name} returned the same failed verification "
                f"fingerprint {finding.repeats} times without a successful "
                "deliverable mutation. Repeated reads, lists, status checks, and "
                "verification calls are diagnostics, not progress. Do not call `finish` "
                "while the overall verifier verdict is failing. Make one concrete "
                "semantic/file mutation that addresses the failed check, then verify once."
            )
        if finding.summary:
            guidance += f" Failure: {finding.summary}"
        if finding.next_action:
            guidance += f" Required repair: {finding.next_action}"
        return guidance

    async def _gate_repeated_verifier(
        self,
        events: list[Event],
        finding: VerifierFailureNoProgress,
    ) -> Disp:
        marker_seq = _verifier_no_progress_marker_seq(events, finding)
        if marker_seq is None:
            meta = {
                "verifier_tool": finding.tool_name,
                "failure_fingerprint_sha256": finding.failure_fingerprint_sha256,
                "streak_start_seq": finding.streak_start_seq,
                "repeat_count": finding.repeats,
            }
            guidance = self._verifier_guidance(finding, repeated_after_nudge=False)
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=f"<system-reminder>\n{guidance}\n</system-reminder>",
                    ),
                    meta={"diagnostic": _VERIFIER_NO_PROGRESS_DETAIL, **meta},
                )
            )
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=finding.marker_detail,
                    meta=meta,
                )
            )
            return Disp.CONTINUE
        acted_since = any(
            isinstance(event, ActionEvent) and event.seq is not None and event.seq > marker_seq
            for event in events
        )
        if not acted_since:
            return Disp.FALLTHROUGH
        await self.land_blocked(
            reason=_VERIFIER_NO_PROGRESS_DETAIL,
            guidance=self._verifier_guidance(finding, repeated_after_nudge=True),
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=_VERIFIER_NO_PROGRESS_DETAIL,
            deterministic=True,
        )
        return Disp.HALT

    async def _emit_no_progress_finish_hint(self) -> Disp:
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Every plan step is done and the app verify PASSES — "
                        "the outcome isn't changing because the work is already "
                        "complete. Do not edit or re-verify again: call `serve` "
                        "to hand off the deliverable, then `finish` NOW.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="no_progress_finish_hint",
            )
        )
        return Disp.CONTINUE

    async def _gate_semantic_no_progress(self, events: list[Event]) -> Disp:
        if not no_progress_detected(self._loop._recent(events)):
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
            isinstance(event, ActionEvent) and event.seq is not None and event.seq > marker_seq
            for event in events
        )
        if not acted_since:
            return Disp.FALLTHROUGH
        if _last_verify_web_app_passed(events) and not _no_progress_finish_hinted(
            events, marker_seq
        ):
            return await self._emit_no_progress_finish_hint()
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

    async def gate_no_progress(self, events: list[Event]) -> Disp:
        """Escalate invalid, repeated, or unchanged verification evidence."""
        finding = repeated_failed_verifier_no_progress(events)
        if isinstance(finding, VerifierEvidenceInvalid):
            return await self._land_invalid_verifier_evidence(finding)
        if isinstance(finding, VerifierFailureNoProgress):
            return await self._gate_repeated_verifier(events, finding)
        return await self._gate_semantic_no_progress(events)

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
