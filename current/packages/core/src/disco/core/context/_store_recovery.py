"""ArtifactMemoryStore recovery/reconstruction collaborators.

Extracted from ``store.py`` so the store class stays under the public-method
limit. This function rebuilds a ContextLedger from the durable files.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, cast

from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef
from .ledger import (
    ContextLedger,
    DirectEditRef,
    ResourceRef,
    VerifierFailureRef,
)

if TYPE_CHECKING:
    from .store import ContextRecoveryError, ReconstructResult, RecoveryNote


class _ArtifactStore(Protocol):
    async def read_verifier_failures(self) -> tuple[VerifierFailureRef, ...]: ...
    async def read_resources(self) -> tuple[ResourceRef, ...]: ...
    async def read_direct_edits(self) -> tuple[DirectEditRef, ...]: ...
    async def read_comments(self) -> tuple[str, ...]: ...
    async def read_goal(self) -> str | None: ...
    async def read_todo(self) -> str | None: ...
    async def read_markdown(self, kind: ArtifactMemoryKind) -> str | None: ...
    def path_for(self, kind: ArtifactMemoryKind) -> str: ...

    @staticmethod
    def _ref(kind: ArtifactMemoryKind, rel_path: str) -> ArtifactMemoryRef: ...


async def _safe_read[T](
    read_fn: Callable[[], Awaitable[tuple[T, ...]]],
    notes: list[RecoveryNote],
    recovery_error_cls: type[ContextRecoveryError],
    recovery_note_cls: type[RecoveryNote],
) -> tuple[T, ...]:
    """Call a read function, capturing ContextRecoveryError as a RecoveryNote."""
    try:
        return await read_fn()
    except recovery_error_cls as raw:
        exc = cast("ContextRecoveryError", raw)
        notes.append(recovery_note_cls(kind=exc.kind, rel_path=exc.rel_path, detail=exc.detail))
        return ()


async def reconstruct_ledger(
    store: _ArtifactStore,
    conversation_id: str,
    workspace_root: str | None,
    *,
    recovery_error_cls: type[ContextRecoveryError],
    reconstruct_result_cls: type[ReconstructResult],
    recovery_note_cls: type[RecoveryNote],
) -> ReconstructResult:
    """Rebuild a ContextLedger from the durable files."""
    notes: list[RecoveryNote] = []
    goal = await store.read_goal()
    todo = await store.read_todo()
    failures = await _safe_read(
        store.read_verifier_failures, notes, recovery_error_cls, recovery_note_cls
    )
    resources = await _safe_read(store.read_resources, notes, recovery_error_cls, recovery_note_cls)
    edits = await _safe_read(store.read_direct_edits, notes, recovery_error_cls, recovery_note_cls)
    comments = await _safe_read(store.read_comments, notes, recovery_error_cls, recovery_note_cls)

    retained: list[ArtifactMemoryRef] = []
    for kind in (ArtifactMemoryKind.DECISIONS, ArtifactMemoryKind.ASSUMPTIONS):
        if await store.read_markdown(kind) is not None:
            retained.append(store._ref(kind, store.path_for(kind)))

    todo_ref = (
        store._ref(ArtifactMemoryKind.TODO, store.path_for(ArtifactMemoryKind.TODO))
        if todo is not None
        else None
    )

    ledger = ContextLedger(
        conversation_id=conversation_id,
        workspace_root=workspace_root,
        active_goal=goal,
        todo_ref=todo_ref,
        retained_refs=tuple(retained),
        latest_verifier_failures=failures,
        unresolved_comments=comments,
        direct_edits=edits,
        resource_manifest=resources,
    )
    return reconstruct_result_cls(ledger=ledger, recovery_errors=tuple(notes))
