"""Plan/alternatives builders: `submit_plan` → PlanEvent (+ C18 done-condition
harvest) and `ask_user` options → AlternativesEvent.

Extracted from engine.py as a stateful collaborator: `Planner` holds a back-ref
to its `AgentLoop` and writes the loop's per-(revision, index) C18 predicate map.
Bodies are byte-identical to the former AgentLoop methods with `self.` rewritten
to `self._loop.`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..dod import predicate_from_obj
from ..events import (
    ActionEvent,
    AlternativeOption,
    AlternativesEvent,
    Event,
    PlanEvent,
    PlanStep,
)
from .turn_control import _CONTINUE_OPTION_ID

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")


class Planner:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

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
                title = str(s.get("title") or s.get("step") or s.get("name") or "").strip()
                detail = s.get("detail") or s.get("description")
                if title:
                    steps.append(PlanStep(title=title, detail=str(detail) if detail else None))
            elif isinstance(s, str) and s.strip():
                steps.append(PlanStep(title=s.strip()))
        if not steps:
            steps = [PlanStep(title="(the planner returned no concrete steps)")]
        summary = str(arguments.get("summary") or "").strip() or "Proposed plan"
        context = str(arguments.get("context") or arguments.get("rationale") or "").strip()
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
            if not isinstance(opt, dict):
                continue
            tool_name = str(
                opt.get("tool_name")
                or (opt.get("tool_call") or {}).get("name")
                or (opt.get("tool_call") or {}).get("tool_name")
                or ""
            ).strip()
            if not tool_name:
                continue
            args = (
                opt.get("arguments")
                or (opt.get("tool_call") or {}).get("arguments")
                or {}
            )
            if not isinstance(args, dict):
                continue
            title = str(opt.get("title") or opt.get("label") or f"Option {i + 1}").strip()
            description = str(opt.get("description") or opt.get("why") or "").strip()
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
