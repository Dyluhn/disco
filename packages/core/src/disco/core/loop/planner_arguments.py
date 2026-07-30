"""Planner argument coercion — extracted from ``plans.py``.

Pure, stateless helpers that coerce raw ``submit_plan`` / ``ask_user`` tool-call
arguments into typed event objects. Nothing here touches the loop, emits events,
or reads runtime state: every function is a pure projection of raw arguments to
a coerced value.

Two responsibilities live here:

1. ``submit_plan`` step coercion — ``_coerce_step`` / ``_harvest_steps`` recover
   PlanStep values from the model's shape drift (dicts, bare strings, mis-routed
   step blobs) without invoking any parser.
2. ``ask_user`` option coercion — ``_coerce_alternative_options`` builds the
   AlternativeOption list from the model's option shape drift, decoupling a
   labeled choice from a runnable recovery pick.
"""

from __future__ import annotations

import re
from typing import Any

from ..dod import predicate_from_obj
from ..events import AlternativeOption, PlanStep

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
_AUTONOMOUS_ASSUMPTIONS_PREAMBLE = (
    "## Assumptions\n"
    "- Autonomous mode is on, so questions_v2 structured intake was skipped.\n"
    "- Defer-don't-block: ambiguous preferences will be handled with reasonable "
    "defaults and kept easy to revise.\n"
)


def _strip_plan_tags(text: object | None) -> str:
    """Defensively remove leaked plan-template wrapper tags from a submitted plan
    field (summary / step title / detail / context). Idempotent: a clean field is
    returned unchanged (modulo surrounding whitespace).

    Accepts `object | None` and coerces to `str` AFTER the falsy check: a model
    that submits a non-string value (int / list / dict) for one of these fields
    must NOT crash the plan parse — it is stringified exactly as the old
    `str(...)`-coercing code did, then tag-stripped."""
    if not text:
        return ""
    return _PLAN_TAG_RE.sub("", str(text)).strip()


def _with_autonomous_assumptions(context: str) -> str:
    if "questions_v2 structured intake was skipped" in context:
        return context
    if context:
        return f"{_AUTONOMOUS_ASSUMPTIONS_PREAMBLE}\n{context}"
    return _AUTONOMOUS_ASSUMPTIONS_PREAMBLE.strip()


def _coerce_step(s: object) -> PlanStep | None:
    """Coerce one raw submit_plan step element into a PlanStep (or None if it has no title).
    Shared by the normal `steps[]` path and the A3 harvest so both apply identical lenient
    shape handling (dict with title/step/name + optional detail + optional done_condition; or a
    bare string title)."""
    if isinstance(s, dict):
        title = _strip_plan_tags(s.get("title") or s.get("step") or s.get("name"))
        detail = _strip_plan_tags(s.get("detail") or s.get("description")) or None
        cond = s.get("done_condition")
        dc = None
        if isinstance(cond, dict):
            try:
                dc = predicate_from_obj(cond)
            except Exception:  # noqa: BLE001 — malformed predicate is advisory-only
                dc = None
        if title:
            return PlanStep(title=title, detail=detail, done_condition=dc)
    elif isinstance(s, str):
        title = _strip_plan_tags(s)
        if title:
            return PlanStep(title=title)
    return None


# [REL-RC A3] Recovery for MiniMax-M3's nested-array tool-arg SERIALIZATION failure: the model
# authors correct structured steps but routes the whole {steps:[{title,…}],…} object STRINGIFIED
# into the wrong submit_plan parameter, leaving the real `steps` kwarg empty (live-proven:
# revise_after_finish run001 rev4). We recover the step titles WITHOUT invoking any parser/eval —
# a bounded, non-backtracking regex over a size-capped input (Codex-gated x5: ast.literal_eval has
# unbounded parser-stack-overflow surface via in-string brackets and unbracketed unary nesting; a
# regex has none). Strictly gated on `not steps` by the caller, so worst case (0 matches) is
# exactly today's behavior — it can never regress a plan that already parsed steps.
_MAX_PLAN_BLOB_BYTES = 65_536  # size pre-cap before any scan
_MAX_HARVESTED_STEPS = 64  # post-match coercion cap
# Non-backtracking + bounded: a `title` key (single or double quoted) → its quoted string value,
# capped at 200 chars. Single-quoted value = no inner single quotes; double-quoted = escapes ok.
# Bounded char classes with no nested/overlapping quantifiers → linear time, no ReDoS.
_TITLE_HARVEST_RE = re.compile(
    r"""(?:'title'|"title")\s*:\s*(?:'([^']{1,200})'|"((?:[^"\\]|\\.){1,200})")"""
)
# A `steps` key marker — required before harvesting from summary/context/rationale so an incidental
# `title:` outside a mis-routed steps payload can NEVER fabricate a step (Codex r5 fail-closed).
_STEPS_ENVELOPE_RE = re.compile(r"""(?:'steps'|"steps")\s*:""")


