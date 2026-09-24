"""Conservative freeform handoff continuity, never verification authority."""

from __future__ import annotations

import posixpath

from ....events import (
    DeliverableEvent,
    Event,
    EventSource,
    ObservationEvent,
    latest_workspace_run_intent,
)
from ....verification import PreviewSelectionIdentity
from .host_claims import preview_selection_at


def _launch_at(events: list[Event], selection: PreviewSelectionIdentity) -> tuple[str, str] | None:
    """Read effective launch data from the already validated host observation."""
    for event in events:
        if (
            not isinstance(event, ObservationEvent)
            or event.id != selection.source_observation_id
            or event.source is not EventSource.ENVIRONMENT
            or event.tool_result.tool_name != "preview_start"
        ):
            continue
        structured = event.tool_result.structured
        if not isinstance(structured, dict) or structured.get("status") != "running":
            return None
        command, cwd = structured.get("command"), structured.get("exec_dir")
        if not isinstance(command, str) or not command.strip():
            return None
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        # Compare host effective values, not model input spelling or URLs. Do not
        # interpret shell commands or equate relative/absolute paths by guessing.
        return command, posixpath.normpath(cwd)
    return None


def _freeform_preview_successor(events: list[Event], handoff: DeliverableEvent) -> bool:
    """Whether a current completion can rebind the same freeform app launch.

    Port/session reuse alone is insufficient. Require the same owned sandbox,
    effective command/root, and selected artifact scope; only the projection and
    framework/custom spelling may rotate. Missing evidence stays fail-closed.
    Fresh live identity and browser verification are still mandatory afterward.
    """
    if type(handoff.seq) is not int:
        return False
    intent = latest_workspace_run_intent(events)
    if intent is not None and (intent.seq or 0) > handoff.seq:
        return False
    before = preview_selection_at(events, through_seq=handoff.seq)
    after = preview_selection_at(events, through_seq=max((e.seq or 0 for e in events), default=0))
    if before is None or after is None:
        return False
    same_owner = (
        before.session_name == after.session_name
        and before.port == after.port
        and before.sandbox_instance_id == after.sandbox_instance_id
        and before.sandbox_generation == after.sandbox_generation
    )
    same_scope = (
        before.static_serve_dir == after.static_serve_dir
        and before.contains_artifact(handoff.path)
        and after.contains_artifact(handoff.path)
    )
    launch = _launch_at(events, before)
    return bool(
        same_owner and same_scope and launch is not None and launch == _launch_at(events, after)
    )
