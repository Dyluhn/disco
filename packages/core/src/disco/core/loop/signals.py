"""Pure, stateless log-derived signal functions for the agent loop.

Compatibility facade: planning, stuck, and read-churn policy now live in
``_signals_planning``, ``_signals_stuck``, and ``_signals_read_churn``.
This module re-exports every public name so existing import paths remain
unchanged.  Call sites read ``signals.<name>(...)``.

Extracted from engine.py (the AgentLoop god-class) as plain module functions:
every one is a pure projection of an event list (or a View) to a verdict, with
no instance state, no emission, and no I/O. They were `@staticmethod`s on
`AgentLoop`; the bodies are byte-identical.
"""

from __future__ import annotations

import posixpath
import re
from typing import TYPE_CHECKING

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    latest_workspace_run_intent,
    pending_workspace_run_intent,
)
from ..llm import OperatingMode
from ..view import effective_plan_progress
from ..workspace_paths import strip_redundant_workspace_prefix

# Re-export the planning, stuck, and read-churn policy functions from their
# dedicated collaborator modules so ``signals.<name>`` remains the single
# import surface for all callers.
from ._signals_planning import (  # noqa: F401 — compatibility re-exports
    _BOOKKEEPING_TOOLS,
    _NON_PRODUCTIVE_TOOLS,
    _SYNTHETIC_FINISH_RESET_STATUSES,
    APPROVED_PLAN_VERIFIER_REPAIR,
    _done_condition_identities,
    _event_seq,
    _is_successful_productive_action,
    _paired_action_ids,
    _plan_approval_seqs,
    _successful_action_ids,
    approved_plan_verifier_repair_event,
    current_revision_instruction,
    finish_intent_replan_after_prior_productive_work,
    harvest_prose_plan_steps,
    harvested_revision_plan_from_user,
    is_successful_productive_action,
    latest_approved_plan,
    plan_is_verifier_repair,
    plan_step_lag_signal,
    planning_turns_since_replan,
    successful_action_ids,
    superseded_plan_owned_failure,
    verifier_repair_execution_active,
    verifier_repair_planning_active,
)
from ._signals_read_churn import (  # noqa: F401 — compatibility re-exports
    _READ_CHURN_LADDER_AT,
    _READ_CHURN_RESET_TOOLS,
    _READ_CHURN_SMALL_LIMIT,
    _READ_CHURN_WARNING_COUNTS,
    _READ_LINES_HEADER_RE,
    READ_CHURN_NUDGE_DIAGNOSTIC,
    ReadChurnState,
    read_churn_state,
)
from ._signals_stuck import (  # noqa: F401 — compatibility re-exports
    _STUCK_ESCAPE_BLOCKABLE_TOOLS,
    PROSE_NOOP_REPAIR_DIAGNOSTIC,
    STUCK_ESCAPE_BLOCK_DETAIL_PREFIX,
    STUCK_ESCAPE_REFUSAL_ERROR_PREFIX,
    SYNTHETIC_FINISH_ATTEMPT_DETAIL,
    actionless_pause_count_current_execution_segment,
    actions_since_last_resume,
    active_plan_step_count,
    bookkeeping_streak_len,
    consecutive_actionless_pauses,
    consecutive_noops,
    fresh_read_autoground_target,
    productive_action_since_approval,
    productive_actions_since_approval,
    should_synthesize_finish_after_actionless_pauses,
    stuck_escape_attempt_count,
    stuck_escape_blocked_tools,
    stuck_escape_refusal_count,
    stuck_escape_seq,
    synthetic_finish_attempted_for_current_pause,
)
from .plan_validation import (  # noqa: F401 — re-exported
    current_blocked_question_landing,
    current_planning_segment_start_seq,
    effective_mode,
    in_planning_for_revision,
    latest_agent_prose_message,
    latest_prose_plan_message,
    latest_prose_plan_summary,
    latest_user_answers_blocked_question,
    latest_user_text,
    next_plan_revision,
    pending_revision_steer,
    plan_nudges_since_current_planning,
    plan_submitted_since_current_planning,
    prose_plan_force_submit,
    prose_plan_harvested,
    prose_plan_steps_from_text,
    revision_force_submit,
    revision_planning_has_prior_approval,
    strip_element_mention,
)

if TYPE_CHECKING:
    from ..view import View


def current_run_events(events: list[Event]) -> list[Event]:
    """Return only evidence produced after the latest workspace run intent."""

    intent = latest_workspace_run_intent(events)
    if intent is None or type(intent.seq) is not int:
        return events
    return [
        event
        for event in events
        if type(event.seq) is int and event.seq > intent.seq
    ]

# _BOOKKEEPING_TOOLS, _NON_PRODUCTIVE_TOOLS, _SYNTHETIC_FINISH_RESET_STATUSES,
# _event_seq, _successful_action_ids, _is_successful_productive_action,
# successful_action_ids, and is_successful_productive_action are now defined
# in _signals_planning.py and re-exported above.

# _event_seq, _successful_action_ids, _paired_action_ids,
# _is_successful_productive_action, successful_action_ids, and
# is_successful_productive_action are now in _signals_planning.py.


# productive_action_since_approval is now in _signals_stuck.py.


# latest_approved_plan, plan_is_verifier_repair, approved_plan_verifier_repair_event,
# superseded_plan_owned_failure, verifier_repair_planning_active,
# verifier_repair_execution_active, and finish_intent_replan_after_prior_productive_work
# are now in _signals_planning.py and re-exported above.


