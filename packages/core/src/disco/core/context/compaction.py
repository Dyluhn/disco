"""CompactionPolicy — pure policy data governing what context may be omitted.

The ACT of compacting (emitting events, rewriting state) lands in CXT-3. This is
only the knobs + the per-kind limit lookup, so CXT-1's ContextPack.from_ledger
and later assemblers share one source of truth for caps.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .source_priority import SourceKind

# Kinds that anchor the run and must never be dropped to make room.
_NEVER_COMPACT: tuple[SourceKind, ...] = (
    SourceKind.GOAL,
    SourceKind.CONTRACT,
    SourceKind.VERIFIER_FAILURE,
    SourceKind.DIRECT_EDIT,
)


class CompactionPolicy(BaseModel):
    """Thresholds for omitting omittable context. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_history_chars: int = 20_000
    max_retained_refs: int = 50
    max_resource_refs: int = 30
    max_recoverable_refs: int = 30
    max_comments: int = 30
    never_compact: tuple[SourceKind, ...] = _NEVER_COMPACT

    @classmethod
    def default(cls) -> CompactionPolicy:
        return cls()

    def limit_for(self, kind: SourceKind) -> int | None:
        """The max number of items to retain for ``kind``.

        Returns ``None`` (unbounded) for never-compact kinds and for kinds that
        are not count-capped here; otherwise the configured cap.
        """
        if kind in self.never_compact:
            return None
        return {
            SourceKind.RESOURCE: self.max_resource_refs,
            SourceKind.RECOVERABLE_REF: self.max_recoverable_refs,
            SourceKind.COMMENT: self.max_comments,
        }.get(kind)
