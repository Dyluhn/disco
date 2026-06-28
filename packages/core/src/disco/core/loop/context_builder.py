"""CXT-4 — the ContextPack assembler + renderer.

Builds a compact, structured ContextPack from the event log (overlaying the
durable ledger from CXT-2) and renders it as a deterministic, byte-stable
`<context-pack>` block — the model-facing context that replaces feeding raw event
history. PURE functions: no loop/runtime/IO. Live prompt wiring (and unifying with
the existing C6 `<current-objective>` recitation) is CXT-7.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..context import (
    CompactionPolicy,
    ContextLedger,
    ContextPack,
    SourceKind,
    SourcePriority,
    VerifierFailureRef,
)
from ..context.compaction import resolved_ranges_from_events
from ..events import Event, EventSource, MessageEvent, PlanEvent

_BLOCK_OPEN = "<context-pack>"
_BLOCK_CLOSE = "</context-pack>"


def _latest_plan(events: Sequence[Event]) -> PlanEvent | None:
    latest: PlanEvent | None = None
    for e in events:
        if isinstance(e, PlanEvent):
            latest = e
    return latest


def _head_user_text(events: Sequence[Event]) -> str | None:
    for e in events:
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            content = e.message.content
            return content if isinstance(content, str) and content.strip() else None
    return None


def build_context_pack(
    events: Sequence[Event],
    *,
    base_ledger: ContextLedger | None = None,
    policy: CompactionPolicy | None = None,
    todo_text: str | None = None,
    failures: tuple[VerifierFailureRef, ...] | None = None,
    allowed_next_actions: tuple[str, ...] = (),
) -> ContextPack:
    """Assemble a ContextPack from the event log, overlaid on the durable ledger.

    Goal/version come from the latest PlanEvent (fallback: head USER message);
    resolved-range summary refs are folded into recoverable refs; `failures` is
    caller-supplied (from the CXT-2 store/ledger) rather than mined from events.
    Pass `failures=()` to explicitly clear; `None` (default) keeps the ledger's.
    """
    led = base_ledger if base_ledger is not None else ContextLedger.empty("")

    plan = _latest_plan(events)
    goal = plan.summary if plan is not None else _head_user_text(events)
    if goal is None:
        goal = led.active_goal
    version = plan.revision if plan is not None else led.current_version

    resolved = resolved_ranges_from_events(events)
    extra_refs = tuple(r.summary_ref for r in resolved if r.summary_ref is not None)

    merged = led.model_copy(
        update={
            "active_goal": goal,
            "current_version": version,
            "resolved_ranges": resolved,
            "retained_refs": (*led.retained_refs, *extra_refs),
            "latest_verifier_failures": (
                failures if failures is not None else led.latest_verifier_failures
            ),
        }
    )
    return ContextPack.from_ledger(
        merged, policy=policy, todo_text=todo_text, allowed_next_actions=allowed_next_actions
    )


def _failure_line(f: VerifierFailureRef) -> str:
    loc = f" ({f.rel_path})" if f.rel_path else ""
    return f"- [{f.severity.value}] {f.kind}: {f.message}{loc}"


def render_context_pack(pack: ContextPack, *, priority: SourcePriority | None = None) -> str:
    """Render the pack as a deterministic, byte-stable `<context-pack>` block.

    Sections are emitted in SourcePriority order; empty sections are omitted. No
    event history / raw tool chatter — only the structured anchors. Stable across
    turns for identical input (KV-cache-friendly prefix)."""
    order = (priority or SourcePriority.default()).order
    sections: dict[SourceKind, list[str]] = {}

    if pack.active_goal:
        head = f"Goal (v{pack.current_version}): {pack.active_goal}"
        sections[SourceKind.GOAL] = [head]
    if pack.active_contract:
        sections[SourceKind.CONTRACT] = [f"Contract: {pack.active_contract}"]
    if pack.current_todo:
        sections[SourceKind.TODO] = ["Todo:", pack.current_todo]
    if pack.latest_failures:
        sections[SourceKind.VERIFIER_FAILURE] = [
            "Unresolved verifier failures:",
            *[_failure_line(f) for f in pack.latest_failures],
        ]
    if pack.direct_edits_summary:
        sections[SourceKind.DIRECT_EDIT] = [
            "User direct edits:",
            *[f"- {e.rel_path}: {e.kind.value}{(' — ' + e.summary) if e.summary else ''}" for e in pack.direct_edits_summary],
        ]
    if pack.unresolved_comments:
        sections[SourceKind.COMMENT] = [
            "Unresolved comments:",
            *[f"- {c}" for c in pack.unresolved_comments],
        ]
    if pack.resource_refs:
        sections[SourceKind.RESOURCE] = [
            "Resources:",
            *[f"- {r.rel_path}" for r in pack.resource_refs],
        ]
    if pack.recoverable_refs:
        sections[SourceKind.RECOVERABLE_REF] = [
            "Recoverable references (read on demand):",
            *[f"- {r.kind.value}: {r.rel_path}" for r in pack.recoverable_refs],
        ]

    lines: list[str] = [_BLOCK_OPEN]
    for kind in order:
        block = sections.get(kind)
        if block:
            lines.extend(block)
    if pack.allowed_next_actions:
        lines.append(f"Allowed next actions: {', '.join(pack.allowed_next_actions)}")
    lines.append(_BLOCK_CLOSE)
    return "\n".join(lines)
