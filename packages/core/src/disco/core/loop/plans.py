"""Plan/alternatives builders: `submit_plan` → PlanEvent (+ C18 done-condition
harvest) and `ask_user` options → AlternativesEvent.

Extracted from engine.py as a stateful collaborator: `Planner` holds a back-ref
to its `AgentLoop` and writes the loop's per-(revision, index) C18 predicate map.
Bodies are byte-identical to the former AgentLoop methods with `self.` rewritten
to `self._loop.`.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ..dod import predicate_from_obj
from ..events import (
    ActionEvent,
    AlternativeOption,
    AlternativesEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from .messages import (
    _PLAN_EXPLORE_FORCE,
    _PLAN_EXPLORE_READ_CAP,
    _REPLAN_FRAMING,
)
from .turn_control import _CONTINUE_OPTION_ID

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

# Plan-field wrapper tags the model sometimes ECHOES into the value it submits.
# `submit_plan` takes plain JSON args, but a model (observed: MiniMax) may leak a
# stray `<summary>` / `</summary>` (or a sibling plan-field tag) into the summary /
# step text — e.g. a summary stored verbatim as "...road plane.</summary>". We strip
# ONLY this closed vocabulary of plan-template tag names (open / close / self-close,
# case-insensitive) so the stored event is clean, while never touching legitimate
# markup the plan itself is about (a plan that mentions `<div>` or `<style>` is safe).
_PLAN_WRAPPER_TAGS = (
    "summary",
    "steps",
    "step",
    "context",
    "rationale",
    "plan",
    "title",
    "detail",
    "description",
)
_PLAN_TAG_RE = re.compile(
    r"</?\s*(?:" + "|".join(_PLAN_WRAPPER_TAGS) + r")\s*/?>",
    re.IGNORECASE,
)


def _strip_plan_tags(text: str | None) -> str:
    """Defensively remove leaked plan-template wrapper tags from a submitted plan
    field (summary / step title / detail / context). Idempotent: a clean field is
    returned unchanged (modulo surrounding whitespace)."""
    if not text:
        return ""
    return _PLAN_TAG_RE.sub("", text).strip()


class Planner:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def note_planning_read_and_maybe_force(self) -> None:
        """(B2/B6) Count one consecutive PLANNING-mode read and, on CROSSING the
        cap, inject ONE forcing reminder ("you have enough context — submit_plan
        now"). Append-once: it fires only at the exact threshold, never on every
        subsequent read. The counter is reset on a submit_plan and on
        (re-)entering planning, so legitimate Phase-1 gathering below the cap is
        unchanged. Without this a re-plan model in an execution frame of mind
        reads indefinitely and never proposes a plan (the revise spinner hangs
        forever — B2 — and it free-builds without re-planning — B6)."""
        self._loop._plan_explore_reads += 1
        if self._loop._plan_explore_reads == _PLAN_EXPLORE_READ_CAP:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=_PLAN_EXPLORE_FORCE.format(
                            n=self._loop._plan_explore_reads
                        ),
                    ),
                )
            )

    async def emit_replan_framing_if_revision(self, text: str) -> None:
        """(B2/B6) When RE-entering planning after a build was already approved
        (a revision — a prior `plan_approved` StatusEvent exists) AND the user
        gave a concrete new instruction, emit ONE RE-PLANNING framing reminder so
        the model proposes a revised plan instead of free-building against the
        old plan. A first plan (no prior approval) gets nothing — unchanged.

        Safe to call AFTER enter_planning has emitted the new user turn + the
        `planning` status: neither adds a `plan_approved` status, so the revision
        check is identical whether computed before or after them."""
        if not text.strip():
            return
        is_revision = any(
            isinstance(e, StatusEvent) and e.detail == "plan_approved"
            for e in await self._loop._events()
        )
        if is_revision:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_REPLAN_FRAMING),
                )
            )

    def plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Build a PlanEvent from a `submit_plan` tool call. Defensive against the
        model's shape drift (steps as dicts or bare strings); revision counts prior
        plans so a re-plan is visibly the next iteration. `context` carries any
        markdown rationale / exploration findings the planner included.

        C18 — also harvest each step's optional `done_condition` (an advisory
        DoDPredicate) into `self._plan_step_predicates` keyed by
        (revision, 1-based index). The predicate is later evaluated when the
        agent emits `plan_step(idx, 'done')`. Malformed predicates (wrong
        `kind`, missing fields, non-dict) are silently dropped — the step
        degrades to "no predicate" exactly like a step that omitted the
        field in the first place. C18 is advisory, not a gate, so a bad
        predicate never blocks the plan from being approved."""
        steps: list[PlanStep] = []
        # Re-seed per-plan-revision (a re-plan supersedes the prior map; we
        # don't keep stale predicates from an obsolete revision).
        revision = 1 + sum(1 for e in events if isinstance(e, PlanEvent))
        for s in arguments.get("steps") or []:
            if isinstance(s, dict):
                title = _strip_plan_tags(s.get("title") or s.get("step") or s.get("name"))
                detail = _strip_plan_tags(s.get("detail") or s.get("description")) or None
                if title:
                    steps.append(PlanStep(title=title, detail=detail))
            elif isinstance(s, str):
                title = _strip_plan_tags(s)
                if title:
                    steps.append(PlanStep(title=title))
        # When the model submits a summary but NO real steps, keep `steps` EMPTY
        # rather than inserting a fake "(the planner returned no concrete steps)"
        # placeholder — that rendered as a broken numbered "1." step in the UI.
        # Every plan consumer already guards `not plan.steps` (the honest no-steps
        # state), so the UI shows a clean summary-only plan card instead.
        summary = _strip_plan_tags(arguments.get("summary")) or "Proposed plan"
        context = _strip_plan_tags(
            arguments.get("context") or arguments.get("rationale")
        )
        # C18 — harvest the predicates (revision-scoped). We walk the raw
        # args (not the rebuilt `steps`) so we can preserve the 1-based
        # step index even when the title/format was leniently coerced.
        raw_steps = arguments.get("steps") or []
        for one_based, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                continue
            cond = raw.get("done_condition")
            if not isinstance(cond, dict):
                # Optional field, absent by default — back-compat: steps
                # without a predicate behave exactly as today.
                continue
            try:
                predicate = predicate_from_obj(cond)
            except Exception:  # noqa: BLE001 — malformed predicate is advisory-only
                # A bad shape is logged once at WARNING (not ERROR — a
                # broken advisory note is not a run failure) and the
                # step silently drops the predicate.
                _LOG.warning(
                    "C18: malformed done_condition on plan step %d (revision %d); "
                    "ignoring (advisory only): %r",
                    one_based, revision, cond,
                )
                continue
            self._loop._plan_step_predicates[(revision, one_based)] = predicate
        return PlanEvent(summary=summary, steps=steps, revision=revision, context=context)

    def alternatives_from_args(
        self, arguments: dict, events: list[Event]
    ) -> AlternativesEvent | None:
        """Build an AlternativesEvent from an `ask_user` tool call's options.
        Defensive against the model's shape drift — options may come as dicts
        with various key names. Correlates with the most recent ActionEvent so
        the UI can show which step the alternatives are answering.

        Returns None when the args are too malformed to produce a useful gate;
        the loop falls back to a free-form question (see the ASK-USER GATE)."""
        raw_options = arguments.get("options") or arguments.get("alternatives") or []
        options: list[AlternativeOption] = []
        for i, opt in enumerate(raw_options):
            # BW-03 — DECOUPLE "labeled choice" from "runnable recovery pick".
            # An option does NOT need a tool_name to be a valid choice: the model
            # often enumerates plain-language paths ("Use approach A") with no
            # tool call. Those still become clickable cards — the pick is recorded
            # as a user message that steers the agent — so we must NOT drop them.
            # A `tool_name`, when present, additionally makes the pick directly
            # runnable; its absence just means "label-only".
            if isinstance(opt, str):
                # A bare string IS the choice label (no tool, no description).
                label = opt.strip()
                if not label:
                    continue
                options.append(
                    AlternativeOption(
                        id=f"opt_{i + 1}",
                        title=label,
                        description="",
                        tool_name="",
                        arguments={},
                    )
                )
                continue
            if not isinstance(opt, dict):
                continue
            tool_name = str(
                opt.get("tool_name")
                or (opt.get("tool_call") or {}).get("name")
                or (opt.get("tool_call") or {}).get("tool_name")
                or ""
            ).strip()
            args = (
                opt.get("arguments")
                or (opt.get("tool_call") or {}).get("arguments")
                or {}
            )
            if not isinstance(args, dict):
                # Malformed args shouldn't sink the whole option — drop the args,
                # keep the labeled choice. (The executor revalidates args anyway.)
                args = {}
            description = str(opt.get("description") or opt.get("why") or "").strip()
            # Broaden label extraction and NEVER fall back to a content-free
            # "Option N": derive a real label from any human-readable field, and
            # only as a last resort name the tool the pick will run. If none of
            # those exist the option carries no information to show — skip it
            # rather than render a meaningless button.
            title = str(
                opt.get("title")
                or opt.get("label")
                or opt.get("name")
                or description
                or tool_name
            ).strip()
            if not title:
                continue
            option_id = str(opt.get("id") or f"opt_{i + 1}")
            options.append(
                AlternativeOption(
                    id=option_id,
                    title=title,
                    description=description,
                    tool_name=tool_name,
                    arguments=args,
                )
            )
        # `ask_user` semantics: the question text is the primary signal; options
        # are optional clickable alternatives. With no options, the loop emits
        # the question as a model message instead of building an AlternativesEvent
        # (handled by the caller — see the ASK-USER GATE).
        if not options:
            return None
        summary = str(
            arguments.get("question")
            or arguments.get("summary")
            or arguments.get("goal")
            or ""
        ).strip()
        if not summary:
            summary = "the agent is asking which path to take"
        # Correlate with the most recent failed action (the immediate predecessor).
        failed_action_id = ""
        for e in reversed(events):
            if isinstance(e, ActionEvent):
                failed_action_id = e.id
                break
        # D2 — always append the "Continue anyway" bypass so the user is never trapped
        # at a decision gate: pick it to reset the failure streak and let the agent
        # keep going with its own judgment (pick_alternative special-cases this id).
        options = [
            *options,
            AlternativeOption(
                id=_CONTINUE_OPTION_ID,
                title="Continue anyway",
                description="Let the agent keep working with its own best next step.",
                tool_name="",  # not a tool — the loop intercepts this id
                arguments={},
            ),
        ]
        return AlternativesEvent(
            failed_action_id=failed_action_id,
            summary=summary,
            options=options,
        )
