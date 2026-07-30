"""Plan/alternatives builders: `submit_plan` → PlanEvent (+ C18 done-condition
harvest) and `ask_user` options → AlternativesEvent.

This module is the compatibility facade for the plan policy slice. The pure
collaborators live in three target-neutral modules, each with one implementation
owner and no duplicated policy:

- ``plan_command_validation`` — shell/command classification (lifecycle/probe
  rejection for command done-conditions).
- ``plan_done_conditions`` — raw/generic/AppKit done-condition validation.
- ``planner_arguments`` — submit_plan/ask_user argument coercion.

Every old public and private name, signature, monkeypatch seam, plan/event
field, done-condition error string and ordering, command-classification
decision, predicate side effect, alternative order/id, and malformed-input
behavior is preserved here as a re-export so existing imports are unchanged.
"""

from __future__ import annotations

import ipaddress  # noqa: F401 — compatibility facade binding
import logging
import re  # noqa: F401 — compatibility facade binding
import shlex  # noqa: F401 — compatibility facade binding
from collections.abc import Callable  # noqa: F401 — compatibility facade binding
from pathlib import PurePosixPath  # noqa: F401 — compatibility facade binding
from typing import TYPE_CHECKING
from urllib.parse import urlsplit  # noqa: F401 — compatibility facade binding

from ..appkit.spec import (  # noqa: F401 — compatibility facade bindings
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
)
from ..dod import (  # noqa: F401 — compatibility facade bindings
    CommandExitPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    predicate_from_obj,
)
from ..dod_util import _hard_deny_reason  # noqa: F401 — compatibility facade binding
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
from ..view import _latest_plan
from ..workspace_paths import (  # noqa: F401 — compatibility facade binding
    strip_redundant_workspace_prefix,
)
from .messages import (
    _PLAN_EXPLORE_FORCE,
    _PLAN_EXPLORE_READ_CAP,
    _REPLAN_FRAMING,
    _WORKFLOW_ROUTER_EXPLORE_FORCE,
    _render_replan_plan_digest,
)
from .plan_command_validation import (  # noqa: F401 — re-exported for back-compat
    _COMMAND_SEPARATOR_TOKENS,
    _CURL_LONG_OPTIONS_WITH_VALUES,
    _CURL_SHORT_OPTIONS_WITH_VALUES,
    _ENV_OPTIONS_WITH_VALUES,
    _HTTPIE_LONG_OPTIONS_WITH_VALUES,
    _HTTPIE_SHORT_OPTIONS_WITH_VALUES,
    _PACKAGE_OPTIONS_WITH_VALUES,
    _SERVER_EXECUTABLES,
    _SERVER_SCRIPT_NAMES,
    _TIMEOUT_OPTIONS_WITH_VALUES,
    _VITE_OPTIONS_WITH_VALUES,
    _WGET_LONG_OPTIONS_WITH_VALUES,
    _WGET_SHORT_OPTIONS_WITH_VALUES,
    _basename,
    _command_segments,
    _discard_option_prefix,
    _extract_curl_url_targets,
    _http_client_option_sets,
    _is_concrete_http_hostname,
    _is_local_preview_hostname,
    _is_loopback_hostname,
    _package_command,
    _probe_target_issue,
    _probe_targets,
    _segment_launches_local_server,
    _segment_launches_via_build_tool,
    _segment_launches_via_named_tool,
    _segment_launches_via_package_manager,
    _segment_launches_via_python,
    _segment_launches_via_runtime_server,
    _segment_probe_issue,
    _shell_nested_command,
    _shell_tokens,
    _strip_command_prefix,
    _tokens_start_background_work,
    _unsafe_command_done_condition_reason,
)
from .plan_done_conditions import (  # noqa: F401 — re-exported for back-compat
    _APPKIT_CANONICAL_DOD_FILES,
    _command_condition_error,
    _comparable_workspace_path,
    _file_exists_path_error,
    _file_parent_child_errors,
    _http_ok_condition_error,
    validate_appkit_plan_done_conditions,
    validate_plan_conditions,
    validate_plan_done_conditions,
    validate_raw_plan_done_conditions,
)
from .planner_arguments import (  # noqa: F401 — re-exported for back-compat
    _AUTONOMOUS_ASSUMPTIONS_PREAMBLE,
    _MAX_HARVESTED_STEPS,
    _MAX_PLAN_BLOB_BYTES,
    _PLAN_TAG_RE,
    _PLAN_WRAPPER_TAGS,
    _STEPS_ENVELOPE_RE,
    _TITLE_HARVEST_RE,
    _coerce_alternative_options,
    _coerce_dict_option,
    _coerce_step,
    _coerce_string_option,
    _extract_option_arguments,
    _extract_option_tool_name,
    _harvest_steps,
    _strip_plan_tags,
    _unwrap_steps_item_wrapper,
    _with_autonomous_assumptions,
)
from .turn_control import _CONTINUE_OPTION_ID

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")


