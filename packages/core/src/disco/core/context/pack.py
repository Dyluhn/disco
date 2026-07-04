"""ContextPack — the compact, model-facing projection of a ContextLedger.

This is the seed of the prompt assembler. ``from_ledger`` performs the pure
derivation: filter out resolved failures, cap omittable source kinds per the
CompactionPolicy, and surface the run anchors (goal/contract/version/todo). The
full event-log + workspace-file ingestion that fills ``current_todo`` from
``todo.md`` and assembles the final prompt lands in CXT-2/CXT-4.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from .artifact_memory import ArtifactMemoryRef
from .compaction import CompactionPolicy
from .ledger import ContextLedger, DirectEditRef, ResourceRef, VerifierFailureRef
from .source_priority import SourceKind

_T = TypeVar("_T")


def _cap(seq: Sequence[_T], kind: SourceKind, policy: CompactionPolicy) -> tuple[_T, ...]:
    """Cap a sequence to the policy limit for ``kind``. Never-compact kinds
    (and uncapped kinds) are returned in full."""
    limit = policy.limit_for(kind)
    if limit is None:
        return tuple(seq)
    return tuple(seq[:limit])


class ContextPack(BaseModel):
    """What the model actually sees: anchors + unresolved failures + capped refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    active_goal: str | None = None
    active_contract: str | None = None
    current_version: int = 0
    current_todo: str | None = None
    design_direction: str | None = None
    todo_ref: ArtifactMemoryRef | None = None
    latest_failures: tuple[VerifierFailureRef, ...] = ()
    unresolved_comments: tuple[str, ...] = ()
    direct_edits_summary: tuple[DirectEditRef, ...] = ()
    resource_refs: tuple[ResourceRef, ...] = ()
    recoverable_refs: tuple[ArtifactMemoryRef, ...] = ()
    allowed_next_actions: tuple[str, ...] = ()

    @classmethod
    def from_ledger(
        cls,
        ledger: ContextLedger,
        *,
        policy: CompactionPolicy | None = None,
        allowed_next_actions: tuple[str, ...] = (),
        todo_text: str | None = None,
        design_direction: str | None = None,
    ) -> ContextPack:
        policy = policy or CompactionPolicy.default()
        direction_text = (
            design_direction.strip()
            if design_direction is not None and design_direction.strip()
            else None
        )

        # Only UNRESOLVED failures reach the model; never-compact keeps them all.
        unresolved_failures = tuple(
            f for f in ledger.latest_verifier_failures if not f.resolved
        )
        return cls(
            active_goal=ledger.active_goal,
            active_contract=ledger.active_contract,
            current_version=ledger.current_version,
            current_todo=todo_text,
            design_direction=direction_text,
            todo_ref=ledger.todo_ref,
            latest_failures=_cap(unresolved_failures, SourceKind.VERIFIER_FAILURE, policy),
            unresolved_comments=_cap(ledger.unresolved_comments, SourceKind.COMMENT, policy),
            direct_edits_summary=_cap(ledger.direct_edits, SourceKind.DIRECT_EDIT, policy),
            resource_refs=_cap(ledger.resource_manifest, SourceKind.RESOURCE, policy),
            recoverable_refs=_cap(ledger.retained_refs, SourceKind.RECOVERABLE_REF, policy),
            allowed_next_actions=allowed_next_actions,
        )
