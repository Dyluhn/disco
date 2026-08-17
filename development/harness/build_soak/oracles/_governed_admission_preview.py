"""Preview identity and current-selection proof for governed admission."""

from __future__ import annotations

import posixpath
from typing import Any
from urllib.parse import quote

from ._governed_admission_helpers import _canonical_digest, _seq


def _validate_preview_structured(
    structured: Any,
    tool_name: str,
) -> dict[str, Any] | None:
    if tool_name == "verify_appkit_app":
        if not isinstance(structured, dict) or structured.get("passed") is not True:
            return None
        structured = structured.get("preview_runtime")
    if not isinstance(structured, dict) or structured.get("status") not in {
        "running",
        "unavailable",
    }:
        return None
    return structured


def _validate_preview_fields(
    structured: dict[str, Any],
) -> dict[str, Any] | None:
    intent = structured.get("intent")
    command = structured.get("command")
    exec_dir = structured.get("exec_dir")
    name = structured.get("name")
    port = structured.get("port")
    launch_kind = structured.get("launch_kind")
    if (
        not isinstance(intent, dict)
        or not isinstance(command, str)
        or not command
        or exec_dir is not None
        and not isinstance(exec_dir, str)
        or not isinstance(name, str)
        or not name
        or type(port) is not int
        or launch_kind not in {"static", "custom", "framework"}
        or intent.get("launch_kind") != launch_kind
    ):
        return None
    digest = _canonical_digest(
        {
            "command": command,
            "exec_dir": exec_dir,
            "intent": intent,
            "name": name,
            "port": port,
        }
    )
    if structured.get("intent_digest") != digest:
        return None
    return {
        "intent": intent,
        "command": command,
        "exec_dir": exec_dir,
        "name": name,
        "port": port,
        "launch_kind": launch_kind,
        "digest": digest,
    }


def _resolve_static_serve_dir(intent: dict[str, Any]) -> str | None:
    normalized = posixpath.normpath(str(intent.get("serve_dir") or "."))
    if normalized == "/workspace":
        return "."
    if normalized.startswith("/workspace/"):
        return normalized.removeprefix("/workspace/")
    if not normalized.startswith("/") and normalized != ".." and not normalized.startswith("../"):
        return normalized
    return None


