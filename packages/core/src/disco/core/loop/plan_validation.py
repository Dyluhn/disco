"""Plan validation and prose-plan harvesting — split from ``loop/signals.py``.

Pure, stateless log-derived signal functions for planning-segment tracking,
prose-plan harvesting, and plan-submission detection.  Every function is a pure
projection of an event list to a verdict, with no instance state, no emission,
and no I/O.
"""

from __future__ import annotations

import re

from ..events import (
    ActionEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from ..llm import OperatingMode
from ..think import strip_think_spans

APPROVED_PLAN_VERIFIER_REPAIR = "approved_plan_verifier_repair"

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


def _step_is_finish_intent(step: PlanStep) -> bool:
    """Whether a plan step's title/detail reads as a finish/handoff intent."""
    text = f"{step.title} {step.detail or ''}".strip()
    if not text:
        return False
    if _ACTIONABLE_BUILD_VERB_RE.search(text):
        return False
    return _FINISH_INTENT_RE.search(text) is not None


def _plan_approval_seqs(events: list[Event]) -> list[int]:
    """Return seqs of all ``plan_approved`` markers."""
    return [
        e.seq or 0 for e in events if isinstance(e, StatusEvent) and e.detail == "plan_approved"
    ]


def _latest_approval_status(events: list[Event]) -> StatusEvent | None:
    """Return the latest ``plan_approved`` StatusEvent, or None."""
    for event in reversed(events):
        if isinstance(event, StatusEvent) and event.detail == "plan_approved":
            return event
    return None


def _plan_by_transition(events: list[Event], approval: StatusEvent) -> PlanEvent | None:
    """Resolve the plan bound by a typed approval transition."""
    from ..dod import predicate_fingerprints

    transition = approval.plan_verification_transition
    if transition is None:
        return None
    plan = next(
        (
            event
            for event in events
            if isinstance(event, PlanEvent)
            and event.id == transition.new_plan_event_id
            and event.revision == transition.new_plan_revision
            and (event.seq or 0) < (approval.seq or 0)
        ),
        None,
    )
    if plan is None:
        return None
    actual_fingerprints = predicate_fingerprints(
        [step.done_condition for step in plan.steps if step.done_condition is not None]
    )
    return plan if actual_fingerprints == transition.new_predicate_fingerprints else None


def _plan_by_legacy_approval(events: list[Event], approval: StatusEvent) -> PlanEvent | None:
    """Reconstruct the plan from a legacy (untyped) approval marker."""
    candidates = [
        event
        for event in events
        if isinstance(event, PlanEvent) and (event.seq or 0) < (approval.seq or 0)
    ]
    return max(candidates, key=lambda event: (event.seq or 0, event.revision), default=None)


def _done_condition_identities(plan: PlanEvent) -> set[str]:
    """Stable identities of a plan's declarative completion predicates."""
    identities: set[str] = set()
    for step in plan.steps:
        predicate = step.done_condition
        if predicate is not None:
            identities.add(predicate.model_dump_json())
    return identities


def _approved_plan_at(events: list[Event], approval_seq: int) -> PlanEvent | None:
    """The PlanEvent that the approval marker at ``approval_seq`` approved."""
    best: PlanEvent | None = None
    for event in events:
        if isinstance(event, PlanEvent) and (event.seq or 0) <= approval_seq:
            if best is None or (event.seq or 0) >= (best.seq or 0):
                best = event
    return best


_MENTION_OPEN = "<mentioned-element>"
_MENTION_CLOSE = "</mentioned-element>"

_PROSE_PLAN_STEP_RE = re.compile(r"^\s*(?:[-*+]\s+|(?:\d{1,2}|[A-Za-z])[\.)]\s+)(.+?)\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMPH_RE = re.compile(r"(\*\*|__|\*|_)([^*_].*?)\1")


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


def _harvested_revision_plan_from_user(
    events: list[Event],
    *,
    in_planning: bool,
    has_prior_approval: bool,
    instruction: str | None,
    revision: int,
) -> PlanEvent | None:
    """Build the one-step forced revision plan from the triggering user text."""
    if not in_planning or not has_prior_approval:
        return None
    title = strip_element_mention(instruction or "")[:200].strip()
    if not title:
        return None
    return PlanEvent(
        summary=title[:120],
        steps=[PlanStep(title=title)],
        revision=revision,
        context=(
            "Synthesized from the pending user follow-up after repeated "
            "planning-mode edit refusals."
        ),
    )


def current_planning_segment_start_seq(events: list[Event]) -> int | None:
    """Seq that bounds the currently-pending planning segment."""
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "planning":
            return e.seq or 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return e.seq or 0
    return None


def plan_submitted_since_current_planning(events: list[Event]) -> bool:
    """True iff the current planning segment already produced a real plan."""
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
    """Count soft prose-planning nudges in the current pending planning segment."""
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
        # Label first, legacy content second — the nudge text is repetition-aware
        # from 2026-08-07b, so byte-equality alone would undercount it.
        from ._signals_planning import _is_plan_nudge_event

        if _is_plan_nudge_event(e, _PLAN_NUDGE):
            count += 1
    return count


def prose_plan_force_submit(events: list[Event]) -> bool:
    """True iff the current planning segment has an unsatisfied force-submit marker."""
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


def _strip_think_blocks(text: str) -> str:
    return strip_think_spans(text)


def _strip_prose_step_markdown(text: str) -> str:
    text = _MD_LINK_RE.sub(r"\1", text)
    text = re.sub(r"^\[[ xX]\]\s+", "", text.strip())
    text = re.sub(r"`([^`]+)`", r"\1", text)
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
    """Latest assistant prose message, stripped of hidden think blocks."""
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
            content = _strip_think_blocks(e.message.content or "").strip()
            if content:
                return content
    return None


def _latest_planning_seq(events: list[Event]) -> int | None:
    """Seq of the most recent ``planning`` marker."""
    planning_seq: int | None = None
    for event in events:
        if isinstance(event, StatusEvent) and event.detail == "planning":
            seq = event.seq or 0
            if planning_seq is None or seq >= planning_seq:
                planning_seq = seq
    return planning_seq


def current_revision_instruction(events: list[Event]) -> str | None:
    """Return the user instruction that triggered the current replan."""
    planning_seq = _latest_planning_seq(events)
    best: MessageEvent | None = None
    for event in events:
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            seq = event.seq or 0
            if planning_seq is not None and seq > planning_seq:
                continue
            if best is None or seq >= (best.seq or 0):
                best = event
    if best is None:
        return None
    return (best.message.content or "").strip() or None


def harvest_prose_plan_steps(events: list[Event]) -> list[PlanStep]:
    """REL-RC-N: recover PlanSteps from the latest prose plan, or one user step."""
    content = latest_prose_plan_message(events) or ""
    titles = prose_plan_steps_from_text(content, limit=8)
    if not titles:
        instruction = current_revision_instruction(events)
        if instruction:
            titles = [instruction[:200]]
    return [PlanStep(title=title) for title in titles[:8] if title]


# ---------------------------------------------------------------------------
# Planning-mode and revision-steer signals
# ---------------------------------------------------------------------------


def in_planning_for_revision(events: list[Event]) -> bool:
    """BW-01 — True iff a plan REVISION is currently pending."""
    planning_seq: int | None = None
    execution_seq: int | None = None
    for e in events:
        if isinstance(e, StatusEvent):
            if e.detail == "planning":
                planning_seq = e.seq or 0
            elif e.detail in {"plan_approved", "plan_revision_idempotent"}:
                execution_seq = e.seq or 0
    if planning_seq is None:
        return False
    return execution_seq is None or planning_seq > execution_seq


def effective_mode(
    events: list[Event],
    *,
    planning: OperatingMode = OperatingMode.PLANNING,
    execution: OperatingMode = OperatingMode.LONG_HORIZON,
    default: OperatingMode = OperatingMode.PLANNING,
) -> OperatingMode:
    """Return the event-log-derived operating mode for a plan-gated loop."""
    planning_seq: int | None = None
    execution_seq: int | None = None
    for event in events:
        if isinstance(event, StatusEvent):
            if event.detail == "planning":
                planning_seq = event.seq or 0
            elif event.detail in {"plan_approved", "plan_revision_idempotent"}:
                execution_seq = event.seq or 0
    if execution_seq is not None and (planning_seq is None or execution_seq > planning_seq):
        return execution
    if planning_seq is not None:
        return planning
    return default


def revision_force_submit(events: list[Event]) -> bool:
    """True iff a REVISION re-plan has been ESCALATED to forced-submit."""
    if not in_planning_for_revision(events):
        return False
    for e in reversed(events):
        if isinstance(e, ActionEvent):
            tool = e.tool_call.tool_name if e.tool_call is not None else None
            if tool == "submit_plan":
                return False
        if isinstance(e, StatusEvent) and e.detail == "force_submit_plan":
            return True
    return False


def pending_revision_steer(events: list[Event]) -> bool:
    """True iff a LIVE revision STEER marker is UNCONSUMED."""
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            if e.detail in ("planning", "plan_approved"):
                return False
            if e.detail == "revision_steer_pending":
                return True
    return False


def current_blocked_question_landing(events: list[Event]) -> bool:
    """True iff the current user-question gate is a blocked landing."""
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            return (
                e.status == ConversationStatus.AWAITING_USER_QUESTION
                and (e.meta or {}).get("blocked_landing") is True
            )
    return False


def latest_user_answers_blocked_question(events: list[Event]) -> bool:
    """True iff the latest USER message is an answer to a blocked landing."""
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
    """The most recent USER MessageEvent content — NOT gated on "unprocessed"."""
    latest: MessageEvent | None = None
    for e in events:
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            if latest is None or (e.seq or 0) >= (latest.seq or 0):
                latest = e
    if latest is None:
        return None
    return (latest.message.content or "").strip() or None


# --- PKG-47: Reference Packs must be read before the first plan ------------------

REFERENCE_PACK_UNREAD_DIAGNOSTIC = "reference_packs_unread"
_REFERENCE_PACK_NUDGE_CAP = 2


def bound_reference_pack_indexes(events: list[Event]) -> list[str]:
    """The ``references/<pack>/PACK.md`` paths of the packs bound to this conversation
    (the latest binding marker wins; an empty binding clears the requirement)."""
    latest: list[str] = []
    for e in events:
        if isinstance(e, MessageEvent) and "reference_packs" in (e.meta or {}):
            markers = e.meta.get("reference_packs") or []
            latest = [str(m["path"]) for m in markers if isinstance(m, dict) and m.get("path")]
    return latest


def _normalized_read_path(arguments: dict[str, object]) -> str | None:
    raw = arguments.get("path")
    if not isinstance(raw, str):
        return None
    path = raw.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/") or None


def unread_reference_pack_indexes(events: list[Event]) -> list[str]:
    """Bound PACK.md paths the agent has not read yet (any prior ``file_read`` counts)."""
    required = bound_reference_pack_indexes(events)
    if not required:
        return []
    read: set[str] = set()
    for e in events:
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name == "file_read":
                path = _normalized_read_path(e.tool_call.arguments)
                if path is not None:
                    read.add(path)
    return [path for path in required if path not in read]


def reference_pack_nudges_since_current_planning(events: list[Event]) -> int:
    """How many times this planning segment already sent the read-first reminder."""
    start_seq = current_planning_segment_start_seq(events)
    if start_seq is None:
        return 0
    return sum(
        1
        for e in events
        if (e.seq or 0) > start_seq
        and isinstance(e, MessageEvent)
        and (e.meta or {}).get("diagnostic") == REFERENCE_PACK_UNREAD_DIAGNOSTIC
    )


def reference_pack_read_first_reminder(unread: list[str]) -> MessageEvent:
    listing = "\n".join(f"- {path}" for path in unread)
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content=(
                "<system-reminder>\nThe user selected reference packs for this build and "
                "you have not read their index yet. Before proposing a plan, file_read each "
                f"of these, then read the listed files that matter:\n{listing}\n"
                "Then call submit_plan.\n</system-reminder>"
            ),
        ),
        meta={"diagnostic": REFERENCE_PACK_UNREAD_DIAGNOSTIC},
    )


def reference_packs_block_submission(events: list[Event]) -> list[str]:
    """The unread indexes that should turn this submit_plan back into reading, or []
    once the reminder cap is reached (the plan is then accepted, never terminated)."""
    unread = unread_reference_pack_indexes(events)
    if not unread:
        return []
    if reference_pack_nudges_since_current_planning(events) >= _REFERENCE_PACK_NUDGE_CAP:
        return []
    return unread
