"""ArtifactMemoryRef — a typed pointer to a durable ``.disco/context/*`` file.

The files themselves are created in CXT-2; this is the stable typed handle that
the ledger and ContextPack carry so the model view can point at recoverable,
file-backed memory without holding its contents inline.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ArtifactMemoryKind(str, Enum):
    """The durable context artifacts. Values mirror the ``.disco/context/*``
    filenames CXT-2 will create, so a ref maps 1:1 to a file on disk."""

    GOAL = "current_goal"
    TODO = "todo"
    DECISIONS = "decisions"
    ASSUMPTIONS = "assumptions"
    VERIFIER_FAILURES = "verifier_failures"
    RESOURCE_MANIFEST = "resource_manifest"
    DIRECT_EDITS = "direct_edits"
    UNRESOLVED_COMMENTS = "unresolved_comments"
    SOURCE_PRIORITY = "source_priority"
    SUMMARY = "summary"
    # [REL-2a] the shared per-artifact runtime manifest — one record per OUTPUT artifact
    # {id,path,kind,sha,shown,verified,verify_verdict,export,preview}. Distinct from RESOURCE_MANIFEST (input
    # resources); folds the scattered output-artifact truth (existence/sha/shown/verified/export/
    # preview) so output-truth/export/the REL-1 verifier/product-evidence read ONE source.
    ARTIFACT_MANIFEST = "artifact_manifest"


class ArtifactMemoryRef(BaseModel):
    """A pointer to a durable context file (recoverable, not inlined)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ArtifactMemoryKind
    rel_path: str
    sha256: str | None = None
    updated_at: datetime | None = None
