"""Rendering owners for the ContextPack and todo.md surfaces.

This module is the single bounded implementation owner for the deterministic,
byte-stable renderers that turn a ``ContextPack`` into a ``<context-pack>``
block and an approved ``PlanEvent`` into a ``todo.md`` checklist.  The former
``context_builder`` module re-exports these symbols so every historical import
path, signature, prompt byte, and ordering rule is preserved.

The renderers are PURE functions over their inputs: no loop, runtime, or IO.
Section ordering follows ``SourcePriority``; empty sections are omitted; the
output is stable across turns for identical input (KV-cache-friendly prefix).
"""

from __future__ import annotations

from collections.abc import Mapping

from ..context import ContextPack, SourceKind, SourcePriority, VerifierFailureRef
from ..events import PlanEvent

_BLOCK_OPEN = "<context-pack>"
_BLOCK_CLOSE = "</context-pack>"
_DISCO_PATH_TOKENS = (".disco/", ".disco")
# The redirect this module authors when a plan's own prose tells the agent to edit
# harness bookkeeping directly, extracted from the bare `return` inside
# `_sanitize_todo_seed_text` so its test can DERIVE it rather than retype it
# (F62 / F58). Value verbatim.
_TODO_PROGRESS_REDIRECT = "Mark progress via update_plan_progress"


def render_plan_as_todo_markdown(
    plan: PlanEvent,
    states: Mapping[int, str] | None = None,
    *,
    include_heading: bool = True,
) -> str:
    """Render a plan checklist, optionally projected from live event progress.

    With no states this produces the unchanged all-pending todo.md seed. The
    live context pack passes the canonical progress fold so that seed and
    execution history cannot present two competing answers.
    """
    lines: list[str] = []
    if include_heading:
        lines.append(f"# {_sanitize_todo_seed_text(plan.summary)}")
        context = (plan.context or "").strip()
        if context:
            lines += ["", _sanitize_todo_seed_text(context)]
        lines.append("")
    lines.append("## Steps")
    for i, step in enumerate(plan.steps, start=1):
        state = states.get(i) if states is not None else None
        mark = "x" if state == "done" else " "
        active = " — ACTIVE" if state == "active" else ""
        lines.append(f"- [{mark}] {i}. {_sanitize_todo_seed_text(step.title)}{active}")
        detail = (getattr(step, "detail", "") or "").strip()
        if detail:
            lines.append(f"  {_sanitize_todo_seed_text(detail)}")
    return "\n".join(lines) + "\n"


def _sanitize_todo_seed_text(text: object) -> str:
    raw = "" if text is None else str(text)
    if not any(token in raw for token in _DISCO_PATH_TOKENS):
        return raw
    lowered = raw.lower()
    if "todo" in lowered and any(verb in lowered for verb in ("mark", "update", "edit", "check")):
        return _TODO_PROGRESS_REDIRECT
    words: list[str] = []
    for word in raw.split():
        if ".disco" in word:
            words.append("harness-managed bookkeeping")
        else:
            words.append(word)
    return " ".join(words)


def _failure_line(f: VerifierFailureRef) -> str:
    loc = f" ({f.rel_path})" if f.rel_path else ""
    return f"- [{f.severity.value}] {f.kind}: {f.message}{loc}"


def _goal_section(pack: ContextPack) -> list[str] | None:
    if pack.active_goal:
        return [f"Goal (v{pack.current_version}): {pack.active_goal}"]
    return None


def _contract_section(pack: ContextPack) -> list[str] | None:
    if pack.active_contract:
        return [f"Contract: {pack.active_contract}"]
    return None


def _design_direction_section(pack: ContextPack) -> list[str] | None:
    if pack.design_direction:
        return pack.design_direction.splitlines()
    return None


def _todo_section(pack: ContextPack) -> list[str] | None:
    if pack.current_todo:
        return ["Todo:", pack.current_todo]
    return None


def _checks_section(pack: ContextPack) -> list[str] | None:
    if pack.current_checks:
        return pack.current_checks.splitlines()
    return None


def _verifier_failure_section(pack: ContextPack) -> list[str] | None:
    if pack.latest_failures:
        return ["Unresolved verifier failures:", *[_failure_line(f) for f in pack.latest_failures]]
    return None


def _direct_edit_section(pack: ContextPack) -> list[str] | None:
    if pack.direct_edits_summary:
        return [
            "User direct edits:",
            *[
                f"- {e.rel_path}: {e.kind.value}{(' — ' + e.summary) if e.summary else ''}"
                for e in pack.direct_edits_summary
            ],
        ]
    return None


def _comment_section(pack: ContextPack) -> list[str] | None:
    if pack.unresolved_comments:
        return ["Unresolved comments:", *[f"- {c}" for c in pack.unresolved_comments]]
    return None


def _resource_section(pack: ContextPack) -> list[str] | None:
    if pack.resource_refs:
        return ["Resources:", *[f"- {r.rel_path}" for r in pack.resource_refs]]
    return None


def _recoverable_ref_section(pack: ContextPack) -> list[str] | None:
    if pack.recoverable_refs:
        return [
            "Recoverable references (read on demand):",
            *[f"- {r.kind.value}: {r.rel_path}" for r in pack.recoverable_refs],
        ]
    return None


def _build_context_pack_sections(pack: ContextPack) -> dict[SourceKind, list[str]]:
    """Build the ordered, non-empty section blocks for a ContextPack.

    Each section is emitted only when its payload is present, preserving the
    exact omission rule and rendered bytes of the former monolithic renderer.
    """
    sections: dict[SourceKind, list[str]] = {}
    for kind, builder in (
        (SourceKind.GOAL, _goal_section),
        (SourceKind.CONTRACT, _contract_section),
        (SourceKind.DESIGN_DIRECTION, _design_direction_section),
        (SourceKind.TODO, _todo_section),
        (SourceKind.CHECKS, _checks_section),
        (SourceKind.VERIFIER_FAILURE, _verifier_failure_section),
        (SourceKind.DIRECT_EDIT, _direct_edit_section),
        (SourceKind.COMMENT, _comment_section),
        (SourceKind.RESOURCE, _resource_section),
        (SourceKind.RECOVERABLE_REF, _recoverable_ref_section),
    ):
        block = builder(pack)
        if block:
            sections[kind] = block
    return sections


def render_context_pack(pack: ContextPack, *, priority: SourcePriority | None = None) -> str:
    """Render the pack as a deterministic, byte-stable `<context-pack>` block.

    Sections are emitted in SourcePriority order; empty sections are omitted. No
    event history / raw tool chatter — only the structured anchors. Stable across
    turns for identical input (KV-cache-friendly prefix)."""
    order = (priority or SourcePriority.default()).order
    sections = _build_context_pack_sections(pack)
    lines: list[str] = [_BLOCK_OPEN]
    for kind in order:
        block = sections.get(kind)
        if block:
            lines.extend(block)
    if pack.allowed_next_actions:
        lines.append(f"Allowed next actions: {', '.join(pack.allowed_next_actions)}")
    lines.append(_BLOCK_CLOSE)
    return "\n".join(lines)