# plan_is_verifier_repair, approved_plan_verifier_repair_event,
# superseded_plan_owned_failure, verifier_repair_planning_active, and
# verifier_repair_execution_active are now in _signals_planning.py.


# finish_intent_replan_after_prior_productive_work and its helpers
# (_step_is_finish_intent, _done_condition_identities, _approved_plan_at) are
# now in _signals_planning.py.


# _is_successful_productive_action, successful_action_ids, and
# is_successful_productive_action are now in _signals_planning.py.


# actionless_pause_count_current_execution_segment, _latest_actionless_pause_seq,
# synthetic_finish_attempted_for_current_pause, and
# should_synthesize_finish_after_actionless_pauses are now in _signals_stuck.py.


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
_NON_PRODUCT_FAILURE_CLASSES = frozenset(
    {
        "browser_action_failed",
        "browser_daemon_unavailable",
        "freshness_protocol_invalid",
    }
)


def count_recent_failures(events: list[Event]) -> int:
    """Consecutive AgentErrorEvents walking back from the tail. Reset by a
    successful ObservationEvent or a USER message (a fresh instruction).
    Interleaved ActionEvents and agent/env messages do NOT reset. Drives the
    Cluster 2 circuit breaker. Failures from non-critical informational tools
    are TRANSPARENT, as are failures from debug infrastructure. They neither
    increment nor reset the streak (a real product failure before them is still
    counted)."""
    tool_by_action = {
        e.id: e.tool_call.tool_name
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call is not None
    }
    # Host probes (the finish gate re-running a recorded check) are not the model's
    # attempts: their results neither increment nor reset the streak.
    probe_ids = {e.id for e in events if isinstance(e, ActionEvent) and e.meta.get("verify_probe")}
    streak = 0
    for e in reversed(events):
        if isinstance(e, ObservationEvent | AgentErrorEvent) and e.action_id in probe_ids:
            continue
        if isinstance(e, AgentErrorEvent):
            if e.failure_class in _NON_PRODUCT_FAILURE_CLASSES:
                continue
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


# fresh_read_autoground_target is now in _signals_stuck.py.


# Recovery boundaries end the current stuck-escape recovery episode exactly like
# a fresh user message: an approved plan revision / harvested revision / manual
# alternative pick starts a NEW execution segment, and the old segment's
# read-loop evidence must not convict it. Mirrors stuck.py's
# `_RECOVERY_BOUNDARY_DETAILS` (kept inline so neither module imports the other
# — stuck.py already imports from signals.py; a detail added there must be
# added here too). The k6g 128k canary proved the asymmetry: detection already
# reset at `plan_approved` while the quarantine episode ran on for 100+ events.
_ESCAPE_RECOVERY_BOUNDARY_DETAILS = frozenset(
    {"plan_approved", "harvested_revision_plan", "alternative_picked:manual"}
)


# stuck_escape_seq, stuck_escape_blocked_tools, stuck_escape_attempt_count,
# stuck_escape_refusal_count, and _ends_stuck_escape_episode are now in
# _signals_stuck.py.


def normalized_workspace_read_path(raw: object) -> str | None:
    """Canonical workspace-relative identity for one read/mutation path argument."""

    path = str(raw or "").strip()
    if not path:
        return None
    normalized = posixpath.normpath(strip_redundant_workspace_prefix(path))
    return normalized if normalized not in ("", ".") else None


# stuck_escape_refusal_count and stuck_escape_attempt_count are now in
# _signals_stuck.py.


# consecutive_noops is now in _signals_stuck.py.


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


# read_churn_state and its helpers (_read_churn_path, _read_churn_limit,
# _read_churn_successful_observations, _file_read_covers_remainder,
# _is_whole_file_read, _is_small_file_read, _is_read_churn_reset_action)
# are now in _signals_read_churn.py.


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


# in_planning_for_revision, effective_mode, revision_force_submit,
# pending_revision_steer, current_blocked_question_landing,
# latest_user_answers_blocked_question, latest_user_text, strip_element_mention,
# revision_planning_has_prior_approval, harvested_revision_plan_from_user,
# current_planning_segment_start_seq, plan_submitted_since_current_planning,
# next_plan_revision, plan_nudges_since_current_planning, prose_plan_force_submit,
# prose_plan_harvested, prose_plan_steps_from_text, latest_prose_plan_message,
# latest_prose_plan_summary, latest_agent_prose_message, harvest_prose_plan_steps
# are now in _signals_planning.py.


# planning_turns_since_replan is now in _signals_planning.py.


# actions_since_last_resume, consecutive_actionless_pauses,
# productive_actions_since_approval, bookkeeping_streak_len, and
# active_plan_step_count are now in _signals_stuck.py.


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
    if pending_workspace_run_intent(events) is not None:
        return True
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
    latest_intent = latest_workspace_run_intent(events)
    if _user_text_suppressed_by_intent(latest, latest_intent):
        return None
    return (latest.message.content or "").strip() or None


def _user_text_suppressed_by_intent(
    latest: MessageEvent, latest_intent: object
) -> bool:
    """Whether the latest user text is suppressed by a later run-intent event."""
    if latest_intent is None:
        return False
    intent_seq = getattr(latest_intent, "seq", None)
    if not (type(latest.seq) is int and type(intent_seq) is int):
        return False
    return latest.seq < intent_seq


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
