"""Event-log deliverable discovery + deliverable-type checks (PKG-08 extraction).

Scans the event log for workspace-relative deliverable paths and judges whether
a scenario's expected deliverable type was satisfied. Extracted from
``runner.py`` so the discovery McCabe score is scoped to this module.

The public symbols are re-exported by ``runner.py`` so existing imports are
unchanged.
"""

from __future__ import annotations

from typing import Any


def _locate_deliverables(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan the event log and return workspace-relative deliverable paths.

    Recognises the same tool names as ``_common._declared_artifacts`` so that
    both the download jail and the verifier agree on what the run produced.

    Each entry is ``{"path": str, "kind": str, "tool": str}``.
    """
    deliverables: list[dict[str, Any]] = []
    for evt in events:
        kind = evt.get("kind")
        if kind == "observation":
            _collect_observation_deliverables(evt, deliverables)
        elif kind == "deliverable":
            _collect_deliverable_event(evt, deliverables)
    return deliverables


def _collect_observation_deliverables(
    evt: dict[str, Any], deliverables: list[dict[str, Any]]
) -> None:
    """Collect deliverables from a successful observation event."""
    tr: dict[str, Any] = evt.get("tool_result") or {}
    if not tr.get("success"):
        return
    tool_name = str(tr.get("tool_name", ""))
    structured: dict[str, Any] = tr.get("structured") or {}
    collector = _OBSERVATION_COLLECTORS.get(tool_name)
    if collector is not None:
        collector(structured, tool_name, deliverables)


def _collect_deck_observation(
    structured: dict[str, Any], tool_name: str, deliverables: list[dict[str, Any]]
) -> None:
    """Collect deliverables from sheet_generate / slides_generate."""
    fn = structured.get("filename")
    if isinstance(fn, str) and fn:
        deliverables.append({"path": fn, "kind": "file", "tool": tool_name})
    es = structured.get("editable_source")
    if isinstance(es, str) and es:
        deliverables.append({"path": es, "kind": "editable_source", "tool": tool_name})


def _collect_image_observation(
    structured: dict[str, Any], tool_name: str, deliverables: list[dict[str, Any]]
) -> None:
    """Collect deliverables from image_generate."""
    p = structured.get("path")
    if isinstance(p, str) and p:
        deliverables.append({"path": p, "kind": "image", "tool": tool_name})


def _collect_audio_observation(
    structured: dict[str, Any], tool_name: str, deliverables: list[dict[str, Any]]
) -> None:
    """Collect deliverables from audio_overview."""
    for key in ("mp3_path", "transcript_path"):
        p = structured.get(key)
        if isinstance(p, str) and p:
            deliverables.append({"path": p, "kind": key.replace("_path", ""), "tool": tool_name})


_OBSERVATION_COLLECTORS: dict[str, Any] = {
    "sheet_generate": _collect_deck_observation,
    "slides_generate": _collect_deck_observation,
    "image_generate": _collect_image_observation,
    "audio_overview": _collect_audio_observation,
}


def _collect_deliverable_event(evt: dict[str, Any], deliverables: list[dict[str, Any]]) -> None:
    """Collect a deliverable event (files or app handoff)."""
    artifact_kind = str(evt.get("artifact_kind", "app"))
    path = str(evt.get("path", ""))
    if artifact_kind == "files":
        if path:
            deliverables.append({"path": path, "kind": "files", "tool": "deliverable"})
    elif artifact_kind == "app":
        # codex round-5: a live-app handoff (the build surface's PRIMARY output for
        # "make me a website") — collect it so a scenario expecting an app can't pass
        # with nothing delivered, and so its preview URL gets validated.
        deliverables.append(
            {
                "path": path,
                "kind": "app",
                "tool": "deliverable",
                "deployment_url": str(evt.get("deployment_url", "")),
            }
        )


# File extensions the runner can download + validate. A deliverable without one of these
# (e.g. a live "app" deliverable) is NOT a file to fetch, so it's skipped — not a failure.
_VALIDATABLE_EXTS = (
    ".pdf",
    ".mp3",
    ".wav",
    ".xlsx",
    ".pptx",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
)

# Expected-deliverable-type → the extensions that satisfy it. A scenario that declares
# `expect.deliverable_type` must actually produce a matching file (codex round-2 false-pass).
_DELIVERABLE_TYPE_EXTS: dict[str, tuple[str, ...]] = {
    "deck": (".pptx", ".pdf", ".html", ".htm"),
    "pdf": (".pdf",),
    "audio": (".mp3", ".wav"),
    "sheet": (".xlsx",),
    "image": (".png", ".jpg", ".jpeg"),
}


def _deliverable_type_satisfied(deliverables: list[dict[str, Any]], dtype: str | None) -> bool:
    """True if the scenario's expected deliverable_type is satisfied. None/unrecognised type
    → no gate. ``app`` is matched by deliverable KIND (a live-app handoff, no file extension);
    other types require ≥1 deliverable with a matching file extension."""
    if not dtype:
        return True
    if str(dtype).lower() == "app":
        return any(d.get("kind") == "app" for d in deliverables)
    exts = _DELIVERABLE_TYPE_EXTS.get(str(dtype).lower())
    if exts is None:
        return True
    return any(str(d.get("path", "")).lower().endswith(exts) for d in deliverables)


__all__ = [
    "_DELIVERABLE_TYPE_EXTS",
    "_VALIDATABLE_EXTS",
    "_deliverable_type_satisfied",
    "_locate_deliverables",
]
