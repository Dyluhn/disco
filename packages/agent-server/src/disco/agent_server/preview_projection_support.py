"""Pure parsing and provenance mechanics for managed preview projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from disco.core import ActionEvent, Event, ObservationEvent, StatusEvent
from disco.core.events import ConversationStatus
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.builtin.preview import PreviewStartArgs
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

from .preview_models import preview_projection_digest


@dataclass(frozen=True)
class ActiveLivePreviewProjection:
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
    if not (
        result.success is True
        and result.call_id == action.tool_call.call_id
        and observation.action_id == action.id
        and result.tool_name == action.tool_call.tool_name
    ):
        return None
    if not isinstance(structured, dict):
        return None
    if result.tool_name == "preview_start":
        return result.tool_name, structured
    runtime = structured.get("preview_runtime")
    if result.tool_name == "verify_appkit_app" and structured.get("passed") is True:
        return (result.tool_name, runtime) if isinstance(runtime, dict) else None
    return None


def _valid_projection_identity(payload: dict[str, Any]) -> bool:
    projection_id = payload.get("projection_id")
    intent_digest = payload.get("intent_digest")
    return (
        isinstance(projection_id, str)
        and projection_id.startswith("pv_")
        and len(projection_id) == 35
        and not (set(projection_id[3:]) - set("0123456789abcdef"))
        and isinstance(intent_digest, str)
        and len(intent_digest) == 64
        and not (set(intent_digest) - set("0123456789abcdef"))
    )


def _valid_projection_runtime(payload: dict[str, Any]) -> bool:
    port = payload.get("port")
    generation = payload.get("sandbox_generation")
    return (
        isinstance(payload.get("name"), str)
        and bool(payload["name"])
        and len(payload["name"]) <= 128
        and type(port) is int
        and (port in USER_PORTS or is_managed_host_preview_port(port))
        and port != NOVNC_PORT
        and payload.get("launch_kind") in {"custom", "framework"}
        and isinstance(payload.get("sandbox_instance_id"), str)
        and bool(payload["sandbox_instance_id"])
        and not payload["sandbox_instance_id"].startswith("session-")
        and type(generation) is int
        and generation >= 1
    )


def _valid_projection_launch(payload: dict[str, Any]) -> bool:
    exec_dir = payload.get("exec_dir")
    intent = payload.get("intent")
    return (
        isinstance(payload.get("command"), str)
        and bool(payload["command"])
        and (exec_dir is None or isinstance(exec_dir, str))
        and isinstance(intent, dict)
        and intent.get("launch_kind") == payload.get("launch_kind")
    )


def _valid_projection_digest(payload: dict[str, Any]) -> bool:
    expected = preview_projection_digest(
        name=payload["name"],
        port=payload["port"],
        command=payload["command"],
        exec_dir=payload.get("exec_dir"),
        intent=payload["intent"],
    )
    return expected is not None and payload["intent_digest"] == expected


def _projection_from_pair(
    action: ActionEvent,
    observation: ObservationEvent,
) -> ActiveLivePreviewProjection | None:
    paired = _runtime_payload_from_pair(action, observation)
    if paired is None:
        return None
    source_tool_name, payload = paired
    if payload.get("status") not in {"running", "unavailable"}:
        return None
    validators = (
        _valid_projection_identity,
        _valid_projection_runtime,
        _valid_projection_launch,
        _valid_projection_digest,
    )
    if not all(validate(payload) for validate in validators):
        return None
    if (
        type(action.seq) is not int
        or type(observation.seq) is not int
        or action.seq >= observation.seq
    ):
        return None
    return ActiveLivePreviewProjection(
        projection_id=payload["projection_id"],
        session_name=payload["name"],
        port=payload["port"],
        launch_kind=payload["launch_kind"],
        intent_digest=payload["intent_digest"],
        sandbox_instance_id=payload["sandbox_instance_id"],
        sandbox_generation=payload["sandbox_generation"],
        source_action_id=action.id,
        source_action_seq=action.seq,
        source_observation_id=observation.id,
        source_observation_seq=observation.seq,
        source_tool_name=source_tool_name,
    )


def _current_revision_segment(
    events: Sequence[Event],
    terminal_seq: int,
) -> list[Event]:
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
    return [event for event in ordered if (event.seq or -1) > previous_terminal]


def _start_projection(
    event: ObservationEvent,
    actions: dict[str, ActionEvent],
) -> tuple[str, ActiveLivePreviewProjection | None] | None:
    result = event.tool_result
    action = actions.get(event.action_id or "")
    if result.success is not True:
        return None
    if action is None:
        return ("", None)
    outer = result.structured
    if result.tool_name == "verify_appkit_app":
        if not isinstance(outer, dict) or outer.get("passed") is not True:
            return None
        payload = outer.get("preview_runtime")
    else:
        payload = outer
    if not isinstance(payload, dict):
        return ("", None)
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return ("", None)
    if payload.get("status") not in {"running", "unavailable"}:
        return ("", None)
    return name, _projection_from_pair(action, event)


def _stopped_names(event: ObservationEvent) -> list[str] | None:
    result = event.tool_result
    if result.tool_name != "preview_stop" or result.success is not True:
        return []
    payload = result.structured
    stopped = payload.get("stopped") if isinstance(payload, dict) else None
    if not isinstance(stopped, list):
        return None
    return stopped if all(isinstance(name, str) and name for name in stopped) else None


def derive_active_live_preview_projection(
    events: Sequence[Event],
    *,
    terminal_seq: int,
) -> ActiveLivePreviewProjection | None:
    actions: dict[str, ActionEvent] = {}
    active: dict[str, tuple[int, ActiveLivePreviewProjection | None]] = {}
    for event in _current_revision_segment(events, terminal_seq):
        if isinstance(event, ActionEvent):
            actions[event.id] = event
            continue
        if not isinstance(event, ObservationEvent):
            continue
        if event.tool_result.tool_name in {"preview_start", "verify_appkit_app"}:
            selected = _start_projection(event, actions)
            if selected is None:
                continue
            name, projection = selected
            if not name:
                active.clear()
                continue
            active[name] = (event.seq or -1, projection)
            continue
        stopped = _stopped_names(event)
        if stopped is None:
            active.clear()
        else:
            for name in stopped:
                active.pop(name, None)
    if not active:
        return None
    return max(active.values(), key=lambda item: item[0])[1]


def _valid_contract_boundary(
    *,
    conversation_id: str,
    version_seq: int,
    tree_digest: str,
    terminal_seq: int,
    app_entry: str,
) -> bool:
    return (
        bool(conversation_id)
        and type(version_seq) is int
        and version_seq >= 1
        and len(tree_digest) == 64
        and not (set(tree_digest) - set("0123456789abcdef"))
        and type(terminal_seq) is int
        and terminal_seq >= 1
        and bool(app_entry)
    )


def _projection_evidence(
    events: Sequence[Event],
    projection: ActiveLivePreviewProjection,
) -> tuple[ActionEvent, ObservationEvent, str, dict[str, Any]] | None:
    action = next(
        (
            event
            for event in events
            if isinstance(event, ActionEvent) and event.id == projection.source_action_id
        ),
        None,
    )
    observation = next(
        (
            event
            for event in events
            if isinstance(event, ObservationEvent) and event.id == projection.source_observation_id
        ),
        None,
    )
    if action is None or observation is None:
        return None
    paired = _runtime_payload_from_pair(action, observation)
    if paired is None:
        return None
    return action, observation, paired[0], paired[1]


def _validated_raw_intent(
    action: ActionEvent,
    source_tool_name: str,
    payload: dict[str, Any],
    projection: ActiveLivePreviewProjection,
) -> dict[str, str | None] | None:
    intent = payload.get("intent")
    if not isinstance(intent, dict):
        return None
    values = {key: intent.get(key) for key in ("serve_dir", "command", "framework", "cwd")}
    if any(value is not None and not isinstance(value, str) for value in values.values()):
        return None
    if not (values["serve_dir"] or values["command"] or values["framework"]):
        return None
    expected = {**values, "launch_kind": projection.launch_kind}
    if intent != expected:
        return None
    if source_tool_name == "verify_appkit_app":
        return values
    if source_tool_name != "preview_start":
        return None
    try:
        args = PreviewStartArgs.model_validate(action.tool_call.arguments)
    except ValueError:
        return None
    return values if _action_intent_matches(args, values, projection.session_name) else None


def _action_intent_matches(
    args: PreviewStartArgs,
    values: dict[str, str | None],
    session_name: str,
) -> bool:
    action_intent = {
        "serve_dir": args.serve_dir,
        "command": args.command,
        "framework": args.framework,
        "cwd": args.cwd,
    }
    if action_intent != values:
        return False
    return args.name is None or args.name == session_name


def derive_sealed_preview_runtime_contract(
    events: Sequence[Event],
    *,
    terminal_seq: int,
    conversation_id: str,
    version_seq: int,
    tree_digest: str,
    app_entry: str,
) -> SealedPreviewRuntimeContract | None:
    if not _valid_contract_boundary(
        conversation_id=conversation_id,
        version_seq=version_seq,
        tree_digest=tree_digest,
        terminal_seq=terminal_seq,
        app_entry=app_entry,
    ):
        return None
    projection = derive_active_live_preview_projection(events, terminal_seq=terminal_seq)
    if projection is None:
        return None
    evidence = _projection_evidence(events, projection)
    if evidence is None:
        return None
    action, _observation, source_tool_name, payload = evidence
    values = _validated_raw_intent(action, source_tool_name, payload, projection)
    if values is None:
        return None
    intent = {**values, "launch_kind": projection.launch_kind}
    material = {
        "app_entry": app_entry,
        "conversation_id": conversation_id,
        "intent": intent,
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
    "preview_projection_digest",
    "projection_identity",
]
