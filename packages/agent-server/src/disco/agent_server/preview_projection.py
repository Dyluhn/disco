"""Pure provenance for a managed dynamic preview that crosses ``FINISHED``.

This is deliberately an *active-live* projection, not a replay recipe.  It
authorizes only the exact PreviewManager generation that was already running
and health-proven before the terminal event.  It contains no executable command
and cannot wake, recreate, or install anything after that generation disappears.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from disco.core import ActionEvent, Event, ObservationEvent, StatusEvent
from disco.core.events import ConversationStatus
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

from .preview_manager import preview_projection_digest


@dataclass(frozen=True)
class ActiveLivePreviewProjection:
    """Identity of one health-proven, non-static PreviewManager generation."""

    projection_id: str
    session_name: str
    port: int
    launch_kind: str
    intent_digest: str
    sandbox_instance_id: str
    sandbox_generation: int
    source_action_id: str
    source_action_seq: int
    source_observation_id: str
    source_observation_seq: int


def _projection_from_pair(
    action: ActionEvent,
    observation: ObservationEvent,
) -> ActiveLivePreviewProjection | None:
    result = observation.tool_result
    structured = result.structured
    if (
        result.tool_name != "preview_start"
        or result.success is not True
        or result.call_id != action.tool_call.call_id
        or action.tool_call.tool_name != "preview_start"
        or observation.action_id != action.id
        or not isinstance(structured, dict)
        or structured.get("status") not in {"running", "unavailable"}
    ):
        return None

    projection_id = structured.get("projection_id")
    session_name = structured.get("name")
    port = structured.get("port")
    launch_kind = structured.get("launch_kind")
    intent_digest = structured.get("intent_digest")
    sandbox_instance_id = structured.get("sandbox_instance_id")
    sandbox_generation = structured.get("sandbox_generation")
    command = structured.get("command")
    exec_dir = structured.get("exec_dir")
    intent = structured.get("intent")
    if (
        not isinstance(projection_id, str)
        or not projection_id.startswith("pv_")
        or len(projection_id) != 35
        or any(char not in "0123456789abcdef" for char in projection_id[3:])
        or not isinstance(session_name, str)
        or not session_name
        or len(session_name) > 128
        or type(port) is not int
        or port not in USER_PORTS
        or port == NOVNC_PORT
        or launch_kind not in {"custom", "framework"}
        or not isinstance(intent_digest, str)
        or len(intent_digest) != 64
        or any(char not in "0123456789abcdef" for char in intent_digest)
        or not isinstance(sandbox_instance_id, str)
        or not sandbox_instance_id
        or sandbox_instance_id.startswith("session-")
        or type(sandbox_generation) is not int
        or sandbox_generation < 1
        or not isinstance(command, str)
        or not command
        or (exec_dir is not None and not isinstance(exec_dir, str))
        or not isinstance(intent, dict)
        or intent.get("launch_kind") != launch_kind
    ):
        return None
    expected_digest = preview_projection_digest(
        name=session_name,
        port=port,
        command=command,
        exec_dir=exec_dir,
        intent=intent,
    )
    if expected_digest is None or intent_digest != expected_digest:
        return None
    if (
        type(action.seq) is not int
        or type(observation.seq) is not int
        or action.seq >= observation.seq
    ):
        return None
    return ActiveLivePreviewProjection(
        projection_id=projection_id,
        session_name=session_name,
        port=port,
        launch_kind=launch_kind,
        intent_digest=intent_digest,
        sandbox_instance_id=sandbox_instance_id,
        sandbox_generation=sandbox_generation,
        source_action_id=action.id,
        source_action_seq=action.seq,
        source_observation_id=observation.id,
        source_observation_seq=observation.seq,
    )


def derive_active_live_preview_projection(
    events: Sequence[Event],
    *,
    terminal_seq: int,
) -> ActiveLivePreviewProjection | None:
    """Return the newest still-selected dynamic preview before ``terminal_seq``.

    Each revision starts after the preceding FINISHED status.  Successful starts
    move their name to the newest selection, and successful stops revoke names.
    Malformed stop evidence clears all eligibility rather than retaining a stale
    authorization.  A newest static selection intentionally returns ``None``:
    static finished previews use the immutable version projection instead.
    """

    ordered = sorted(
        (event for event in events if type(event.seq) is int and event.seq < terminal_seq),
        key=lambda event: event.seq or -1,
    )
    previous_terminal = max(
        (
            event.seq
            for event in ordered
            if isinstance(event, StatusEvent)
            and event.status is ConversationStatus.FINISHED
            and event.seq is not None
        ),
        default=0,
    )
    segment = [event for event in ordered if (event.seq or -1) > previous_terminal]
    actions: dict[str, ActionEvent] = {}
    active: dict[str, tuple[int, ActiveLivePreviewProjection | None]] = {}

    for event in segment:
        if isinstance(event, ActionEvent):
            actions[event.id] = event
            continue
        if not isinstance(event, ObservationEvent):
            continue
        result = event.tool_result
        if result.tool_name == "preview_start":
            action = actions.get(event.action_id or "")
            if result.success is not True:
                continue
            structured = result.structured
            if (
                action is None
                or not isinstance(structured, dict)
                or not isinstance(structured.get("name"), str)
                or not structured["name"]
                or structured.get("status") not in {"running", "unavailable"}
            ):
                # A successful start is a canonical selection change.  If its
                # identity cannot be reconstructed, retaining an older selection
                # would serve stale content under an unknown newer target.
                active.clear()
                continue
            projection = _projection_from_pair(action, event)
            # A healthy static selection also supersedes an older dynamic
            # selection. Store ``None`` for that selected static generation (and
            # for malformed dynamic identity, which likewise fails closed).
            active[structured["name"]] = (event.seq or -1, projection)
            continue
        if result.tool_name != "preview_stop" or result.success is not True:
            continue
        structured = result.structured
        stopped = structured.get("stopped") if isinstance(structured, dict) else None
        if not isinstance(stopped, list) or not all(
            isinstance(name, str) and name for name in stopped
        ):
            active.clear()
            continue
        for name in stopped:
            active.pop(name, None)

    if not active:
        return None
    _seq, selected = max(active.values(), key=lambda item: item[0])
    return selected


def projection_identity(projection: ActiveLivePreviewProjection) -> tuple[Any, ...]:
    """The fields a live PreviewManager must match exactly."""

    return (
        projection.projection_id,
        projection.session_name,
        projection.port,
        projection.launch_kind,
        projection.intent_digest,
        projection.sandbox_instance_id,
        projection.sandbox_generation,
    )


__all__ = [
    "ActiveLivePreviewProjection",
    "derive_active_live_preview_projection",
    "projection_identity",
]
