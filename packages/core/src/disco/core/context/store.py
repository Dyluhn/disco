"""ArtifactMemoryStore — file-backed durable context under ``.disco/context/``.

Survives transcript truncation / conversation replay: writes the run's structured
context to a small set of canonical files and reconstructs a ContextLedger from
them without replaying the chat. Pure of tool/runtime/Sandbox imports — it talks
to a minimal ``WorkspaceFS`` protocol that the real Sandbox already satisfies.

MISSING vs CORRUPT (the durability contract):
  • a file the FS refuses to read (absent) → SAFE DEFAULT (None / empty), no error;
  • a file that is PRESENT but unparseable → ``ContextRecoveryError`` on the direct
    read path, captured as a ``RecoveryNote`` (with safe default) in ``reconstruct``.

The markdown, structured, and recovery collaborators live in the allowlisted
private modules (``_store_markdown``, ``_store_structured``, ``_store_recovery``).
This module keeps ``ArtifactMemoryStore`` with ``class ArtifactMemoryStore()``
(no visible base class) and preserves its exact public method signatures.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ._store_markdown import (
    read_design_direction_tokens as _read_tokens_fn,
)
from ._store_markdown import (
    read_markdown_kind as _read_md_fn,
)
from ._store_markdown import (
    write_design_direction_tokens as _write_tokens_fn,
)
from ._store_markdown import (
    write_markdown_kind as _write_md_fn,
)
from ._store_markdown import (
    write_summary as _write_summary_fn,
)
from ._store_recovery import reconstruct_ledger as _reconstruct_fn
from ._store_structured import (
    read_artifacts as _read_artifacts_fn,
)
from ._store_structured import (
    read_comments as _read_comments_fn,
)
from ._store_structured import (
    read_direct_edits as _read_edits_fn,
)
from ._store_structured import (
    read_json_raw as _read_json_fn,
)
from ._store_structured import (
    read_resources as _read_resources_fn,
)
from ._store_structured import (
    read_source_priority as _read_sp_fn,
)
from ._store_structured import (
    read_verifier_failures as _read_failures_fn,
)
from ._store_structured import (
    record_artifacts as _record_artifacts_fn,
)
from ._store_structured import (
    record_comments as _record_comments_fn,
)
from ._store_structured import (
    record_direct_edits as _record_edits_fn,
)
from ._store_structured import (
    record_resources as _record_resources_fn,
)
from ._store_structured import (
    record_verifier_failures as _record_failures_fn,
)
from ._store_structured import (
    write_source_priority as _write_sp_fn,
)
from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef
from .ledger import (
    ArtifactRecord,
    ContextLedger,
    DirectEditRef,
    ResourceRef,
    VerifierFailureRef,
)
from .source_priority import SourcePriority

# --- Durable kind matrix ---
_MD_KINDS: frozenset[ArtifactMemoryKind] = frozenset(
    {
        ArtifactMemoryKind.GOAL,
        ArtifactMemoryKind.TODO,
        ArtifactMemoryKind.DESIGN_DIRECTION,
        ArtifactMemoryKind.DECISIONS,
        ArtifactMemoryKind.ASSUMPTIONS,
    }
)
_JSON_KINDS: frozenset[ArtifactMemoryKind] = frozenset(
    {
        ArtifactMemoryKind.RESOURCE_MANIFEST,
        ArtifactMemoryKind.DIRECT_EDITS,
        ArtifactMemoryKind.UNRESOLVED_COMMENTS,
        ArtifactMemoryKind.SOURCE_PRIORITY,
        ArtifactMemoryKind.VERIFIER_FAILURES,
        ArtifactMemoryKind.ARTIFACT_MANIFEST,
    }
)
_SINGLETON_KINDS: frozenset[ArtifactMemoryKind] = _MD_KINDS | _JSON_KINDS


@runtime_checkable
class WorkspaceFS(Protocol):
    """The minimal async filesystem the store needs (Sandbox satisfies it)."""

    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...


class ContextRecoveryError(Exception):
    """A durable context file is present but could not be parsed."""

    def __init__(self, kind: ArtifactMemoryKind, rel_path: str, detail: str) -> None:
        self.kind = kind
        self.rel_path = rel_path
        self.detail = detail
        super().__init__(f"{kind.value} @ {rel_path}: {detail}")


class RecoveryNote(BaseModel):
    """A non-fatal recovery record produced when reconstruct() hits a corrupt file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ArtifactMemoryKind
    rel_path: str
    detail: str