def _paired_preview_payload(
    action: dict[str, Any],
    observation: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    call = action.get("tool_call")
    result = observation.get("tool_result")
    if not isinstance(call, dict) or not isinstance(result, dict):
        return None
    tool_name = call.get("tool_name")
    if tool_name not in {"preview_start", "verify_appkit_app"}:
        return None
    expected = {
        "tool_name": tool_name,
        "call_id": call.get("call_id"),
        "success": True,
    }
    if any(result.get(field) != value for field, value in expected.items()):
        return None
    if observation.get("action_id") != action.get("id"):
        return None
    return tool_name, result


def preview_identity_from_pair(
    action: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract a preview identity from a paired action/observation."""
    paired = _paired_preview_payload(action, observation)
    if paired is None:
        return None
    tool_name, result = paired
    structured = _validate_preview_structured(result.get("structured"), tool_name)
    if structured is None:
        return None
    fields = _validate_preview_fields(structured)
    if fields is None:
        return None
    action_seq = _seq(action)
    observation_seq = _seq(observation)
    if action_seq < 1 or observation_seq <= action_seq:
        return None
    static_serve_dir: str | None = None
    if fields["launch_kind"] == "static":
        static_serve_dir = _resolve_static_serve_dir(fields["intent"])
        if static_serve_dir is None:
            return None
    return {
        "projection_id": structured.get("projection_id"),
        "session_name": fields["name"],
        "port": fields["port"],
        "url": str(structured.get("url") or ""),
        "launch_kind": fields["launch_kind"],
        "intent_digest": fields["digest"],
        "sandbox_instance_id": structured.get("sandbox_instance_id"),
        "sandbox_generation": structured.get("sandbox_generation"),
        "static_serve_dir": static_serve_dir,
        "source_action_id": action.get("id"),
        "source_action_seq": action_seq,
        "source_observation_id": observation.get("id"),
        "source_observation_seq": observation_seq,
    }


def _stopped_preview_names(
    action: dict[str, Any] | None,
    result: dict[str, Any],
) -> list[str] | None:
    if not isinstance(action, dict):
        return None
    call = action.get("tool_call")
    structured = result.get("structured")
    if not isinstance(call, dict) or not isinstance(structured, dict):
        return None
    stopped = structured.get("stopped")
    if call.get("tool_name") != "preview_stop":
        return None
    if result.get("call_id") != call.get("call_id"):
        return None
    if not isinstance(stopped, list) or not stopped:
        return None
    return stopped if all(isinstance(name, str) and name for name in stopped) else None


def _apply_preview_observation(
    event: dict[str, Any],
    actions: dict[str, dict[str, Any]],
    active: dict[str, tuple[int, dict[str, Any]]],
) -> None:
    result = event.get("tool_result")
    if not isinstance(result, dict) or result.get("success") is not True:
        return
    tool_name = result.get("tool_name")
    action = actions.get(str(event.get("action_id") or ""))
    if tool_name in {"preview_start", "verify_appkit_app"}:
        identity = preview_identity_from_pair(action, event) if isinstance(action, dict) else None
        if identity is None or not isinstance(identity.get("session_name"), str):
            active.clear()
            return
        active[identity["session_name"]] = (_seq(event), identity)
        return
    if tool_name != "preview_stop":
        return
    stopped = _stopped_preview_names(action, result)
    if stopped is None:
        active.clear()
        return
    for name in stopped:
        active.pop(name, None)


def _active_preview_selection(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> dict[str, Any] | None:
    actions: dict[str, dict[str, Any]] = {}
    active: dict[str, tuple[int, dict[str, Any]]] = {}
    ordered = sorted(
        (event for event in events if 0 <= _seq(event) <= through_seq),
        key=_seq,
    )
    for event in ordered:
        if event.get("kind") == "action":
            actions[str(event.get("id") or "")] = event
        elif event.get("kind") == "observation":
            _apply_preview_observation(event, actions, active)
    return max(active.values(), key=lambda item: item[0])[1] if active else None


def _event_with_identity(
    events: list[dict[str, Any]],
    *,
    kind: str,
    event_id: Any,
    seq: Any,
) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in events
            if event.get("kind") == kind and event.get("id") == event_id and _seq(event) == seq
        ),
        None,
    )


def _same_operational_preview(
    first: dict[str, Any],
    second: dict[str, Any],
) -> bool:
    fields = (
        "projection_id",
        "session_name",
        "port",
        "launch_kind",
        "intent_digest",
        "sandbox_instance_id",
        "sandbox_generation",
        "url",
    )
    return all(first.get(field) == second.get(field) for field in fields)


def _selected_preview(
    handoff: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if handoff is None:
        return current
    if isinstance(current, dict) and _same_operational_preview(handoff, current):
        return handoff
    return None


def _preview_selection_is_current(
    selection: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    deliverable_seq: int,
    verdict_seq: int,
) -> bool:
    source_action = _event_with_identity(
        events,
        kind="action",
        event_id=selection.get("source_action_id"),
        seq=selection.get("source_action_seq"),
    )
    source_observation = _event_with_identity(
        events,
        kind="observation",
        event_id=selection.get("source_observation_id"),
        seq=selection.get("source_observation_seq"),
    )
    reconstructed = (
        preview_identity_from_pair(source_action, source_observation)
        if isinstance(source_action, dict) and isinstance(source_observation, dict)
        else None
    )
    handoff = _active_preview_selection(events, through_seq=deliverable_seq)
    current = _active_preview_selection(events, through_seq=verdict_seq - 1)
    selected = _selected_preview(handoff, current)
    return (
        reconstructed == selection
        and selection == selected
        and selection.get("source_observation_seq", verdict_seq) < verdict_seq
    )


def _preview_verification_url(
    selection: dict[str, Any],
    artifact_path: str,
) -> str | None:
    port = selection.get("port")
    if type(port) is not int:
        return None
    base = f"http://127.0.0.1:{port}"
    serve_dir = selection.get("static_serve_dir")
    if serve_dir is None:
        return f"{base}/"
    root = posixpath.normpath(str(serve_dir or "."))
    artifact = posixpath.normpath(artifact_path)
    if root != "." and artifact != root and not artifact.startswith(f"{root}/"):
        return None
    relative = artifact if root == "." else posixpath.relpath(artifact, root)
    if relative == "index.html":
        return f"{base}/"
    encoded = "/".join(quote(part, safe="") for part in relative.split("/"))
    return f"{base}/{encoded}"
