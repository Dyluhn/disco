"""disco.core.context — durable, structured context runtime (P0).

Pure, serializable value objects for moving Build/Agent memory out of chat and
into structured state: a ContextLedger (durable, file-backed in CXT-2) projected
into a compact, model-facing ContextPack (assembled in CXT-4). No frontend, tool,
or loop-runtime dependencies — these are stable domain models only.
"""

from __future__ import annotations

from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef
from .compaction import CompactionPolicy
from .ledger import (
    ContextLedger,
    DirectEditKind,
    DirectEditRef,
    HandoffRef,
    ResolvedContextRange,
    ResourceRef,
    Severity,
    VerifierFailureRef,
)
from .compaction import (
    context_compact_if_needed,
    context_mark_resolved,
    context_write_summary,
    resolved_ranges_from_events,
)
from .pack import ContextPack
from .source_priority import SourceKind, SourcePriority
from .store import (
    ArtifactMemoryStore,
    ContextRecoveryError,
    ReconstructResult,
    RecoveryNote,
    WorkspaceFS,
)

__all__ = [
    "ArtifactMemoryKind",
    "ArtifactMemoryRef",
    "ArtifactMemoryStore",
    "CompactionPolicy",
    "ContextLedger",
    "ContextPack",
    "ContextRecoveryError",
    "DirectEditKind",
    "DirectEditRef",
    "HandoffRef",
    "ReconstructResult",
    "RecoveryNote",
    "ResolvedContextRange",
    "ResourceRef",
    "Severity",
    "SourceKind",
    "SourcePriority",
    "VerifierFailureRef",
    "WorkspaceFS",
    "context_compact_if_needed",
    "context_mark_resolved",
    "context_write_summary",
    "resolved_ranges_from_events",
]
