"""[REL-2a] Single-source projection of EMITTED output-artifact paths from the event log.

This is the ONE definition of "which workspace-relative paths did this conversation emit as
results" — the download jail's allowlist. The agent-server route (`_declared_artifacts`) and the
REL-2a artifact-manifest fold both derive from THIS helper so the two can never drift. Moving the
logic here (from the route) is a pure no-op refactor — the route's existing tests pin equivalence.

Sources collected per tool (unchanged from the original route logic):
  sheet_generate / slides_generate — structured["filename"] + structured["editable_source"]
  image_generate                   — structured["path"]
  audio_overview                   — structured["mp3_path"] + structured["transcript_path"]
  DeliverableEvent artifact_kind="files" — e.path
"""

from __future__ import annotations

import posixpath

from ..env import disco_env
from ..events import DeliverableEvent, Event, ObservationEvent

# [REL-2a step2b] Shadow flag — when ON, the artifact-manifest fold dual-writes AND a core site
# compares the maintained manifest against this projection, logging divergence (RETURNS legacy; no
# reader switched). Default OFF → byte-identical to today. Promote only after live 0-divergence.
_SHADOW_FLAG = "ARTIFACT_MANIFEST_SHADOW"


def manifest_shadow_enabled() -> bool:
    """True iff DISCO_ARTIFACT_MANIFEST_SHADOW is set truthy (default OFF)."""
    return str(disco_env(_SHADOW_FLAG) or "").strip().lower() in ("1", "true", "yes", "on")


def manifest_path_divergence(
    projected: set[str], manifest_paths: set[str]
) -> tuple[set[str], set[str]]:
    """Compare the single-source projection against the maintained manifest's paths. Returns
    ``(missing, extra)`` — paths the projection has but the manifest lacks, and paths the manifest
    has but the projection doesn't. Both empty ⇒ the manifest faithfully tracks emitted artifacts."""
    return (projected - manifest_paths, manifest_paths - projected)


def artifact_paths_from_events(events: list[Event]) -> set[str]:
    """The set of workspace-relative paths this conversation EMITTED as results (normalized)."""
    out: set[str] = set()
    for e in events:
        if (
            isinstance(e, ObservationEvent)
            and e.tool_result.success
            and e.tool_result.structured
        ):
            tn = e.tool_result.tool_name
            s = e.tool_result.structured
            if tn in ("sheet_generate", "slides_generate"):
                fn = s.get("filename")
                if isinstance(fn, str) and fn:
                    out.add(posixpath.normpath(fn))
                es = s.get("editable_source")
                if isinstance(es, str) and es:
                    out.add(posixpath.normpath(es))
            elif tn == "image_generate":
                p = s.get("path")
                if isinstance(p, str) and p:
                    out.add(posixpath.normpath(p))
            elif tn == "audio_overview":
                for key in ("mp3_path", "transcript_path"):
                    p = s.get(key)
                    if isinstance(p, str) and p:
                        out.add(posixpath.normpath(p))
        elif isinstance(e, DeliverableEvent) and e.artifact_kind == "files":
            out.add(posixpath.normpath(e.path))
    return out
