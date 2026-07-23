"""Pure provenance for managed dynamic previews that cross ``FINISHED``.

``ActiveLivePreviewProjection`` remains the identity of the exact generation
that was health-proven before FINISHED.  ``SealedPreviewRuntimeContract`` is a
separate, stricter replay authority: it binds the raw accepted launch intent to
the final immutable workspace seal.  Restores revalidate that intent through
PreviewManager and never replay a resolved command, port, or mutable mirror.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from disco.core import ActionEvent, Event, ObservationEvent, StatusEvent
from disco.core.events import ConversationStatus
from disco.tools.builtin.preview import PreviewStartArgs
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
    source_tool_name: str = "preview_start"


@dataclass(frozen=True)
class SealedPreviewRuntimeContract:
    """Typed raw launch intent bound to one final immutable app workspace."""

    contract_id: str
    conversation_id: str
    version_seq: int
    tree_digest: str
    terminal_seq: int
    app_entry: str
    projection: ActiveLivePreviewProjection
    session_name: str
    serve_dir: str | None
    command: str | None
    framework: str | None
    cwd: str | None

    def start_kwargs(self) -> dict[str, Any]:
        return {
            "serve_dir": self.serve_dir,
            "command": self.command,
            "framework": self.framework,
            "cwd": self.cwd,
            "name": self.session_name,
            "supervise": True,
        }

    def intent(self) -> dict[str, Any]:
        return {
            "serve_dir": self.serve_dir,
            "command": self.command,
            "framework": self.framework,
            "cwd": self.cwd,
            "launch_kind": self.projection.launch_kind,
        }


def _runtime_payload_from_pair(
    action: ActionEvent,
    observation: ObservationEvent,
) -> tuple[str, dict[str, Any]] | None:
    result = observation.tool_result
    structured = result.structured
    if (
        result.success is not True
        or result.call_id != action.tool_call.call_id
        or observation.action_id != action.id
        or result.tool_name != action.tool_call.tool_name
        or not isinstance(structured, dict)
    ):
        return None
    if result.tool_name == "preview_start":
        return result.tool_name, structured
    if result.tool_name == "verify_appkit_app" and structured.get("passed") is True:
        runtime = structured.get("preview_runtime")
        if isinstance(runtime, dict):
            return result.tool_name, runtime
    return None


def _projection_from_pair(
    action: ActionEvent,
    observation: ObservationEvent,
) -> ActiveLivePreviewProjection | None:
    paired = _runtime_payload_from_pair(action, observation)
    if paired is None:
        return None
    source_tool_name, structured = paired
    if structured.get("status") not in {"running", "unavailable"}:
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
        source_tool_name=source_tool_name,
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
        if result.tool_name in {"preview_start", "verify_appkit_app"}:
            action = actions.get(event.action_id or "")
            if result.success is not True:
                continue
            outer = result.structured
            if result.tool_name == "verify_appkit_app":
                if not isinstance(outer, dict) or outer.get("passed") is not True:
                    continue
                structured = outer.get("preview_runtime")
            else:
                structured = outer
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


def derive_sealed_preview_runtime_contract(
    events: Sequence[Event],
    *,
    terminal_seq: int,
    conversation_id: str,
    version_seq: int,
    tree_digest: str,
    app_entry: str,
) -> SealedPreviewRuntimeContract | None:
    """Bind the selected dynamic intent to the exact final workspace seal.

    Model-authored ``preview_start`` arguments are validated with the public tool
    schema and must exactly match the host observation's normalized intent.  A
    strict AppKit verifier may supply the same host-owned runtime payload after
    verifying the canonical manager.  Only raw intent is retained; the resolved
    command, original port, and original cwd are provenance, never replay input.
    """

    if (
        not conversation_id
        or type(version_seq) is not int
        or version_seq < 1
        or len(tree_digest) != 64
        or any(char not in "0123456789abcdef" for char in tree_digest)
        or type(terminal_seq) is not int
        or terminal_seq < 1
        or not app_entry
    ):
        return None
    projection = derive_active_live_preview_projection(events, terminal_seq=terminal_seq)
    if projection is None:
        return None
    actions = {
        event.id: event
        for event in events
        if isinstance(event, ActionEvent) and event.id == projection.source_action_id
    }
    observations = {
        event.id: event
        for event in events
        if isinstance(event, ObservationEvent) and event.id == projection.source_observation_id
    }
    action = actions.get(projection.source_action_id)
    observation = observations.get(projection.source_observation_id)
    if action is None or observation is None:
        return None
    paired = _runtime_payload_from_pair(action, observation)
    if paired is None:
        return None
    source_tool_name, payload = paired
    intent = payload.get("intent")
    if not isinstance(intent, dict):
        return None
    values = {key: intent.get(key) for key in ("serve_dir", "command", "framework", "cwd")}
    if any(value is not None and not isinstance(value, str) for value in values.values()):
        return None
    if not (values["serve_dir"] or values["command"] or values["framework"]):
        return None
    expected_intent = {
        **values,
        "launch_kind": projection.launch_kind,
    }
    if intent != expected_intent:
        return None
    if source_tool_name == "preview_start":
        try:
            args = PreviewStartArgs.model_validate(action.tool_call.arguments)
        except ValueError:
            return None
        action_intent = {
            "serve_dir": args.serve_dir,
            "command": args.command,
            "framework": args.framework,
            "cwd": args.cwd,
        }
        if action_intent != values or (
            args.name is not None and args.name != projection.session_name
        ):
            return None
    elif source_tool_name != "verify_appkit_app":
        return None

    material = {
        "app_entry": app_entry,
        "conversation_id": conversation_id,
        "intent": expected_intent,
        "projection_id": projection.projection_id,
        "session_name": projection.session_name,
        "source_action_id": projection.source_action_id,
        "source_observation_id": projection.source_observation_id,
        "terminal_seq": terminal_seq,
        "tree_digest": tree_digest,
        "version_seq": version_seq,
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return SealedPreviewRuntimeContract(
        contract_id=f"sealed-preview:{digest}",
        conversation_id=conversation_id,
        version_seq=version_seq,
        tree_digest=tree_digest,
        terminal_seq=terminal_seq,
        app_entry=app_entry,
        projection=projection,
        session_name=projection.session_name,
        serve_dir=values["serve_dir"],
        command=values["command"],
        framework=values["framework"],
        cwd=values["cwd"],
    )


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
    "SealedPreviewRuntimeContract",
    "derive_active_live_preview_projection",
    "derive_sealed_preview_runtime_contract",
    "projection_identity",
]
