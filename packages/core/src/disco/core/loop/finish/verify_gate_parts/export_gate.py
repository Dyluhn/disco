"""[P10] Export render-correctness gate.

Owns: resolving which stamped export the current finish should be graded
against (the latest post-export deliverable's own file when it names one,
otherwise the latest stamped export) and refusing FINISHED when that export
is blank, truncated, or corrupt.
"""

from __future__ import annotations

from typing import Any

from ..common import (
    _LOG,
    EXPORT_GATE_MAX_REFUSALS,
    AgentStep,
    ConversationStatus,
    DeliverableEvent,
    Disp,
    Event,
    EventSource,
    ExportRenderFacts,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    _artifact_record_kind,
    _safe_deliverable_file_path,
    count_export_gate_refusals,
    export_gate_refusal_reminder,
    export_gate_release_warning,
    export_render_facts_for_path,
    latest_export_render_facts,
    latest_export_render_index,
)


async def manifest_export_artifact_path(
    gate: Any, events: list[Event], *, require_shown: bool
) -> str | None:
    records = await gate._artifact_manifest_records(events, consumer="export_render")
    if records is None:
        return None

    shown: list[str] = []
    unshown: list[str] = []
    seen: set[str] = set()
    for record in records:
        if _artifact_record_kind(record) == "app":
            continue
        raw_path = getattr(record, "path", None)
        if not isinstance(raw_path, str):
            continue
        path = _safe_deliverable_file_path(raw_path)
        if path is None or path in seen:
            continue
        if export_render_facts_for_path(events, path) is None:
            continue
        seen.add(path)
        if bool(getattr(record, "shown", False)):
            shown.append(path)
        else:
            unshown.append(path)

    if shown:
        return shown[-1]
    if require_shown:
        return None
    if len(unshown) == 1:
        return unshown[0]
    if len(unshown) > 1:
        _LOG.info(
            "artifact-manifest reader export path ambiguous for %s: %s",
            gate._loop.conversation_id,
            unshown,
        )
    return None


def _latest_post_export_deliverable(
    events: list[Event], export_idx: int
) -> DeliverableEvent | None:
    for i in range(len(events) - 1, export_idx, -1):
        ev = events[i]
        if isinstance(ev, DeliverableEvent):
            return ev
    return None


async def _export_render_facts(
    gate: Any,
    events: list[Event],
    latest_post_export_deliverable: DeliverableEvent | None,
) -> ExportRenderFacts | None:
    manifest_path = await manifest_export_artifact_path(
        gate,
        events,
        require_shown=(
            latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "files"
        ),
    )
    if manifest_path is not None:
        facts = export_render_facts_for_path(events, manifest_path)
        if facts is not None:
            return facts
    if (
        latest_post_export_deliverable is not None
        and latest_post_export_deliverable.artifact_kind == "files"
    ):
        facts = export_render_facts_for_path(events, latest_post_export_deliverable.path)
        if facts is not None:
            return facts
    return latest_export_render_facts(events)


async def gate_export_render(gate: Any, step: AgentStep, events: list[Event]) -> Disp:
    """[P10] Refuse FINISHED when a rendered export (deck/document) is BLANK,
    TRUNCATED, or CORRUPT — the "looks done but the file is empty" false
    completeness. The real executor for the inert ``ExportContract.validate``
    stage.

    Reads the render facts the producer stamped from the ACTUAL output bytes
    (``latest_export_render_facts``), NOT the model's declared slide_count. A
    broken export re-enters the loop with a concrete steer; a good one (or none
    produced) falls through. Bounded by ``EXPORT_GATE_MAX_REFUSALS`` so a
    genuinely-broken renderer can't trap the run — it releases with a loud
    UNVERIFIED warning, exactly like the browser-verify valve. Decision/message
    logic is pure (``contract.export_render``); this function only emits.
    """
    # The latest deliverable AFTER the export decides which facts govern this
    # finish (only the latest counts: a newer files handoff means the broken deck
    # is current and must be gated).
    export_idx = latest_export_render_index(events)
    latest_post_export_deliverable = _latest_post_export_deliverable(events, export_idx)
    # An app deliverable emitted AFTER the broken export means the app is the
    # current handoff and the deck is superseded — fall through (the app gates
    # own that path).
    if (
        latest_post_export_deliverable is not None
        and latest_post_export_deliverable.artifact_kind == "app"
    ):
        return Disp.FALLTHROUGH

    # A FILES handoff naming a specific stamped file is gated on THAT file's facts,
    # so delivering a known-bad export can't clear on a newer sibling's good facts.
    # Otherwise the latest stamped export governs.
    facts = await _export_render_facts(gate, events, latest_post_export_deliverable)
    if facts is None or facts.ok:
        return Disp.FALLTHROUGH  # no export stamped, or it renders fine

    if count_export_gate_refusals(events, since=export_idx) >= EXPORT_GATE_MAX_REFUSALS:
        await gate._loop._emit(
            StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_export"),
        )
        await gate._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=export_gate_release_warning(facts)),
            )
        )
        return Disp.FALLTHROUGH

    await gate._record_verifier_failure_to_context(message=facts.detail, rel_path=None)
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=export_gate_refusal_reminder(facts)),
        )
    )
    return Disp.CONTINUE
