"""Small, transport-independent report → deck observation helpers.

The harness never needs the deck's private product objects.  It records only
bounded stage labels and validates the public typed ``slides_generate`` result
shape that an outside observer can see on the Agent conversation stream.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._observe import redact

_MAX_STAGES = 200
_MAX_ARTIFACTS = 32
_TERMINAL_STATUSES = {"FINISHED", "ERROR", "STUCK", "STOPPED"}


def event_from_frame(frame: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = frame.get("event")
    return nested if isinstance(nested, Mapping) else frame


def frame_status(frame: Mapping[str, Any]) -> str:
    event = event_from_frame(frame)
    status = event.get("status") or frame.get("status")
    if frame.get("type") == "state" and isinstance(frame.get("state"), Mapping):
        status = frame["state"].get("execution_status") or status
    return str(status or "").upper()


def frame_stage(frame: Mapping[str, Any]) -> str:
    event = event_from_frame(frame)
    for key in ("stage", "phase", "kind", "name", "tool_name", "type"):
        value = event.get(key) or frame.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return "event"


def _walk(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        found.append(value)
        for item in value.values():
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    return found


def _artifact_name(value: Any) -> str | None:
    if isinstance(value, str):
        lowered = value.casefold()
        if any(ext in lowered for ext in (".pptx", ".pdf", ".html", ".svg")):
            return value[:240]
        return None
    if isinstance(value, Mapping):
        for key in ("path", "filename", "name", "url", "artifact_path"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                found = _artifact_name(candidate)
                if found:
                    return found
    return None


def validate_frames(frames: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate only public typed deck evidence; never infer success from prose."""
    tool_seen = False
    tool_success = False
    typed_structured = False
    degraded = False
    formats: list[str] = []
    artifacts: list[str] = []
    for mapping in _walk(list(frames)):
        tool_name = str(mapping.get("tool_name") or mapping.get("name") or "")
        structured = mapping.get("structured")
        if tool_name == "slides_generate":
            tool_seen = True
            tool_success = tool_success or mapping.get("success") is True
            if isinstance(structured, Mapping):
                # The typed tool envelope is present when structured output
                # has a deck/slides payload or explicitly declares its format.
                typed_structured = typed_structured or (
                    isinstance(structured.get("slides"), list)
                    or isinstance(structured.get("deck"), Mapping)
                    or isinstance(structured.get("artifact"), Mapping)
                )
                fmt = structured.get("format")
                if isinstance(fmt, str) and fmt:
                    formats.append(fmt[:40])
            for key in ("degraded", "fallback", "fallback_used"):
                if mapping.get(key) is True or (
                    isinstance(structured, Mapping) and structured.get(key) is True
                ):
                    degraded = True
        for key in ("artifact", "artifacts", "structured"):
            for candidate in _walk(mapping.get(key)):
                name = _artifact_name(candidate)
                if name and name not in artifacts:
                    artifacts.append(name)
                    if len(artifacts) >= _MAX_ARTIFACTS:
                        break
        if len(artifacts) >= _MAX_ARTIFACTS:
            break
    terminal = next(
        (frame_status(frame) for frame in reversed(frames) if frame_status(frame)),
        "",
    )
    native_pptx = any(fmt.casefold() == "pptx" for fmt in formats) or any(
        name.casefold().endswith(".pptx") for name in artifacts
    )
    return redact(
        {
            "slides_generate_seen": tool_seen,
            "slides_generate_success": tool_success,
            "typed_slides": tool_seen and tool_success and typed_structured,
            "native_pptx": native_pptx,
            "degraded": degraded,
            "formats": sorted(set(formats)),
            "artifacts": artifacts,
            "terminal_status": terminal,
        }
    )


def summarize_frames(
    frames: Sequence[Mapping[str, Any]],
    *,
    elapsed_ms: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Return bounded stage/timing/typed-artifact diagnostics for a target run."""
    stages: list[dict[str, Any]] = []
    for index, frame in enumerate(frames[:_MAX_STAGES]):
        row: dict[str, Any] = {
            "stage": frame_stage(frame),
            "status": frame_status(frame) or None,
        }
        if elapsed_ms is not None and index < len(elapsed_ms):
            row["elapsed_ms"] = max(0, int(elapsed_ms[index]))
        stages.append(row)
    validation = validate_frames(frames)
    return {
        "stage_count": len(stages),
        "stages": stages,
        "terminal_status": validation["terminal_status"],
        "artifact_validation": validation,
    }


def is_terminal(frame: Mapping[str, Any]) -> bool:
    return frame_status(frame) in _TERMINAL_STATUSES
