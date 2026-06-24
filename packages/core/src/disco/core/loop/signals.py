"""Pure, stateless log-derived signal functions for the agent loop.

Extracted from engine.py (the AgentLoop god-class) as plain module functions:
every one is a pure projection of an event list (or a View) to a verdict, with
no instance state, no emission, and no I/O. They were `@staticmethod`s on
`AgentLoop`; the bodies are byte-identical. Call sites read `signals.<name>(...)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    DeliverableEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from ..llm import OperatingMode
from ..view import effective_plan_progress

if TYPE_CHECKING:
    from ..view import View

# Plan/meta tools that mutate bookkeeping state but do no real work. Excluded
# from "did the agent act?" accounting everywhere (valve taxonomy + the
# actionless streak) so a model can't look productive by shuffling plan state.
_BOOKKEEPING_TOOLS = frozenset(
    {"submit_plan", "propose_plan_update", "plan_step", "update_plan_progress", "finish"}
)

# The tool names that don't count as "productive work" for the execution gate:
# meta tools + READ-ONLY/inspection tools that don't change workspace state. The
# build-finish gate must require a STATE-CHANGING action since plan approval — a
# model that only READ the files (file_read/list/search/extract/preview/browser)
# and then declared done has delivered nothing (caught live: a macOS-clone
# iteration "finished" after 5 file_reads with zero edits). plan_step is
# informational; the planning tool would have been intercepted upstream.
_NON_PRODUCTIVE_TOOLS = frozenset(
    {
        "submit_plan",
        "plan_step",
        "update_plan_progress",  # declarative progress snapshot — pure UI signal, no work
        "ask_user",
        "propose_plan_update",
        "notify_user",
        "finish",
        "remember",  # bookkeeping — recording a fact isn't task progress on its own
        "serve",  # a handoff marker, not task work itself
        # read-only / inspection: gather context but never change the deliverable
        "file_read",
        "file_list",
        "search",
        "extract",
        "server_status",
        "shell_view",
        "shell_wait",
        "browser",
        # W-45: verify_web_app is a PROBE (self-test), not a productive edit — it
        # must not move _last_productive_seq, or the finish-gate loop breaker
        # (same verdict since the last edit ⇒ stuck) could never key on it.
        "verify_web_app",
    }
)


def productive_action_since_approval(events: list[Event]) -> bool:
    """Has the agent done any state-changing or information-gathering work since the
    most recent plan approval? Used to gate the execution-mode FINISHED transition:
    if False, the loop refuses to finish (the model would be declaring done without
    having acted). If there is no plan_approved marker (plan-mode never entered, or
    not yet approved), the gate is inert — return True so the regular finish path
    runs untouched."""
    # find the seq of the most recent plan-approval marker
    approval_seq: int | None = None
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            approval_seq = e.seq
            break
    if approval_seq is None:
        return True  # no plan-first lifecycle here; don't gate the finish
    # any ActionEvent after that point whose tool is NOT a meta tool counts
    for e in events:
        if (e.seq or 0) <= approval_seq:
            continue
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
                return True
    return False


def hard_deny_reason(action: ActionEvent) -> str | None:
    """Cluster 3: a non-negotiable command-level refusal. Returns a reason if
    the action is a hard-denied shell command (mkfs, raw-device write, fork
    bomb, rm -rf /), else None. Refused outright before the confirm gate."""
    from ..security.analyzers import hard_deny_reason

    tc = action.tool_call
    if tc is None or tc.tool_name not in ("shell", "shell_exec", "code_exec"):
        return None
    command = str(tc.arguments.get("command") or tc.arguments.get("code") or "")
    return hard_deny_reason(command)


def count_recent_failures(events: list[Event]) -> int:
    """Consecutive AgentErrorEvents walking back from the tail. Reset by a
    successful ObservationEvent or a USER message (a fresh instruction).
    Interleaved ActionEvents and agent/env messages do NOT reset. Drives the
    Cluster 2 circuit breaker."""
    streak = 0
    for e in reversed(events):
        if isinstance(e, AgentErrorEvent):
            streak += 1
        elif isinstance(e, ObservationEvent):
            break
        elif isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
    return streak


def recovery_requested_since_reset(events: list[Event]) -> bool:
    """D2 guard: True if a circuit-breaker recovery was ALREADY requested in the
    current failure streak (a `recovery_requested` StatusEvent before the streak's
    reset boundary — a USER message or successful Observation). Stops the breaker
    from re-asking every iteration; the second time through it falls to the step
    (agent proposes) or, if it failed again, the hard halt."""
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "recovery_requested":
            return True
        if isinstance(e, ObservationEvent) and e.tool_result.success:
            return False
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return False
    return False


def stuck_escape_seq(events: list[Event]) -> int | None:
    """The seq of the most recent `stuck_escape` marker since the last USER
    message, else None. Reset only on a USER message — NOT on a successful
    observation: pattern-1 stuck (the same *succeeding* no-op action repeated)
    would otherwise reset every cycle and reframe forever. One reframe escape per
    user turn; a second stall in the same turn halts."""
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "stuck_escape":
            return e.seq
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return None
    return None


def stuck_escape_attempt_count(events: list[Event]) -> int:
    """C7 — total number of `stuck_escape` markers emitted so far in this
    conversation. Used to select the rotating reminder text (the model's
    view of the previous escape's reminder, if byte-identical, would
    invite self-imitation). Counts ALL markers (not just since-last-user
    message) so the rotation is global within a run: a model that has
    seen reminder index 0 in a prior turn will see a different index on
    the next escape, even after a user message resets the loop state.
    Returns 0 when no escape has happened yet (the first escape gets
    index 0, the second gets index 1, ...)."""
    n = 0
    for e in events:
        if isinstance(e, StatusEvent) and e.detail == "stuck_escape":
            n += 1
    return n


def consecutive_noops(events: list[Event]) -> int:
    """Count trailing agent steps consumed WITHOUT a real executed action:
    prose MessageEvents, DeliverableEvents (the serve intercept), and
    duplicate-`remember` ActionEvents (the dedup pair). Resets on any real
    ActionEvent or a USER message; plan bookkeeping and fresh
    KnowledgeEvents are neutral (neither break nor count).

    The original version counted only prose messages and broke on ANY
    ActionEvent — so a model spamming `serve` emitted 50+ DeliverableEvents
    through the intercept `continue` path and never tripped a valve
    (Phase-B re-run, 2026-06-10); only the stuck detector, 95 events
    later, stopped it."""
    count = 0
    for e in reversed(events):
        if isinstance(e, ObservationEvent):
            continue  # paired with its action — judge the action instead
        if isinstance(e, ActionEvent):
            tool = e.tool_call.tool_name if e.tool_call is not None else None
            if tool == "remember":
                count += 1  # only the duplicate path emits remember actions
                continue
            if tool in _BOOKKEEPING_TOOLS:
                continue  # plan shuffling: neither real work nor spam signal
            break
        # A resume is a fresh boundary: a resumed run gets a clean actionless
        # runway. Without this the streak walks PAST the pause+resume (the resume
        # marker is ENVIRONMENT, not USER) and keeps counting the pre-pause
        # noops, so resume re-pauses after ~1 turn (live MiniMax-M3 build,
        # 2026-06). Key on the REAL resume boundary, producer-agnostically:
        #   * a PAUSED StatusEvent — everything after the most recent pause is
        #     the post-resume segment, so the PAUSED is the runway boundary. This
        #     covers BOTH resume producers: resume_service appends a RUNNING flip
        #     after the PAUSED, AND AgentLoop.resume() emits a *bare* RUNNING with
        #     no detail (the original detail=="resumed" check NEVER matched that
        #     path, so a resumed build still re-counted pre-pause noops and
        #     re-paused — the codex P1). A PAUSED only lands when the loop
        #     actually paused and returned; any later events are post-resume, so
        #     this never resets mid-run (a normal run's bare RUNNING is neutral).
        #   * RUNNING/detail="resumed" — the resume_service flip, kept so an
        #     IDLE-with-unfinished-plan resume (legal, but with NO preceding
        #     PAUSED in the log) still resets.
        if isinstance(e, StatusEvent) and (
            e.status == ConversationStatus.PAUSED
            or (e.status == ConversationStatus.RUNNING and e.detail == "resumed")
        ):
            break
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
            count += 1
            continue
        if isinstance(e, DeliverableEvent):
            count += 1
            continue
    return count


def auto_continue_attempts(events: list[Event]) -> int:
    """Count auto-continue events fired since the most recent USER message.
    The cap resets every time the user sends a fresh prompt — each new
    instruction gets its own auto-continue budget. The harness uses this
    to keep the loop moving without infinite-looping."""
    count = 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return count
        if (
            isinstance(e, StatusEvent)
            and e.detail
            and e.detail.startswith("auto_continue:")
        ):
            count += 1
    return count


def plan_is_incomplete(events: list[Event]) -> tuple[bool, list[int]]:
    """Is the most recent plan only partially done? Returns (incomplete, missing_idxs).

    A step is "complete" iff its EFFECTIVE latest state (from plan_step OR
    update_plan_progress — see `effective_plan_progress`) is "done". If there's no plan,
    returns (False, []) — nothing to gate on. This is the truth-source for the actionless
    valve's done-build guard (`plan_steps_complete`): if a step is unmarked or stuck
    "active", the build is honestly incomplete."""
    plan, states = effective_plan_progress(events)
    if plan is None:
        return (False, [])
    total = len(plan.steps)
    missing = [i + 1 for i in range(total) if states.get(i + 1) != "done"]
    return (bool(missing), missing)


def plan_steps_complete(events: list[Event]) -> bool:
    """B5 — the definition-of-done signal for the actionless valve. True iff a
    plan EXISTS with steps AND every step is marked done (the affirmative
    inverse of `plan_is_incomplete`, reusing it as the truth source).

    The valve uses this to tell a genuinely-finished build apart from a real
    stall: a model that signals completion via `notify_user` (instead of
    `finish()`) tips the actionless valve, and without this check a DONE build
    would land PAUSED. Deliberately CONSERVATIVE about the no-plan case — when
    there is no plan at all (Research runs, ad-hoc tasks) there is nothing to
    judge completeness against, so this returns False and the valve keeps its
    existing pause/continue behavior. Only an explicit, fully-checked-off plan
    reads as complete; ambiguity never auto-finishes."""
    plan: PlanEvent | None = None
    for e in events:
        if isinstance(e, PlanEvent):
            if plan is None or e.revision >= plan.revision:
                plan = e
    if plan is None or not plan.steps:
        return False  # no plan to judge → ambiguous, never auto-finish here
    incomplete, _ = plan_is_incomplete(events)
    return not incomplete


def in_planning_for_revision(events: list[Event]) -> bool:
    """BW-01 — True iff a plan REVISION is currently pending: the build re-entered
    PLANNING (a StatusEvent detail=="planning", emitted by request_plan) MORE
    recently than the latest plan approval (StatusEvent detail=="plan_approved").

    Mirrors the seq predicate at engine.py:1062-1077 (the post-restart mode
    reconstruction), inverted: that code stays in execution mode iff
    plan_approved_seq > reenter_planning_seq; a revision is pending in the
    opposite case — planning re-entered AFTER the last approval and not yet
    re-approved. Releases AUTOMATICALLY once the revised plan is approved (then
    plan_approved.seq > planning.seq again).

    Cases:
      * never planned / no "planning" marker → False (nothing pending).
      * "planning" seen but never approved → True (first plan still pending —
        a revision request that hasn't been approved is, likewise, pending).
      * latest approval is more recent than the latest "planning" → False
        (a freshly-approved build, including right after a revision lands).

    The actionless valve reads this to SUPPRESS stale-plan terminal decisions
    while the user awaits a revised plan (BW-01: the valve force-finished off the
    still-"done" prior revision's checklist during a pending re-plan).
    """
    planning_seq: int | None = None
    approved_seq: int | None = None
    for e in events:
        if isinstance(e, StatusEvent):
            if e.detail == "planning":
                planning_seq = e.seq or 0
            elif e.detail == "plan_approved":
                approved_seq = e.seq or 0
    if planning_seq is None:
        return False  # never (re-)entered planning → no pending revision
    # planning was entered; pending iff it is the most recent of the two markers
    return approved_seq is None or planning_seq > approved_seq


def planning_turns_since_replan(events: list[Event]) -> int:
    """BW-01 follow-up 2 — count tool-less PLANNING turns spent since the build
    most recently (re-)entered planning (StatusEvent detail=="planning"), as long
    as NO plan has since been submitted (PlanEvent) or (re-)approved
    (plan_approved). This is the stateless, noop-INDEPENDENT bound the actionless
    valve halts a pending re-plan on.

    Why a separate count (codex P1 — the residual hang): each tool-less planning
    turn appends exactly one ENVIRONMENT plan-nudge message (engine `_PLAN_NUDGE`,
    emitted ONLY by the planning-mode gate). A model spinning in planning racks
    these up even when the noop counters DON'T move — a BLANK tool-less planning
    turn emits neither an AGENT message (so `consecutive_noops` ignores it) nor an
    invisible-step bump (the planning gate, unlike `handle_noop_step`, never
    touches `_invisible_steps`), so the noop ladder reads 0 forever and the
    `in_planning_for_revision` ceiling never trips → the run hangs. Counting the
    nudges instead is independent of that classification: prose, blank, or nothing,
    every tool-less planning turn is one nudge.

    Returns 0 when NOT spinning in planning: no `planning` marker at all, or a
    later PlanEvent / plan_approved (the model DID submit/land a plan since
    re-entry — the auto-release case).
    """
    from .engine import _PLAN_NUDGE

    planning_seq: int | None = None
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "planning":
            planning_seq = e.seq or 0
            break
    if planning_seq is None:
        return 0
    count = 0
    for e in events:
        if (e.seq or 0) <= planning_seq:
            continue
        # A plan was submitted or (re-)approved after re-entry → no longer a
        # planning spin; release the bound (the model moved forward).
        if isinstance(e, PlanEvent):
            return 0
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            return 0
        if (
            isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and e.message.content == _PLAN_NUDGE
        ):
            count += 1
    return count


def actions_since_last_resume(events: list[Event]) -> int:
    """Count ActionEvents (excluding meta/bookkeeping tools and the
    verify-on-finish probe) since the last resume boundary, or since start.

    The resume boundary is the REAL one, keyed producer-agnostically exactly
    like `consecutive_noops` (fb60fc8):
      * a `StatusEvent(PAUSED)` — everything after the most recent pause is the
        post-resume segment, so the PAUSED is the boundary. This covers BOTH
        resume producers: resume_service appends a RUNNING flip after the
        PAUSED, AND `AgentLoop.resume()` emits a *bare* `StatusEvent(RUNNING)`
        with no detail. A PAUSED only lands when the loop actually paused and
        returned, so any later events are post-resume; a normal run's bare
        RUNNING (no preceding PAUSED) is neutral and never resets mid-run.
      * `StatusEvent(RUNNING, detail="resumed")` — the resume_service flip,
        kept so an IDLE-with-unfinished-plan resume (legal, NO preceding PAUSED
        in the log) still resets.

    Keying ONLY on detail=="resumed" (the original) NEVER matched the
    bare-RUNNING resume that `AgentLoop.resume()` emits, so after a resume that
    FOLLOWED prior work this returned the STALE pre-pause count — and the BW-02
    STUCK escalation's `== 0` gate never fired, re-pausing forever (codex P1,
    the twin of the fb60fc8 consecutive_noops bare-RUNNING blind spot)."""
    count = 0
    for e in reversed(events):
        if isinstance(e, StatusEvent) and (
            e.status == ConversationStatus.PAUSED
            or (e.status == ConversationStatus.RUNNING and e.detail == "resumed")
        ):
            break
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.meta.get("verify_probe"):
                # The finish gate's own probe — running it is not evidence
                # the AGENT did real work (re-run #6 leak).
                continue
            if e.tool_call.tool_name not in _BOOKKEEPING_TOOLS:
                count += 1
    return count


def consecutive_actionless_pauses(events: list[Event]) -> int:
    """BW-02 escalation signal — the trailing run of
    `StatusEvent(PAUSED, detail="actionless")` landings, counting back from the
    end of `events`, separated only by resume markers and noop steps.

    A real (non-bookkeeping, non-verify-probe) ActionEvent — a file_read, search,
    browser, edit, anything the model actually DID — breaks the run, as does any
    OTHER landing (FINISHED/STUCK/a different PAUSE reason). RUNNING resume markers
    are transparent. So the count is the number of consecutive zero-ACTION
    actionless pauses already in the log: an actionless PAUSE only lands when the
    segment leading to it produced no real action (a real action resets the noop
    streak before the cap), so a trailing run of them with no action in between is
    a degenerate "keeps pausing" loop the plain pause never breaks. The actionless
    valve consults this so the SECOND such pause escalates to STUCK instead of
    re-pausing forever; the FIRST (count 0 here) stays the useful stop."""
    count = 0
    for e in reversed(events):
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name in _BOOKKEEPING_TOOLS or e.meta.get("verify_probe"):
                continue  # bookkeeping / the finish probe is not the model acting
            break  # a real action breaks the degenerate streak
        if isinstance(e, StatusEvent):
            if e.status == ConversationStatus.PAUSED and e.detail == "actionless":
                count += 1
                continue
            if e.status == ConversationStatus.RUNNING:
                continue  # a resume marker between pauses — transparent
            break  # a different PAUSE reason, or FINISHED/STUCK — streak ends
    return count


def productive_actions_since_approval(events: list[Event]) -> int:
    """Count PRODUCTIVE (state-changing) actions since the latest plan approval — i.e.
    actions NOT in `_NON_PRODUCTIVE_TOOLS` (excludes reads, browser, and the
    plan_step/update_plan_progress bookkeeping). Used to keep the actionless valve's
    `completed_via_notify` net HONEST: a build is "complete" only if the agent actually
    DID work, not merely marked every step done + spammed notify_user — which would
    otherwise FINISH an empty workspace (the `update_plan_progress` path made this
    reachable for capable models). No approval yet → counts from the start."""
    approval_seq: int | None = None
    for e in events:
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            approval_seq = e.seq
    count = 0
    for e in events:
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if approval_seq is not None and (e.seq or 0) <= approval_seq:
            continue
        if e.meta.get("verify_probe"):
            continue  # the finish gate's own probe is not agent work (#6 leak)
        if e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
            count += 1
    return count


def plan_step_lag_signal(events: list[Event]) -> bool:
    """The 'auditor' for the soft plan-step nudge. Returns True when the
    agent has done substantial productive work but the capstone tracker is
    clearly lagging — the classic 'did the work, forgot to check it off'
    failure. The nudge that follows is SOFT (a gentle suggestion, not a
    gate), and fires at most once per lag episode (no re-fire until the
    agent marks another step or the lag clears).

    Heuristic (all derivable from the log, stateless):
      - a plan exists with steps, and we're past plan approval
      - productive (state-changing) actions since approval >= total steps
        (i.e. enough work has happened that *something* should be marked)
      - fewer than half the steps are marked done (tracker is behind)
      - no soft-lag nudge has fired since the last tracker action (plan_step
        OR update_plan_progress)
        (so we nudge once per episode, not every iteration)
    """
    # Latest plan.
    plan: PlanEvent | None = None
    approval_seq: int | None = None
    for e in events:
        if isinstance(e, PlanEvent):
            if plan is None or e.revision >= plan.revision:
                plan = e
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            approval_seq = e.seq
    if plan is None or not plan.steps or approval_seq is None:
        return False
    total = len(plan.steps)

    productive = 0
    last_tracker_seq = -1  # last plan_step OR update_plan_progress (either tracker)
    last_lag_nudge_seq = -1
    for e in events:
        seq = e.seq or 0
        if seq <= approval_seq:
            continue
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            name = e.tool_call.tool_name
            if name in ("plan_step", "update_plan_progress"):
                last_tracker_seq = seq  # either channel counts as "tracking"
            elif name not in _NON_PRODUCTIVE_TOOLS:
                productive += 1
        elif (
            isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and "plan-step tracker" in e.message.content
        ):
            last_lag_nudge_seq = seq

    if productive < total:
        return False  # not enough work yet to expect check-offs
    # Done-count from BOTH channels (a capable model tracks via update_plan_progress —
    # nagging it about plan_step would be wrong, so the shared reader is the truth).
    _, states = effective_plan_progress(events)
    done_count = sum(1 for i in range(1, total + 1) if states.get(i) == "done")
    if done_count * 2 >= total:
        return False  # tracker is keeping up (>= half done)
    # Only nudge once per episode: skip if a lag nudge already fired more recently
    # than the last tracker action (nothing checked off since — no point repeating).
    if last_lag_nudge_seq > last_tracker_seq:
        return False
    return True


def bookkeeping_streak_len(events: list[Event]) -> int:
    """Trailing count of consecutive BOOKKEEPING-only actions (plan_step /
    submit_plan / propose_plan_update — NOT `finish`, which is intercepted before
    it lands as an ActionEvent) since the last real action, user message, or
    resume/approval marker. This is the signal the StuckDetector and noop valve
    both miss for plan_step spam (issue C)."""
    plan_bk = _BOOKKEEPING_TOOLS - {"finish"}
    streak = 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, StatusEvent) and e.detail in ("resumed", "plan_approved"):
            break
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name in plan_bk:
                streak += 1
            else:
                break  # a real (state-changing) action ends the streak
        # observations / other status events between actions are skipped
    return streak


def active_plan_step_count(events: list[Event]) -> int:
    # Step count of the latest PlanEvent, or 0 if no plan has been
    # approved yet. Used by the (c.3) bookkeeping halt (T7 / E3) to scale
    # the spam cap with the plan's actual size -- a model finishing an
    # N-step plan may legitimately emit up to ~N `plan_step` calls; the
    # halt threshold becomes max(HALT_AT, N + slack) so a legit burst
    # does not trip it.
    plan = None
    for e in events:
        if isinstance(e, PlanEvent):
            if plan is None or e.revision >= plan.revision:
                plan = e
    if plan is None or not plan.steps:
        return 0
    return len(plan.steps)


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


def estimate_tokens(view: View) -> int:
    # ~4 chars/token heuristic. A-S1 fix: the old version summed only message
    # `.content`, ignoring tool-call argument JSON, tool schemas, and the
    # system prompt — so the real prompt was LARGER than estimated yet the
    # trigger still fired. Now we also count serialized tool-call args and add
    # a fixed allowance for the system prompt + tool-schema prefix.
    chars = 0
    for m in view.messages:
        chars += len(m.content or "")
        for tc in getattr(m, "tool_calls", None) or []:
            # tool_calls may be dicts or pydantic models; stringify defensively.
            chars += len(str(getattr(tc, "arguments", None) or tc))
    # System prompt + tool-schema prefix allowance (~2k tokens), counted so the
    # trigger reflects the true prompt size, not just the transcript tail.
    return chars // 4 + 2_000


def has_unprocessed_user_message(events: list[Event]) -> bool:
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
