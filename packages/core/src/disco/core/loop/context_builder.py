"""CXT-4 — the ContextPack assembler + renderer (compatibility facade).

This module remains the historical import path for the ContextPack assembler
(``build_context_pack``) and the deterministic renderers
(``render_context_pack``, ``render_plan_as_todo_markdown``).  The rendering
implementation now lives in :mod:`context_rendering`, the single bounded owner;
this facade re-exports every public/private name, signature, and rendered byte
so consumers are unchanged.

PURE functions: no loop/runtime/IO. Live prompt wiring (and unifying with
the existing C6 ``<current-objective>`` recitation) is CXT-7.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..context import (
    CompactionPolicy,
    ContextLedger,
    ContextPack,
    VerifierFailureRef,
)
from ..context import (
    SourceKind as SourceKind,
)
from ..context import (
    SourcePriority as SourcePriority,
)
from ..context.compaction import resolved_ranges_from_events
from ..events import Event, EventSource, MessageEvent, PlanEvent
from . import check_ledger
from .context_rendering import (
    _BLOCK_CLOSE as _BLOCK_CLOSE,
)
from .context_rendering import (
    _BLOCK_OPEN as _BLOCK_OPEN,
)
from .context_rendering import (
    _DISCO_PATH_TOKENS as _DISCO_PATH_TOKENS,
)
from .context_rendering import (
    _failure_line as _failure_line,
)
from .context_rendering import (
    _sanitize_todo_seed_text as _sanitize_todo_seed_text,
)
from .context_rendering import (
    render_context_pack as render_context_pack,
)
from .context_rendering import (
    render_plan_as_todo_markdown as render_plan_as_todo_markdown,
)


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
    design_direction: str | None = None,
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
    retained_refs = []
    seen_ref_paths: set[str] = set()
    for ref in (*led.retained_refs, *extra_refs):
        if ref.rel_path in seen_ref_paths:
            continue
        seen_ref_paths.add(ref.rel_path)
        retained_refs.append(ref)

    merged = led.model_copy(
        update={
            "active_goal": goal,
            "current_version": version,
            "resolved_ranges": resolved,
            "retained_refs": tuple(retained_refs),
            "latest_verifier_failures": (
                failures if failures is not None else led.latest_verifier_failures
            ),
        }
    )
    return ContextPack.from_ledger(
        merged,
        policy=policy,
        todo_text=todo_text,
        design_direction=design_direction,
        checks_text=check_ledger.render_checks_block(list(events)),
        allowed_next_actions=allowed_next_actions,
    )