class Planner:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def discard_plan_predicates(self, revision: int) -> None:
        """Remove transient C18 entries for a plan rejected before persistence."""
        predicates = getattr(self._loop, "_plan_step_predicates", None)
        if not isinstance(predicates, dict):
            return
        for key in tuple(predicates):
            if key[0] == revision:
                predicates.pop(key, None)

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
            content = (
                _WORKFLOW_ROUTER_EXPLORE_FORCE
                if self._loop._workflow_router_phase_active()
                else _PLAN_EXPLORE_FORCE.format(n=self._loop._plan_explore_reads)
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=content),
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
        events = await self._loop._events()
        is_revision = any(
            isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events
        )
        if is_revision:
            # Thread the CURRENT approved plan + the new instruction INLINE into the
            # framing so the model revises the actual plan (and emits submit_plan) instead
            # of reconstructing it from a long post-build history and narrating it in prose
            # (the intermittent NO_REPLAN narrate-without-submit residual).
            plan = _latest_plan(events)
            digest = (
                _render_replan_plan_digest(plan)
                if plan is not None and getattr(plan, "steps", None)
                else "  (the prior plan's steps are not on record — restate the full plan)"
            )
            content = _REPLAN_FRAMING.format(current_plan=digest, instruction=text.strip())
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=content),
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
        # A rejected submit_plan is allowed to recover with a corrected plan at
        # the same revision. Clear its transient advisory map before rebuilding,
        # otherwise an omitted condition from the rejected attempt survives and
        # can be evaluated against the corrected plan.
        self.discard_plan_predicates(revision)
        # [REL-RC A3] Only ITERATE `steps` when it is a real list. A model that mis-routes the
        # step array as a STRING into the `steps` slot would otherwise char-iterate ("abc" →
        # 'a','b','c' → bogus single-char steps); that string is instead routed to _harvest_steps
        # below. done_condition is parsed ONTO each PlanStep via _coerce_step (resume-durable).
        _raw_steps = _unwrap_steps_item_wrapper(arguments.get("steps"))
        if isinstance(_raw_steps, list):
            for s in _raw_steps:
                step = _coerce_step(s)
                if step is not None:
                    steps.append(step)
        # [REL-RC A3] If no steps parsed, attempt the bounded regex-harvest recovery for steps the
        # model serialized into the wrong submit_plan parameter (no parser invoked; fail-closed).
        if not steps:
            steps.extend(_harvest_steps(arguments))
        # When the model submits a summary but NO real steps, keep `steps` EMPTY
        # rather than inserting a fake "(the planner returned no concrete steps)"
        # placeholder — that rendered as a broken numbered "1." step in the UI.
        # Every plan consumer already guards `not plan.steps` (the honest no-steps
        # state), so the UI shows a clean summary-only plan card instead.
        summary = _strip_plan_tags(arguments.get("summary")) or "Proposed plan"
        context = _strip_plan_tags(arguments.get("context") or arguments.get("rationale"))
        if getattr(self._loop, "_autonomous", False):
            context = _with_autonomous_assumptions(context)
        # C18 — harvest the predicates (revision-scoped). We walk the raw
        # args (not the rebuilt `steps`) so we can preserve the 1-based
        # step index even when the title/format was leniently coerced.
        # [REL-RC A3] guard: only a real list carries indexed predicates (a string never does).
        _rs = _unwrap_steps_item_wrapper(arguments.get("steps"))
        raw_steps = _rs if isinstance(_rs, list) else []
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
                    one_based,
                    revision,
                    cond,
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
        options = _coerce_alternative_options(raw_options)
        # `ask_user` semantics: the question text is the primary signal; options
        # are optional clickable alternatives. With no options, the loop emits
        # the question as a model message instead of building an AlternativesEvent
        # (handled by the caller — see the ASK-USER GATE).
        if not options:
            return None
        summary = str(
            arguments.get("question") or arguments.get("summary") or arguments.get("goal") or ""
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