def _harvest_steps(arguments: dict) -> list[PlanStep]:
    """[REL-RC A3] Recover step titles the model mis-routed into the wrong submit_plan parameter.
    The `steps` param when it arrived AS A STRING is harvested directly (a title there is a step by
    definition); summary/context/rationale are harvested ONLY past a `steps:` envelope. No parser
    is ever invoked. Returns [] if nothing is safely recoverable."""
    out: list[PlanStep] = []
    candidates: list[tuple[str, bool]] = []  # (blob, require_steps_envelope)
    raw_steps = arguments.get("steps")
    if isinstance(raw_steps, str):
        candidates.append((raw_steps, False))  # the steps slot itself — harvest directly
    for key in ("summary", "context", "rationale"):
        v = arguments.get(key)
        if isinstance(v, str):
            candidates.append((v, True))  # require a steps: envelope (no false positives)
    for blob, require_envelope in candidates:
        if len(blob) > _MAX_PLAN_BLOB_BYTES:
            continue
        start = 0
        if require_envelope:
            env = _STEPS_ENVELOPE_RE.search(blob)
            if env is None:
                continue
            start = env.end()
        for mt in _TITLE_HARVEST_RE.finditer(blob, start):
            title = _strip_plan_tags(mt.group(1) or mt.group(2))
            if title:
                out.append(PlanStep(title=title))
            if len(out) >= _MAX_HARVESTED_STEPS:
                break
        if out:
            break  # first blob that yields a real step wins
    return out


def _unwrap_steps_item_wrapper(value: object) -> object:
    if not isinstance(value, dict) or len(value) != 1:
        return value
    key, wrapped = next(iter(value.items()))
    if key in {"item", "items"} and isinstance(wrapped, list):
        return wrapped
    return value


def _coerce_string_option(opt: str, index: int) -> AlternativeOption | None:
    """Coerce a bare-string option into a label-only AlternativeOption."""
    label = opt.strip()
    if not label:
        return None
    return AlternativeOption(
        id=f"opt_{index + 1}",
        title=label,
        description="",
        tool_name="",
        arguments={},
    )


def _extract_option_tool_name(opt: dict) -> str:
    """Extract the runnable tool_name from an option's tool_call/tool_name fields."""
    return str(
        opt.get("tool_name")
        or (opt.get("tool_call") or {}).get("name")
        or (opt.get("tool_call") or {}).get("tool_name")
        or ""
    ).strip()


def _extract_option_arguments(opt: dict) -> dict:
    """Extract the option's arguments, dropping malformed args to keep the choice."""
    args = opt.get("arguments") or (opt.get("tool_call") or {}).get("arguments") or {}
    if not isinstance(args, dict):
        # Malformed args shouldn't sink the whole option — drop the args,
        # keep the labeled choice. (The executor revalidates args anyway.)
        args = {}
    return args


def _coerce_dict_option(opt: dict, index: int) -> AlternativeOption | None:
    """Coerce one dict option into an AlternativeOption, or None when content-free.

    BW-03 — DECOUPLE "labeled choice" from "runnable recovery pick". An option does
    NOT need a tool_name to be a valid choice: the model often enumerates plain-language
    paths ("Use approach A") with no tool call. Those still become clickable cards —
    the pick is recorded as a user message that steers the agent — so we must NOT drop
    them. A `tool_name`, when present, additionally makes the pick directly runnable;
    its absence just means "label-only".
    """
    tool_name = _extract_option_tool_name(opt)
    args = _extract_option_arguments(opt)
    description = str(opt.get("description") or opt.get("why") or "").strip()
    # Broaden label extraction and NEVER fall back to a content-free
    # "Option N": derive a real label from any human-readable field, and
    # only as a last resort name the tool the pick will run. If none of
    # those exist the option carries no information to show — skip it
    # rather than render a meaningless button.
    title = str(
        opt.get("title") or opt.get("label") or opt.get("name") or description or tool_name
    ).strip()
    if not title:
        return None
    option_id = str(opt.get("id") or f"opt_{index + 1}")
    return AlternativeOption(
        id=option_id,
        title=title,
        description=description,
        tool_name=tool_name,
        arguments=args,
    )


def _coerce_alternative_options(raw_options: Any) -> list[AlternativeOption]:
    """Coerce the model's option shape drift into a list of AlternativeOption.

    Bare strings become label-only choices; dicts are coerced with decoupled
    label/tool semantics; content-free options are skipped (never placeholdered).
    """
    options: list[AlternativeOption] = []
    for i, opt in enumerate(raw_options):
        if isinstance(opt, str):
            coerced = _coerce_string_option(opt, i)
            if coerced is not None:
                options.append(coerced)
            continue
        if not isinstance(opt, dict):
            continue
        coerced = _coerce_dict_option(opt, i)
        if coerced is not None:
            options.append(coerced)
    return options
