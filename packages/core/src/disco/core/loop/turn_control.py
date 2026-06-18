"""Turn-taking control: the actionless/stuck/circuit-breaker valves and the
virtual meta-tool handlers (notify_user / remember / serve / delegate_explore /
ask_user / clarify / propose_plan_update / no-op).

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

import logging
from typing import TYPE_CHECKING

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
    ToolResult,
)
from ..llm import OperatingMode
from . import signals
from .bootstrap import _detect_project_bootstrap
from .boundaries import AgentStep
from .control import Disp
from .messages import _stuck_escape_reminder
from .observe import _FANOUT_INPUT_MAX_CHARS
from .stuck import repeated_verify_no_progress

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

# WALK-19 — the corrective reminder injected when the no-progress breaker first
# trips. Failure-independent: the model's edits are "succeeding" but the app is
# unchanged, so the message reframes toward a hypothesis + a DIFFERENT outcome.
_NO_PROGRESS_REMINDER = (
    "<system-reminder>\n"
    "Your recent edits keep applying successfully but the app/verify outcome "
    "has NOT changed across several different attempts — you are likely editing "
    "code that does not affect what you're observing (wrong file, wrong layer, a "
    "cached build, or the symptom has a different root cause). STOP making more "
    "varied edits. State a concrete hypothesis for WHY the outcome is unchanged, "
    "then take a DIFFERENT diagnostic step (read the actual served output / "
    "console errors, check the build is rebuilding, or inspect a different layer) "
    "before editing again. If you cannot make the observed result change, call "
    "`finish` and state what is blocked.\n"
    "</system-reminder>"
)


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


class Valve:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

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
        if noops >= self._loop._ACTIONLESS_BREAK_CAP:
            # B5 — completion BEFORE pause. A model that signals "done" via
            # notify_user (×N) instead of finish() trips the actionless valve.
            # If the build's definition-of-done is actually met (a plan exists
            # and EVERY step is marked done), the run is finished — land a clean
            # FINISHED rather than PAUSED so a completed build doesn't read as
            # paused. Conservative: `plan_steps_complete` is False for the
            # no-plan / partially-done cases, so a genuine stall still PAUSES
            # below (the thrash guard is untouched).
            if signals.plan_steps_complete(events):
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
            if incomplete:
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "The agent produced 3 consecutive responses"
                                " without doing any real work while plan steps"
                                " remain undone — pausing instead of burning"
                                " tokens. Resume to continue."
                            ),
                        ),
                    )
                )
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
                )
                return True

        if noops >= self._loop._max_consecutive_noops:
            # The model is spinning without acting and won't stop — end
            # cleanly rather than burn. (A real run resumes on a user steer;
            # the prompt steers toward finish/act.)
            actions_since = signals.actions_since_last_resume(events)
            if incomplete and actions_since == 0:
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n⚠ finishing was"
                                " blocked: plan steps remain undone and"
                                " no work happened in this run segment."
                                "\n</system-reminder>"
                            ),
                        ),
                    )
                )
                await self._loop._emit(
                    StatusEvent(status=ConversationStatus.PAUSED, detail="noop_limit")
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
        if self._loop._stuck.is_stuck(self._loop._recent(events)):
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
                await self._loop._emit(StatusEvent(status=ConversationStatus.STUCK))
                return Disp.HALT
            # else: escape just marked, model hasn't retried yet → fall through
            # and let it act this iteration (with the bumped temperature below).
        return Disp.FALLTHROUGH

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
                    "help. Diagnose the real blocker in one sentence, then take a "
                    "DIFFERENT technical path (a different library, API, command, or "
                    "algorithm). Narrate the pivot with `notify_user`. If it is "
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
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                f"⚠ Autonomous run forfeited after {fails} consecutive "
                                "failures (recovery attempted, still failing). "
                                "Recent errors:\n"
                                + "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
                            ),
                        ),
                    )
                )
                await self._loop._emit(StatusEvent(status=ConversationStatus.STUCK))
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
        if not repeated_verify_no_progress(self._loop._recent(events)):
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
            await self._loop._emit(
                StatusEvent(status=ConversationStatus.STUCK, detail="no_progress")
            )
            return Disp.HALT
        # Marker present but the model hasn't acted on the nudge yet → let it act.
        return Disp.FALLTHROUGH

    async def gate_plan_step_lag(self, events: list[Event]) -> list[Event]:
        # (c.5) SOFT plan-step nudge — the auditor. When substantial work
        # has happened but the capstone tracker is lagging (the "did the
        # work, forgot to check it off" failure), inject ONE gentle
        # reminder so the model keeps the tracker honest. NOT a gate —
        # the model is free to ignore it; it fires at most once per lag
        # episode. This is the proactive nudge (vs. the finish-boundary
        # auto-continue which catches the same thing at the end).
        if signals.plan_step_lag_signal(events):
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "Gentle note: you've done a fair amount of work but "
                            "the plan-step tracker is behind — most steps aren't "
                            "marked done yet. If any completed steps are done, "
                            "mark them with plan_step(idx, 'done') so the user can "
                            "see real progress. No need to stop what you're doing; "
                            "just keep the tracker in sync as you go.\n"
                            "</system-reminder>"
                        ),
                    ),
                )
            )
            events = await self._loop._events()  # include the nudge in this step's View
        return events

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
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Stopped: the agent kept updating the plan checklist "
                                "without doing any real work. Re-run or steer it toward a "
                                "concrete action."
                            ),
                        ),
                    )
                )
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.STUCK, detail="bookkeeping_only"
                    )
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
            url = str(step.tool_call.arguments.get("url") or "").strip()
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

    async def gate_ask_fresh_session(self, step: AgentStep, events: list[Event]) -> Disp:
        if (
            step.tool_call is not None
            and step.tool_call.tool_name in ("ask_user", "clarify", "propose_plan_update")
            and self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
            # In autonomous mode, ask_user/clarify are owned by the headless-
            # stall guard below (a clean "no user — decide yourself" nudge);
            # don't pre-empt them here with the interactive "do work, then ask"
            # message, which tells the model it can ask when it can't. (g.5)
            # still governs propose_plan_update on a fresh session.
            and not (
                self._loop._autonomous
                and step.tool_call.tool_name in ("ask_user", "clarify")
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
            and step.tool_call.tool_name in ("ask_user", "clarify")
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
                        "can from the information you already have and continue "
                        "working toward the goal."
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
                # message + STUCK/detail pair (c.3) emits.
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Stopped: the agent kept updating the "
                                "plan checklist without doing any real "
                                "work. Re-run or steer it toward a "
                                "concrete action."
                            ),
                        ),
                    )
                )
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.STUCK,
                        detail="bookkeeping_only",
                    )
                )
                return Disp.HALT
            self._loop.mode = self._loop._execution_mode
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING, detail="plan_approved"
                )
            )
            return Disp.CONTINUE
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                detail=new_plan.id,
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
            qopts = it.get("options") or []
            if isinstance(qopts, list):
                qopts = [str(o) for o in qopts]
            else:
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