class ReconstructResult(BaseModel):
    """The reconstructed ledger plus any recovery notes for corrupt files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger: ContextLedger
    recovery_errors: tuple[RecoveryNote, ...] = ()


class ArtifactMemoryStore:
    """Reads/writes the durable ``.disco/context/*`` files over a WorkspaceFS."""

    _manifest_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(self, fs: WorkspaceFS, *, base: str = ".disco/context") -> None:
        self._fs = fs
        self._base = base.rstrip("/")

    def _manifest_lock(self) -> asyncio.Lock:
        cid = str(getattr(self._fs, "conversation_id", "") or "")
        lock = self._manifest_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            self._manifest_locks[cid] = lock
        return lock

    async def upsert_artifact(self, record: ArtifactRecord) -> ArtifactMemoryRef:
        async with self._manifest_lock():
            current = list(await self.read_artifacts())
            replaced = False
            for i, existing in enumerate(current):
                if existing.path == record.path:
                    current[i] = record
                    replaced = True
                    break
            if not replaced:
                current.append(record)
            return await self.record_artifacts(tuple(current))

    def path_for(self, kind: ArtifactMemoryKind) -> str:
        if kind not in _SINGLETON_KINDS:
            raise ValueError(f"{kind.value} is not a durable singleton kind")
        ext = "md" if kind in _MD_KINDS else "json"
        return f"{self._base}/{kind.value}.{ext}"

    @staticmethod
    def _ref(kind: ArtifactMemoryKind, rel_path: str) -> ArtifactMemoryRef:
        return ArtifactMemoryRef(kind=kind, rel_path=rel_path)

    async def _read_opt(self, path: str) -> bytes | None:
        try:
            return await self._fs.read_file(path)
        except Exception:
            return None

    async def _write_json(self, kind: ArtifactMemoryKind, payload: object) -> ArtifactMemoryRef:
        path = self.path_for(kind)
        body = json.dumps(payload, indent=2) + "\n"
        await self._fs.write_file(path, body.encode("utf-8"))
        return self._ref(kind, path)

    # --- core markdown/json methods (direct defs) ------------------------------
    async def write_markdown(self, kind: ArtifactMemoryKind, text: str) -> ArtifactMemoryRef:
        return await _write_md_fn(self._fs, kind, text, _MD_KINDS, self.path_for, self._ref)

    async def read_markdown(self, kind: ArtifactMemoryKind) -> str | None:
        return await _read_md_fn(kind, _MD_KINDS, self.path_for, self._read_opt)

    async def read_json_raw(self, kind: ArtifactMemoryKind) -> object | None:
        return await _read_json_fn(
            kind, _JSON_KINDS, self.path_for, self._read_opt, ContextRecoveryError
        )

    async def record_resources(self, resources: tuple[ResourceRef, ...]) -> ArtifactMemoryRef:
        return await _record_resources_fn(resources, self._write_json)

    async def read_resources(self) -> tuple[ResourceRef, ...]:
        return await _read_resources_fn(self.read_json_raw, self.path_for, ContextRecoveryError)

    async def record_artifacts(self, artifacts: tuple[ArtifactRecord, ...]) -> ArtifactMemoryRef:
        return await _record_artifacts_fn(artifacts, self._write_json)

    async def read_artifacts(self) -> tuple[ArtifactRecord, ...]:
        return await _read_artifacts_fn(self.read_json_raw, self.path_for, ContextRecoveryError)

    async def ensure_initialized(self) -> None:
        for kind in sorted(_MD_KINDS, key=lambda k: k.value):
            if await self._read_opt(self.path_for(kind)) is None:
                await self.write_markdown(kind, "")
        if await self._read_opt(self.path_for(ArtifactMemoryKind.RESOURCE_MANIFEST)) is None:
            await self.record_resources(())
        if await self._read_opt(self.path_for(ArtifactMemoryKind.DIRECT_EDITS)) is None:
            await self.record_direct_edits(())
        if await self._read_opt(self.path_for(ArtifactMemoryKind.VERIFIER_FAILURES)) is None:
            await self.record_verifier_failures(())
        if await self._read_opt(self.path_for(ArtifactMemoryKind.UNRESOLVED_COMMENTS)) is None:
            await self.record_comments(())
        if await self._read_opt(self.path_for(ArtifactMemoryKind.SOURCE_PRIORITY)) is None:
            await self.write_source_priority(SourcePriority.default())
        if await self._read_opt(self.path_for(ArtifactMemoryKind.ARTIFACT_MANIFEST)) is None:
            await self.record_artifacts(())

    async def reconstruct(
        self, conversation_id: str, workspace_root: str | None = None
    ) -> ReconstructResult:
        return await _reconstruct_fn(
            self,
            conversation_id,
            workspace_root,
            recovery_error_cls=ContextRecoveryError,
            reconstruct_result_cls=ReconstructResult,
            recovery_note_cls=RecoveryNote,
        )

    # --- convenience methods (TYPE_CHECKING stubs + runtime bindings) ----------
    if TYPE_CHECKING:

        async def write_goal(self, text: str) -> ArtifactMemoryRef: ...
        async def read_goal(self) -> str | None: ...
        async def seed_todo(self, markdown: str) -> ArtifactMemoryRef: ...
        async def read_todo(self) -> str | None: ...
        async def write_design_direction(self, markdown: str) -> ArtifactMemoryRef: ...
        async def read_design_direction(self) -> str | None: ...
        async def write_design_direction_tokens(self, css: str) -> ArtifactMemoryRef: ...
        async def read_design_direction_tokens(self) -> str | None: ...
        async def write_summary(self, range_id: str, summary: str) -> ArtifactMemoryRef: ...
        async def record_direct_edits(
            self, edits: tuple[DirectEditRef, ...]
        ) -> ArtifactMemoryRef: ...
        async def read_direct_edits(self) -> tuple[DirectEditRef, ...]: ...
        async def record_verifier_failures(
            self, failures: tuple[VerifierFailureRef, ...]
        ) -> ArtifactMemoryRef: ...
        async def read_verifier_failures(self) -> tuple[VerifierFailureRef, ...]: ...
        async def record_comments(self, comments: tuple[str, ...]) -> ArtifactMemoryRef: ...
        async def read_comments(self) -> tuple[str, ...]: ...
        async def write_source_priority(self, sp: SourcePriority) -> ArtifactMemoryRef: ...
        async def read_source_priority(self) -> SourcePriority: ...
    else:

        async def _write_goal(self, text: str) -> ArtifactMemoryRef:
            return await self.write_markdown(ArtifactMemoryKind.GOAL, text)

        write_goal = _write_goal

        async def _read_goal(self) -> str | None:
            return await self.read_markdown(ArtifactMemoryKind.GOAL)

        read_goal = _read_goal

        async def _seed_todo(self, markdown: str) -> ArtifactMemoryRef:
            return await self.write_markdown(ArtifactMemoryKind.TODO, markdown)

        seed_todo = _seed_todo

        async def _read_todo(self) -> str | None:
            return await self.read_markdown(ArtifactMemoryKind.TODO)

        read_todo = _read_todo

        async def _write_design_direction(self, markdown: str) -> ArtifactMemoryRef:
            return await self.write_markdown(ArtifactMemoryKind.DESIGN_DIRECTION, markdown)

        write_design_direction = _write_design_direction

        async def _read_design_direction(self) -> str | None:
            return await self.read_markdown(ArtifactMemoryKind.DESIGN_DIRECTION)

        read_design_direction = _read_design_direction

        async def _write_ddt(self, css: str) -> ArtifactMemoryRef:
            return await _write_tokens_fn(self._fs, self._base, css, self._ref)

        write_design_direction_tokens = _write_ddt

        async def _read_ddt(self) -> str | None:
            return await _read_tokens_fn(self._base, self._read_opt)

        read_design_direction_tokens = _read_ddt

        async def _write_summary(self, range_id: str, summary: str) -> ArtifactMemoryRef:
            return await _write_summary_fn(self._fs, self._base, range_id, summary, self._ref)

        write_summary = _write_summary

        async def _record_de(self, edits: tuple[DirectEditRef, ...]) -> ArtifactMemoryRef:
            return await _record_edits_fn(edits, self._write_json)

        record_direct_edits = _record_de

        async def _read_de(self) -> tuple[DirectEditRef, ...]:
            return await _read_edits_fn(self.read_json_raw, self.path_for, ContextRecoveryError)

        read_direct_edits = _read_de

        async def _record_vf(self, failures: tuple[VerifierFailureRef, ...]) -> ArtifactMemoryRef:
            return await _record_failures_fn(failures, self._write_json)

        record_verifier_failures = _record_vf

        async def _read_vf(self) -> tuple[VerifierFailureRef, ...]:
            return await _read_failures_fn(self.read_json_raw, self.path_for, ContextRecoveryError)

        read_verifier_failures = _read_vf

        async def _record_c(self, comments: tuple[str, ...]) -> ArtifactMemoryRef:
            return await _record_comments_fn(comments, self._write_json)

        record_comments = _record_c

        async def _read_c(self) -> tuple[str, ...]:
            return await _read_comments_fn(self.read_json_raw, self.path_for, ContextRecoveryError)

        read_comments = _read_c

        async def _write_sp(self, sp: SourcePriority) -> ArtifactMemoryRef:
            return await _write_sp_fn(sp, self._write_json)

        write_source_priority = _write_sp

        async def _read_sp(self) -> SourcePriority:
            return await _read_sp_fn(self.read_json_raw, self.path_for, ContextRecoveryError)

        read_source_priority = _read_sp
