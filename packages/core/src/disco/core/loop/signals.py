"""Pure, stateless log-derived signal functions for the agent loop.

Extracted from engine.py (the AgentLoop god-class) as plain module functions:
every one is a pure projection of an event list (or a View) to a verdict, with
no instance state, no emission, and no I/O. They were `@staticmethod`s on
`AgentLoop`; the bodies are byte-identical. Call sites read `signals.<name>(...)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    PlanStep,
    StatusEvent,
)
from ..llm import OperatingMode
from ..think import strip_think_spans
from ..view import effective_plan_progress

if TYPE_CHECKING:
    from ..view import View

# Plan/meta tools that mutate bookkeeping state but do no real work. Excluded
# from "did the agent act?" accounting everywhere (valve taxonomy + the
# actionless streak) so a model can't look productive by shuffling plan state.
_BOOKKEEPING_TOOLS = frozenset(
    {
        "submit_plan",
        "propose_plan_update",
        "plan_step",
        "update_plan_progress",
        "finish",
        "think",
    }
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
        "think",
        "plan_step",
        "update_plan_progress",  # declarative progress snapshot — pure UI signal, no work
        "ask_user",
        "questions_v2",
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
        # Preview lifecycle controls only change host observation state. They do
        # not mutate the deliverable and must never invalidate verifier evidence.
        "preview_start",
        "preview_status",
        "preview_logs",
        "preview_stop",
        "shell_view",
        "shell_wait",
        "browser",
        # W-45: verify_web_app is a PROBE (self-test), not a productive edit — it
        # must not move _last_productive_seq, or the finish-gate loop breaker
        # (same verdict since the last edit ⇒ stuck) could never key on it.
        "verify_web_app",
        # EPIC G: verify_appkit_app is the STRICT app verifier — a probe beside
        # verify_web_app (the finish gate drives it in AppKit mode). Re-verifying
        # without a productive edit must read as no-progress, never as work.
        "verify_appkit_app",
        # EPIC D3: design_lint is a READ-ONLY design-slop scanner — a verification
        # probe beside verify_web_app, never a productive edit. Re-linting without
        # an edit must read as no-progress (same verdict ⇒ stuck), not as work.
        "design_lint",
        # EPIC H3: app_snapshot_version checkpoints the specs+tree (writes only a
        # version record, never the deliverable). Re-snapshotting without a
        # productive edit must read as no-progress, not as task work.
        "app_snapshot_version",
    }
)

_FINISH_INTENT_RE = re.compile(
    r"\b(?:finish(?:ing)?|finali[sz]e|complete|completion|done|ship|handoff|"
    r"hand[\s-]?off|deliver|submit(?: the)? final|final answer|ready for "
    r"verification|call finish)\b",
    re.IGNORECASE,
)
_ACTIONABLE_BUILD_VERB_RE = re.compile(
    r"\b(?:add(?:s|ed|ing)?|create(?:s|d)?|creating|build(?:s|ing)?|built|"
    r"implement(?:s|ed|ing)?|writ(?:e|es|ing)|wrote|design(?:s|ed|ing)?|"
    r"styl(?:e|es|ed|ing)|mak(?:e|es|ing)|made|configure(?:s|d)?|configuring|"
    r"config|install(?:s|ed|ing)?|includ(?:e|es|ed|ing)|insert(?:s|ed|ing)?|"
    r"append(?:s|ed|ing)?|generat(?:e|es|ed|ing)|develop(?:s|ed|ing)?|"
    r"scaffold(?:s|ed|ing)?|integrat(?:e|es|ed|ing)|updat(?:e|es|ed|ing)|"
    r"fix(?:es|ed|ing)?|refactor(?:s|ed|ing)?|polish(?:es|ed|ing)?|setup|"
    r"set[\s-]?up|wire[\s-]?up)\b",
    re.IGNORECASE,
)

SYNTHETIC_FINISH_ATTEMPT_DETAIL = "synthetic_finish_attempted"
PROSE_NOOP_REPAIR_DIAGNOSTIC = "prose_noop_repair"
READ_CHURN_NUDGE_DIAGNOSTIC = "read_churn_nudge"
STUCK_ESCAPE_BLOCK_DETAIL_PREFIX = "stuck_escape_block:"
_STUCK_ESCAPE_BLOCKABLE_TOOLS = frozenset({"file_read"})

_READ_CHURN_SMALL_LIMIT = 25
_READ_CHURN_WARNING_COUNTS = frozenset({5, 10, 15})
_READ_CHURN_LADDER_AT = 20
_READ_LINES_HEADER_RE = re.compile(r"\[lines\s+(\d+)-(\d+)\s+of\s+(\d+)")
_READ_CHURN_RESET_TOOLS = frozenset(
    {
        "file_write",
        "file_edit",
        "file_append",
        "shell",
        "shell_exec",
        "run_project_script",
        "browser",
        "design_lint",
        "update_plan_progress",
        "submit_plan",
        "finish",
    }
)


@dataclass(frozen=True)
class ReadChurnState:
    path: str
    count: int
    warning_count: int | None
    invisible_noops: int


def _event_seq(event: Event, fallback: int) -> int:
    return event.seq if event.seq is not None else fallback


def _successful_action_ids(events: list[Event]) -> set[str]:
    """Action ids whose paired result is a successful ObservationEvent."""
    succeeded: dict[str, bool] = {}
    for e in events:
        if isinstance(e, ObservationEvent):
            action_id = e.action_id
            if isinstance(action_id, str):
                succeeded.setdefault(action_id, bool(e.tool_result.success))
        elif isinstance(e, AgentErrorEvent) and e.action_id is not None:
            succeeded.setdefault(e.action_id, False)
    return {action_id for action_id, ok in succeeded.items() if ok}


def _paired_action_ids(events: list[Event]) -> set[str]:
    """Action ids with any paired ObservationEvent or AgentErrorEvent."""
    paired: set[str] = set()
    for e in events:
        if isinstance(e, ObservationEvent):
            action_id = e.action_id
            if isinstance(action_id, str):
                paired.add(action_id)
        elif isinstance(e, AgentErrorEvent) and e.action_id is not None:
            paired.add(e.action_id)
    return paired


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
    successful_actions = _successful_action_ids(events)
    # any successful ActionEvent after that point whose tool is NOT a meta tool counts
    for e in events:
        if (e.seq or 0) <= approval_seq:
            continue
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.id in successful_actions and e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
                return True
    return False


def _plan_approval_seqs(events: list[Event]) -> list[int]:
    return [
        e.seq or 0 for e in events if isinstance(e, StatusEvent) and e.detail == "plan_approved"
    ]


def _step_is_finish_intent(step: PlanStep) -> bool:
    text = f"{step.title} {step.detail or ''}".strip()
    if not text:
        return False
    if _ACTIONABLE_BUILD_VERB_RE.search(text):
        return False
    return _FINISH_INTENT_RE.search(text) is not None


def finish_intent_replan_after_prior_productive_work(events: list[Event]) -> bool:
    """True for a re-plan that only tells the model to finish after earlier work.

    The execution nudge is anchored to the latest ``plan_approved`` marker. That
    is correct for a fresh plan, but a re-plan whose only remaining step is
    completion/handoff can otherwise erase the productive work that happened in
    the prior approved segment and deadlock on "plan not executed yet". This
    predicate is intentionally narrow: it requires a prior approval segment with
    successful productive work, and the latest plan's unfinished steps must be
    finish-intent only. A genuinely never-executed plan therefore still returns
    False and lands in the existing ``approve_plan_no_execution`` cap path.
    """

    approvals = _plan_approval_seqs(events)
    if len(approvals) < 2:
        return False
    previous_approval_seq = approvals[-2]
    latest_approval_seq = approvals[-1]

    successful_actions = _successful_action_ids(events)
    prior_productive = False
    for event in events:
        seq = event.seq or 0
        if seq <= previous_approval_seq or seq >= latest_approval_seq:
            continue
        if _is_successful_productive_action(event, successful_actions):
            prior_productive = True
            break
    if not prior_productive:
        return False

    plan, states = effective_plan_progress(events)
    if plan is None or not plan.steps:
        return False
    plan_seq = plan.seq or 0
    if plan_seq > latest_approval_seq or plan_seq <= previous_approval_seq:
        return False

    remaining = [
        step for index, step in enumerate(plan.steps, start=1) if states.get(index) != "done"
    ]
    return bool(remaining) and all(_step_is_finish_intent(step) for step in remaining)


def _is_successful_productive_action(event: Event, successful_actions: set[str]) -> bool:
    if not isinstance(event, ActionEvent) or event.tool_call is None:
        return False
    if event.meta.get("verify_probe"):
        return False
    return event.id in successful_actions and event.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS


_SYNTHETIC_FINISH_RESET_STATUSES = frozenset(
    {
        ConversationStatus.IDLE,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    }
)


def successful_action_ids(events: list[Event]) -> set[str]:
    """Public alias for the paired-success scan (runtime ladder guards use it)."""
    return _successful_action_ids(events)


def is_successful_productive_action(event: Event, successful: set[str]) -> bool:
    """Public alias — True iff `event` is a successful PRODUCTIVE action."""
    return _is_successful_productive_action(event, successful)


def actionless_pause_count_current_execution_segment(events: list[Event]) -> int:
    """REL-RC-P: consecutive actionless PAUSE landings in the current execution
    segment.

    The segment starts at the latest ``plan_approved`` marker. Walking backward
    from the tail, actionless pauses count; resume/RUNNING markers, assistant
    prose, observations, and non-productive tools are transparent. A successful
    productive action or any terminal/parked status resets the count. This is the
    finish-boundary sibling of the older BW-02 pause-loop signal, but deliberately
    does not reset on reads/browser probes: those are activity, not productive
    completion work.
    """
    indexed = [(idx, event) for idx, event in enumerate(events, start=1)]
    approval_seq: int | None = None
    for idx, event in indexed:
        if isinstance(event, StatusEvent) and event.detail == "plan_approved":
            approval_seq = _event_seq(event, idx)
    if approval_seq is None:
        return 0

    successful_actions = _successful_action_ids(events)
    count = 0
    for idx, event in reversed(indexed):
        seq = _event_seq(event, idx)
        if seq <= approval_seq:
            break
        if _is_successful_productive_action(event, successful_actions):
            break
        if isinstance(event, StatusEvent):
            if event.status == ConversationStatus.PAUSED and event.detail == "actionless":
                count += 1
                continue
            if event.status == ConversationStatus.RUNNING:
                continue
            if event.status in _SYNTHETIC_FINISH_RESET_STATUSES:
                # Terminal-collapse: a blocked-breaker LANDING (explain+ask) is
                # bookkeeping over the pause it supersedes, not a fresh user
                # interaction — it must not reset the actionless count or the
                # REL-RC-P valve goes blind (the marker precedes it).
                if (getattr(event, "meta", None) or {}).get("blocked_landing"):
                    continue
                break
            if event.status == ConversationStatus.PAUSED:
                break
    return count


def _latest_actionless_pause_seq(events: list[Event]) -> int | None:
    latest: int | None = None
    for idx, event in enumerate(events, start=1):
        if (
            isinstance(event, StatusEvent)
            and event.status == ConversationStatus.PAUSED
            and event.detail == "actionless"
        ):
            latest = _event_seq(event, idx)
    return latest


def synthetic_finish_attempted_for_current_pause(events: list[Event]) -> bool:
    """True iff REL-RC-P already attempted finish after the latest actionless
    pause. A later actionless pause re-arms the behavior; in-memory counters are
    not involved."""
    latest_pause = _latest_actionless_pause_seq(events)
    if latest_pause is None:
        return False
    for idx, event in enumerate(events, start=1):
        if _event_seq(event, idx) <= latest_pause:
            continue
        if isinstance(event, StatusEvent) and event.detail == SYNTHETIC_FINISH_ATTEMPT_DETAIL:
            return True
    return False


def should_synthesize_finish_after_actionless_pauses(events: list[Event]) -> bool:
    """REL-RC-P decision predicate. The engine separately checks it is not in
    planning mode; this pure helper owns the replayable event-derived guards."""
    return (
        actionless_pause_count_current_execution_segment(events) >= 2
        and productive_action_since_approval(events)
        and not synthetic_finish_attempted_for_current_pause(events)
    )


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


# Non-critical bookkeeping tools whose malformed/failed calls must NOT burn the
# circuit-breaker streak. `update_plan_progress` UPDATES the plan tracker but performs
# NO workspace action; some models (e.g. MiniMax-M3)
# intermittently emit a malformed `steps` payload (empty strings instead of step
# dicts). The schema correctly rejects it with an actionable message, but a cosmetic
# bookkeeping hiccup is NOT "the model is stuck on the task" — counting it toward the
# Cluster-2 breaker would escalate a trivial build to AWAITING_USER for nothing.
_NONCRITICAL_FAILURE_TOOLS = frozenset({"update_plan_progress"})


def count_recent_failures(events: list[Event]) -> int:
    """Consecutive AgentErrorEvents walking back from the tail. Reset by a
    successful ObservationEvent or a USER message (a fresh instruction).
    Interleaved ActionEvents and agent/env messages do NOT reset. Drives the
    Cluster 2 circuit breaker. Failures from non-critical informational tools
    (`_NONCRITICAL_FAILURE_TOOLS`) are TRANSPARENT — they neither increment nor
    reset the streak (a real failure before them is still counted)."""
    tool_by_action = {
        e.id: e.tool_call.tool_name
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call is not None
    }
    streak = 0
    for e in reversed(events):
        if isinstance(e, AgentErrorEvent):
            action_id = e.action_id
            if (
                action_id is not None
                and tool_by_action.get(action_id) in _NONCRITICAL_FAILURE_TOOLS
            ):
                continue  # cosmetic bookkeeping error — transparent to the breaker
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


# [REL-RC-E] Re-groundable edit error codes: repeating any of these on the same path means the model
# is editing against a stale view of the file, which one real file_read fixes.
# FRESH_READ_REQUIRED is
# the read-before-write gate; bad_range / bad_line are line-target edits (file_replace_lines /
# file_insert_lines) whose numbers fell outside the current file — a fresh read
# shows the true lines.
_AUTOGROUND_ERROR_CODES = frozenset({"FRESH_READ_REQUIRED", "bad_range", "bad_line"})


def fresh_read_autoground_target(events: list[Event]) -> str | None:
    """[REL-RC-B/REL-RC-E] The workspace path the build is LOOPING on at an edit gate — the path to
    auto-read once to break a repeating FRESH_READ_REQUIRED / bad_range / bad_line
    edit loop, or None.

    Returns the path of the most-recent FRESH_READ_REQUIRED edit error IFF that path has produced
    >= 2 such errors since the last successful Observation or USER message (the streak), AND no
    ``auto_ground_read:{path}`` marker exists since the last USER message (the
    durable one-auto-read-
    per-path-per-revision sentinel). The marker scan stops only at the USER message, so it SURVIVES
    the injected read's own success Observation — a file that STILL fails after one genuine read
    falls through to the circuit breaker and STUCKs cleanly, never re-arming the auto-read.

    AgentErrorEvent carries no path (only ``error`` + ``action_id``), so the edited path is resolved
    via the failed ActionEvent's tool_call ``path`` argument. Matched on the RAW path string
    (consistent within a loop; the file_read tool canonicalizes internally) because core cannot
    import the tool-layer canonicalizer."""
    path_by_action: dict[str, str] = {}
    for e in events:
        if isinstance(e, ActionEvent) and e.tool_call:
            p = e.tool_call.arguments.get("path")
            if isinstance(p, str) and p:
                path_by_action[e.id] = p
    # streak: same-path FRESH_READ_REQUIRED count, reset by a successful Observation OR a USER msg.
    target: str | None = None
    count = 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, ObservationEvent) and e.tool_result.success:
            break
        if isinstance(e, AgentErrorEvent) and e.error in _AUTOGROUND_ERROR_CODES:
            p = path_by_action.get(e.action_id or "")
            if p:
                if target is None:
                    target = p
                if p == target:
                    count += 1
    if target is None or count < 2:
        return None
    # durable sentinel: have we ALREADY auto-read this path since the last USER message?
    marker = f"auto_ground_read:{target}"
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, StatusEvent) and e.detail == marker:
            return None
    return target


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


def stuck_escape_blocked_tools(events: list[Event]) -> frozenset[str]:
    """Return host-typed tools quarantined for the current escape turn.

    The block is load-bearing state, so it is encoded in SYSTEM StatusEvent
    details immediately before the existing ``stuck_escape`` marker rather
    than volatile metadata. Exact adjacency binds the block to that escape;
    a stale marker, intervening event, or non-system spoof is inert.
    """
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            return frozenset()
        if not (
            isinstance(event, StatusEvent)
            and event.source == EventSource.SYSTEM
            and event.status == ConversationStatus.RUNNING
            and event.detail == "stuck_escape"
        ):
            continue
        blocked: set[str] = set()
        cursor = index - 1
        while cursor >= 0:
            marker = events[cursor]
            if not (
                isinstance(marker, StatusEvent)
                and marker.source == EventSource.SYSTEM
                and marker.status == ConversationStatus.RUNNING
                and isinstance(marker.detail, str)
                and marker.detail.startswith(STUCK_ESCAPE_BLOCK_DETAIL_PREFIX)
            ):
                break
            tool_name = marker.detail.removeprefix(STUCK_ESCAPE_BLOCK_DETAIL_PREFIX)
            if (
                re.fullmatch(r"[a-z][a-z0-9_]*", tool_name) is None
                or tool_name not in _STUCK_ESCAPE_BLOCKABLE_TOOLS
            ):
                return frozenset()
            blocked.add(tool_name)
            cursor -= 1
        return frozenset(blocked)
    return frozenset()


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
            if tool == "submit_plan":
                # A plan submission is the PLANNING->EXECUTION gate transition — real
                # progress that BOUNDS the actionless streak, not neutral plan-shuffling.
                # Without this `break` the backward walk steps past submit_plan (it was in
                # _BOOKKEEPING_TOOLS -> `continue`) and keeps counting pre-submit planning
                # prose, so a legit REVISION re-plan (narrate the revised plan + submit) was
                # miscounted as one long actionless stall -> false PAUSE -> STUCK. Reset HERE
                # only; _BOOKKEEPING_TOOLS is left intact for its other readers (codex review).
                # Safe universally: it can only SHORTEN the streak, never manufacture a stall.
                break
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


def prose_noop_repair_seen_current_execution_segment(events: list[Event]) -> bool:
    """Has the one-shot execution prose repair already fired in this segment?

    The repair marker is durable so retry bounds survive loop recreation. The
    segment boundary mirrors the existing actionless runway boundaries that can
    make a new execution attempt legitimate: a fresh user message, a resume
    boundary, or a newly approved plan. Real actions do not clear this marker;
    the nudge is a per-segment correction, not a per-streak correction.
    """
    for e in reversed(events):
        if isinstance(e, MessageEvent):
            if e.source == EventSource.USER:
                break
            if (
                e.source == EventSource.ENVIRONMENT
                and e.meta.get("diagnostic") == PROSE_NOOP_REPAIR_DIAGNOSTIC
            ):
                return True
        if isinstance(e, StatusEvent):
            if e.status == ConversationStatus.PAUSED:
                break
            if e.status == ConversationStatus.RUNNING and e.detail in (
                "plan_approved",
                "resumed",
                "planning",
            ):
                break
    return False


def _read_churn_path(action: ActionEvent) -> str | None:
    if action.tool_call is None or action.tool_call.tool_name != "file_read":
        return None
    path = action.tool_call.arguments.get("path")
    return path if isinstance(path, str) and path else None


def _read_churn_limit(action: ActionEvent) -> int | None:
    if action.tool_call is None:
        return None
    value = action.tool_call.arguments.get("limit")
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _read_churn_successful_observations(events: list[Event]) -> dict[str, ObservationEvent]:
    return {
        event.action_id: event
        for event in events
        if isinstance(event, ObservationEvent) and event.tool_result.success
    }


def _file_read_covers_remainder(observation: ObservationEvent | None) -> bool:
    if observation is None:
        return False
    match = _READ_LINES_HEADER_RE.search(observation.tool_result.content or "")
    if match is None:
        return False
    _start, end, total = (int(group) for group in match.groups())
    return end >= total


def _is_whole_file_read(action: ActionEvent, observation: ObservationEvent | None) -> bool:
    if action.tool_call is None or action.tool_call.tool_name != "file_read":
        return False
    # A no-limit read (including offset-to-EOF) is the model moving out of tiny
    # paging. The tool result header lets range reads that reach EOF reset too.
    return _read_churn_limit(action) is None or _file_read_covers_remainder(observation)


def _is_small_file_read(action: ActionEvent, observation: ObservationEvent | None) -> bool:
    if observation is None or action.tool_call is None:
        return False
    if action.tool_call.tool_name != "file_read":
        return False
    limit = _read_churn_limit(action)
    return limit is not None and limit <= _READ_CHURN_SMALL_LIMIT


def _is_read_churn_reset_action(action: ActionEvent) -> bool:
    if action.tool_call is None:
        return False
    tool = action.tool_call.tool_name
    return (
        tool in _READ_CHURN_RESET_TOOLS
        or tool.startswith("preview_")
        or (tool.startswith("file_") and tool.endswith("_lines"))
    )


def read_churn_state(events: list[Event]) -> ReadChurnState | None:
    """Current same-target tiny ``file_read`` streak in execution.

    The scan is intentionally event-log-derived: diagnostics already emitted in
    the current streak suppress duplicate warnings, while any reset action before
    the streak makes old diagnostics irrelevant and re-arms the ladder.
    """
    successful = _read_churn_successful_observations(events)
    diagnostics_seen: set[tuple[str, int]] = set()
    target: str | None = None
    count = 0

    for event in reversed(events):
        if isinstance(event, MessageEvent):
            if event.source == EventSource.USER:
                break
            if event.source == EventSource.AGENT:
                break
            if (
                event.source == EventSource.ENVIRONMENT
                and event.meta.get("diagnostic") == READ_CHURN_NUDGE_DIAGNOSTIC
            ):
                path = event.meta.get("path")
                n = event.meta.get("count", event.meta.get("streak"))
                if isinstance(path, str) and isinstance(n, int):
                    diagnostics_seen.add((path, n))
            continue
        if isinstance(event, StatusEvent):
            if event.detail in ("plan_approved", "planning"):
                break
            continue
        if isinstance(event, PlanEvent):
            break
        if isinstance(event, ObservationEvent | AgentErrorEvent):
            continue
        if not isinstance(event, ActionEvent):
            continue

        observation = successful.get(event.id)
        path = _read_churn_path(event)
        if target is None:
            if path is None or not _is_small_file_read(event, observation):
                if _is_read_churn_reset_action(event):
                    break
                continue
            target = path
            count = 1
            continue

        if path is not None:
            if path != target:
                break
            if _is_whole_file_read(event, observation):
                break
            if _is_small_file_read(event, observation):
                count += 1
                continue
            break
        if _is_read_churn_reset_action(event):
            break

    if target is None or count == 0:
        return None
    warning_count = (
        count
        if count in _READ_CHURN_WARNING_COUNTS and (target, count) not in diagnostics_seen
        else None
    )
    invisible_noops = max(0, count - (_READ_CHURN_LADDER_AT - 1))
    return ReadChurnState(
        path=target,
        count=count,
        warning_count=warning_count,
        invisible_noops=invisible_noops,
    )


def auto_continue_attempts(events: list[Event]) -> int:
    """Count auto-continue events fired since the most recent USER message.
    The cap resets every time the user sends a fresh prompt — each new
    instruction gets its own auto-continue budget. The harness uses this
    to keep the loop moving without infinite-looping."""
    count = 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return count
        if isinstance(e, StatusEvent) and e.detail and e.detail.startswith("auto_continue:"):
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


def effective_mode(
    events: list[Event],
    *,
    planning: OperatingMode = OperatingMode.PLANNING,
    execution: OperatingMode = OperatingMode.LONG_HORIZON,
    default: OperatingMode = OperatingMode.PLANNING,
) -> OperatingMode:
    """Return the event-log-derived operating mode for a plan-gated loop.

    A ``plan_approved`` marker moves the loop into execution until a later
    ``planning`` marker re-enters the plan gate. With no lifecycle marker, the
    caller's default is used so non-plan surfaces can keep their configured mode
    while build loops still default to PLANNING.
    """
    planning_seq: int | None = None
    approved_seq: int | None = None
    for event in events:
        if isinstance(event, StatusEvent):
            if event.detail == "planning":
                planning_seq = event.seq or 0
            elif event.detail == "plan_approved":
                approved_seq = event.seq or 0
    if approved_seq is not None and (planning_seq is None or approved_seq > planning_seq):
        return execution
    if planning_seq is not None:
        return planning
    return default


def revision_force_submit(events: list[Event]) -> bool:
    """True iff a REVISION re-plan has been ESCALATED to forced-submit and not yet
    satisfied. The engine emits a StatusEvent(detail="force_submit_plan") marker after
    `_REVISION_FORCE_SUBMIT_K` prose-only revision-planning nudges (the soft _PLAN_NUDGE
    was ignored and the build would otherwise pause "actionless" → STUCK, with resume
    re-entering the same prose loop). While this is True, `driver.tools_for_step` narrows
    the offered planning tools to `submit_plan` + READ tools (within a read grace), so the
    model can file_read the current files to ground its revised plan and then submit — the
    prose-narration case is still caught by the actionless valve (a tool-less turn).

    Stateless (read from the event log) so it SURVIVES resume: a resumed build replays the
    marker and stays forced. Gated on `in_planning_for_revision` (revision-only — the
    initial-plan / non-revision stall path never sees this). RELEASES as soon as the model
    submits (a `submit_plan` ActionEvent more recent than the marker) or the revised plan
    is approved (`in_planning_for_revision` flips False)."""
    if not in_planning_for_revision(events):
        return False
    for e in reversed(events):
        if isinstance(e, ActionEvent):
            tool = e.tool_call.tool_name if e.tool_call is not None else None
            if tool == "submit_plan":
                return False  # a submission since the marker → escalation satisfied
        if isinstance(e, StatusEvent) and e.detail == "force_submit_plan":
            return True
    return False


def pending_revision_steer(events: list[Event]) -> bool:
    """True iff a LIVE revision STEER marker is UNCONSUMED. The disco kernel ingress
    (`DiscoKernel.send_user_turn`) appends a `StatusEvent(detail="revision_steer_pending")`
    for every steer whose text is a revision intent — a SEQUENCE-STABLE signal that
    in-flight ActionEvent/ObservationEvent CANNOT mask (unlike `latest_unprocessed_user_text`,
    whose unprocessed semantics an in-flight assistant turn invalidates — the live
    NO_REPLAN_AFTER_REVISION race: a steer at seq N followed by an in-flight write at seq N+k
    made `has_unprocessed_user_message` False so the polling guards no-op'd).

    The SOLE consumer is `_maybe_reenter_planning_for_followup` (run-entry AND mid-step), which
    re-enters PLANNING and emits the `planning` marker — consuming this. So this is True only
    while the latest `revision_steer_pending` is MORE RECENT than the latest `planning` /
    `plan_approved` marker. Idempotent across multiple steers in one turn; stateless (survives
    resume)."""
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            if e.detail in ("planning", "plan_approved"):
                return False  # consumed by a (re-)plan since the marker
            if e.detail == "revision_steer_pending":
                return True
    return False


def current_blocked_question_landing(events: list[Event]) -> bool:
    """True iff the current user-question gate is a blocked landing.

    Blocked landings preserve their legacy PAUSED/STUCK marker, then supersede it
    with ``AWAITING_USER_QUESTION`` carrying ``meta.blocked_landing``. A user
    answer appended after that status does not clear the gate; only a later
    status transition does. Therefore the current gate is determined by the
    latest StatusEvent, not by the latest event of any kind.
    """
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            return (
                e.status == ConversationStatus.AWAITING_USER_QUESTION
                and (e.meta or {}).get("blocked_landing") is True
            )
    return False


def latest_user_answers_blocked_question(events: list[Event]) -> bool:
    """True iff the latest USER message is an answer to a blocked landing.

    ``AgentLoop.run`` emits a bare RUNNING marker before its drive loop. After
    that, ``current_blocked_question_landing`` is no longer true, but the latest
    user message is still the answer to the blocked question. Look backward from
    that user message to the preceding status gate so re-plan guards can keep
    treating the text as answer-context until an agent action/message consumes it.
    """
    latest_user_index: int | None = None
    for index, e in enumerate(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            latest_user_index = index
    if latest_user_index is None:
        return False
    for e in reversed(events[:latest_user_index]):
        if isinstance(e, StatusEvent):
            return (
                e.status == ConversationStatus.AWAITING_USER_QUESTION
                and (e.meta or {}).get("blocked_landing") is True
            )
    return False


def latest_user_text(events: list[Event]) -> str | None:
    """The most recent USER MessageEvent content — NOT gated on "unprocessed". Supplies the
    re-plan framing text for the marker-driven path (`pending_revision_steer`), where
    `latest_unprocessed_user_text` returns None because an in-flight assistant turn already
    invalidated the unprocessed predicate."""
    latest: MessageEvent | None = None
    for e in events:
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            if latest is None or (e.seq or 0) >= (latest.seq or 0):
                latest = e
    if latest is None:
        return None
    return (latest.message.content or "").strip() or None


def current_revision_instruction(events: list[Event]) -> str | None:
    """[REL-RC-F] The USER's revision instruction for the CURRENT revision-planning session — the
    latest genuine ``source=USER`` MessageEvent at or before the most recent ``planning`` marker.

    Session-tied on purpose (NOT a blind latest-user-text): it excludes the ENVIRONMENT-injected
    ``force_submit_plan`` directive (source is ENVIRONMENT, not USER) AND any user message that
    arrived AFTER this planning segment began, so the synthesized-step fallback always uses the
    literal instruction that TRIGGERED this replan. Durable (event-sourced) → survives resume."""
    planning_seq: int | None = None
    for e in events:
        if isinstance(e, StatusEvent) and e.detail == "planning":
            s = e.seq or 0
            if planning_seq is None or s >= planning_seq:
                planning_seq = s
    best: MessageEvent | None = None
    for e in events:
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            s = e.seq or 0
            if planning_seq is not None and s > planning_seq:
                continue  # a user msg AFTER planning began is not what triggered this replan
            if best is None or s >= (best.seq or 0):
                best = e
    if best is None:
        return None
    return (best.message.content or "").strip() or None


_MENTION_OPEN = "<mentioned-element>"
_MENTION_CLOSE = "</mentioned-element>"


def strip_element_mention(text: str) -> str:
    """Remove the preview element-mention wrapper, preserving the user ask."""
    start = text.find(_MENTION_OPEN)
    if start < 0:
        return text.strip()
    body_start = start + len(_MENTION_OPEN)
    end = text.find(_MENTION_CLOSE, body_start)
    if end < 0:
        return text[:start].strip()
    return f"{text[:start]}{text[end + len(_MENTION_CLOSE) :]}".strip()


def revision_planning_has_prior_approval(events: list[Event]) -> bool:
    """True for a re-plan after an approved plan, false for initial planning."""
    return any(isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events)


def harvested_revision_plan_from_user(events: list[Event]) -> PlanEvent | None:
    """Build the one-step forced revision plan from the triggering user text."""
    if not in_planning_for_revision(events) or not revision_planning_has_prior_approval(events):
        return None
    instruction = current_revision_instruction(events) or latest_user_text(events) or ""
    title = strip_element_mention(instruction)[:200].strip()
    if not title:
        return None
    return PlanEvent(
        summary=title[:120],
        steps=[PlanStep(title=title)],
        revision=next_plan_revision(events),
        context=(
            "Synthesized from the pending user follow-up after repeated "
            "planning-mode edit refusals."
        ),
    )


def current_planning_segment_start_seq(events: list[Event]) -> int | None:
    """Seq that bounds the currently-pending planning segment.

    Prefer the durable ``planning`` marker emitted by request_plan/enter_planning.
    Initial plan mode historically may not have such a marker, so fall back to the
    latest USER message. The caller is still responsible for only using this while
    the loop is actually in PLANNING mode.
    """
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "planning":
            return e.seq or 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return e.seq or 0
    return None


def plan_submitted_since_current_planning(events: list[Event]) -> bool:
    """True iff the current planning segment already produced a real plan.

    Normal ``submit_plan`` is intercepted into a ``PlanEvent`` rather than recorded
    as an ``ActionEvent``, but tests and older logs may still carry an action. Treat
    either as satisfying the planning gate so prose harvesting never runs after a
    real submission for this follow-up.
    """
    start_seq = current_planning_segment_start_seq(events)
    if start_seq is None:
        return False
    for e in events:
        if (e.seq or 0) <= start_seq:
            continue
        if isinstance(e, PlanEvent):
            return True
        if isinstance(e, ActionEvent):
            tool = e.tool_call.tool_name if e.tool_call is not None else None
            if tool == "submit_plan":
                return True
    return False


def next_plan_revision(events: list[Event]) -> int:
    """The revision number Planner.plan_from_args would assign next."""
    return 1 + sum(1 for e in events if isinstance(e, PlanEvent))


def plan_nudges_since_current_planning(events: list[Event]) -> int:
    """Count soft prose-planning nudges in the current pending planning segment.

    Event-derived counterpart to the old in-memory ``_plan_nudges`` runway. It is
    bounded by the latest planning marker (or latest USER message for initial-plan
    mode) and releases to 0 as soon as a PlanEvent/submit_plan lands.
    """
    from .engine import _PLAN_NUDGE

    start_seq = current_planning_segment_start_seq(events)
    if start_seq is None:
        return 0
    count = 0
    for e in events:
        if (e.seq or 0) <= start_seq:
            continue
        if isinstance(e, PlanEvent):
            return 0
        if isinstance(e, ActionEvent):
            tool = e.tool_call.tool_name if e.tool_call is not None else None
            if tool == "submit_plan":
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


def prose_plan_force_submit(events: list[Event]) -> bool:
    """True iff the current planning segment has an unsatisfied force-submit marker.

    Unlike ``revision_force_submit``, this is deliberately planning-segment scoped
    rather than revision-only so initial-plan prose loops can use the same marker
    and driver narrowing. It releases on PlanEvent/submit_plan/approval.
    """
    start_seq = current_planning_segment_start_seq(events)
    if start_seq is None:
        return False
    for e in reversed(events):
        if (e.seq or 0) <= start_seq:
            return False
        if isinstance(e, PlanEvent):
            return False
        if isinstance(e, ActionEvent):
            tool = e.tool_call.tool_name if e.tool_call is not None else None
            if tool == "submit_plan":
                return False
        if isinstance(e, StatusEvent):
            if e.detail == "plan_approved":
                return False
            if e.detail == "force_submit_plan":
                return True
    return False


def prose_plan_harvested(events: list[Event]) -> bool:
    """Has REL-RC-N already harvested a prose plan in this planning segment?"""
    start_seq = current_planning_segment_start_seq(events)
    if start_seq is None:
        return False
    for e in reversed(events):
        if (e.seq or 0) <= start_seq:
            return False
        if isinstance(e, StatusEvent) and e.detail == "prose_plan_harvested":
            return True
    return False


_PROSE_PLAN_STEP_RE = re.compile(r"^\s*(?:[-*+]\s+|(?:\d{1,2}|[A-Za-z])[\.)]\s+)(.+?)\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMPH_RE = re.compile(r"(\*\*|__|\*|_)([^*_].*?)\1")


def _strip_think_blocks(text: str) -> str:
    return strip_think_spans(text)


def _strip_prose_step_markdown(text: str) -> str:
    text = _MD_LINK_RE.sub(r"\1", text)
    text = re.sub(r"^\[[ xX]\]\s+", "", text.strip())
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # A small fixed-point loop handles nested/simple emphasis without trying to
    # parse markdown; this is a recovery heuristic, not a renderer.
    for _ in range(3):
        new = _MD_EMPH_RE.sub(r"\2", text)
        if new == text:
            break
        text = new
    return text.strip(" \t`*_")


def prose_plan_steps_from_text(text: str, *, limit: int = 8) -> list[str]:
    """Parse numbered/bulleted prose-plan lines into clean step titles."""
    out: list[str] = []
    clean = _strip_think_blocks(text)
    for line in clean.splitlines():
        mt = _PROSE_PLAN_STEP_RE.match(line)
        if mt is None:
            continue
        step = _strip_prose_step_markdown(mt.group(1))
        if not step:
            continue
        out.append(step[:200])
        if len(out) >= limit:
            break
    return out


def latest_prose_plan_message(events: list[Event]) -> str | None:
    """Latest assistant prose emitted in the current planning segment."""
    start_seq = current_planning_segment_start_seq(events)
    if start_seq is None:
        return None
    for e in reversed(events):
        if (e.seq or 0) <= start_seq:
            return None
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
            content = (e.message.content or "").strip()
            if content:
                return content
    return None


def latest_prose_plan_summary(events: list[Event]) -> str | None:
    """Best short summary from the latest assistant prose plan message."""
    content = latest_prose_plan_message(events)
    if not content:
        return None
    clean = _strip_think_blocks(content)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    plan_lines = [line for line in lines if line.lower().startswith("plan")]
    raw = plan_lines[-1] if plan_lines else (lines[0] if lines else "")
    summary = _strip_prose_step_markdown(raw)
    return summary[:200] if summary else None


def latest_agent_prose_message(events: list[Event]) -> str | None:
    """Latest assistant prose message, stripped of hidden think blocks.

    Used by REL-RC-P's synthetic finish summary. Restricting this to
    ``source=AGENT`` avoids feeding system reminders or user instructions back as
    the completion summary.
    """
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
            content = _strip_think_blocks(e.message.content or "").strip()
            if content:
                return content
    return None


def harvest_prose_plan_steps(events: list[Event]) -> list[PlanStep]:
    """REL-RC-N: recover PlanSteps from the latest prose plan, or one user step.

    The fallback is intentionally the literal pending user instruction. That mirrors
    REL-RC-F's adjudicable one-step fallback while avoiding a stranded execution
    with no tracker.
    """
    content = latest_prose_plan_message(events) or ""
    titles = prose_plan_steps_from_text(content, limit=8)
    if not titles:
        instruction = current_revision_instruction(events)
        if instruction:
            titles = [instruction[:200]]
    return [PlanStep(title=title) for title in titles[:8] if title]


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
    successful_actions = _successful_action_ids(events)
    count = 0
    for e in events:
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if approval_seq is not None and (e.seq or 0) <= approval_seq:
            continue
        if e.meta.get("verify_probe"):
            continue  # the finish gate's own probe is not agent work (#6 leak)
        if e.id in successful_actions and e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
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
    # Progress = the agent did REAL WORK after the message: a tool call
    # (ActionEvent), an observation (ObservationEvent), a plan change (PlanEvent),
    # or an assistant message (AGENT MessageEvent). A pure terminal/status marker
    # (RUNNING/FINISHED/STUCK/ERROR StatusEvent) does NOT count as processing the
    # turn. The engine appends the terminal marker at run end, so a follow-up that
    # lands during finalization — just before that marker — was previously MASKED
    # as "already processed" and stranded forever (no re-kick, no re-entry). With
    # status markers excluded, such a turn is correctly UNPROCESSED so the runtime
    # re-kicks and run() re-enters to handle it.
    #
    # The basis is deliberately STRICT to avoid false negatives on synthetic no-op
    # turns: system-reminder / F9-dedup injections carry EventSource.ENVIRONMENT or
    # SYSTEM (only AGENT messages count here), and status events no longer count at
    # all — neither can spuriously consume a real user turn. `latest_unprocessed_
    # user_text` reads through this same predicate, so it stays consistent.
    last_progress = max(
        (
            e.seq or 0
            for e in events
            if isinstance(e, (ActionEvent, ObservationEvent, PlanEvent))
            or (isinstance(e, MessageEvent) and e.source == EventSource.AGENT)
        ),
        default=0,
    )
    return last_user > last_progress


def latest_unprocessed_user_text(events: list[Event]) -> str | None:
    """The text of the most recent USER message IFF it is still unprocessed
    (arrived after the most recent agent activity — see has_unprocessed_user_message).
    Returns None when there is no fresh user turn. Used by Bug-12 follow-up
    re-planning to inspect the new instruction's intent."""
    if not has_unprocessed_user_message(events):
        return None
    latest: MessageEvent | None = None
    for e in events:
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            if latest is None or (e.seq or 0) >= (latest.seq or 0):
                latest = e
    if latest is None:
        return None
    return (latest.message.content or "").strip() or None


_TERMINAL_IDLE_FOLLOWUP_STATUSES = frozenset(
    {
        ConversationStatus.IDLE,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
    }
)


def latest_terminal_idle_followup_user_text(events: list[Event]) -> str | None:
    """Latest USER text that arrived after a terminal/idle status and has not been
    consumed by a re-plan boundary.

    This is intentionally narrower than ``latest_unprocessed_user_text`` in one
    direction and broader in another: it requires a preceding terminal/idle marker
    (so non-terminal mid-run steer semantics stay unchanged), but it ignores later
    agent activity until a ``planning``/``plan_approved`` marker consumes the
    follow-up. That covers terminal-IDLE pickups where an assistant/action event has
    already masked the usual unprocessed-user predicate before the write gate runs.
    """
    latest_user: MessageEvent | None = None
    for e in reversed(events):
        if latest_user is None:
            if isinstance(e, StatusEvent) and e.detail in ("planning", "plan_approved"):
                return None
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                latest_user = e
            continue
        if isinstance(e, StatusEvent):
            if e.detail in ("planning", "plan_approved"):
                return None
            if e.status in _TERMINAL_IDLE_FOLLOWUP_STATUSES:
                return (latest_user.message.content or "").strip() or None
    return None


# Bug 12 (§11.4) — change/revision verbs that mark a follow-up as MUTATING (it
# asks for a workspace change), so a follow-up on an approved/finished build must
# re-enter PLANNING and go through a revised plan rather than a free write on the
# stale plan. Word-boundary matched (so "add" doesn't fire inside "additional",
# "fix" not inside "prefix"). The list is deliberately broad: the failure mode is
# an UNAUTHORIZED mutation, so ambiguity biases toward re-planning; only a clear
# pure question (no change verb + reads like a question) is exempted.
_CHANGE_VERB_RE = re.compile(
    r"\b("
    r"revise|revised|change|changed|add|adds|added|update|updated|remove|removed|"
    r"delete|deleted|replace|replaced|fix|fixed|edit|edited|modify|modified|adjust|"
    r"adjusted|tweak|tweaked|swap|swapped|rename|renamed|move|moved|set|insert|append|"
    r"instead|also|different|differently|implement|build|create|rebuild|redo|rework|"
    r"convert|increase|decrease|enlarge|shrink|recolor|restyle"
    r")\b",
    re.IGNORECASE,
)

# Leading words that mark a clear, non-mutating QUESTION (answerable without a
# forced re-plan). Only consulted when NO change verb is present.
_QUESTION_LEAD_RE = re.compile(
    r"^\s*(what|which|why|how|when|where|who|whose|whom|is|are|was|were|do|does|did|"
    r"can|could|will|would|should|has|have|had|may|might)\b",
    re.IGNORECASE,
)


def is_revision_intent(text: str) -> bool:
    """Bug 12 (§11.4) — conservative predicate: does this follow-up ask for a
    workspace CHANGE (→ must re-enter PLANNING for a revised plan), or is it a
    pure non-mutating question (→ answerable without a forced re-plan)?

    Rule (bias toward re-planning, the safe contract — an unauthorized mutation
    on a stale plan is the failure mode):
      * any change/revision verb present → True (re-plan).
      * otherwise, a clear question (leads with a question word or ends with '?')
        → False (answer it, no dead-end).
      * anything else ambiguous → True (re-plan)."""
    t = (text or "").strip()
    if not t:
        return False
    if _CHANGE_VERB_RE.search(t):
        return True
    if t.endswith("?") or _QUESTION_LEAD_RE.match(t):
        return False
    return True
