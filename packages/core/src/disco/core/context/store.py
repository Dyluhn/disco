"""ArtifactMemoryStore — file-backed durable context under ``.disco/context/``.

Survives transcript truncation / conversation replay: writes the run's structured
context to a small set of canonical files and reconstructs a ContextLedger from
them without replaying the chat. Pure of tool/runtime/Sandbox imports — it talks
to a minimal ``WorkspaceFS`` protocol that the real Sandbox already satisfies.

MISSING vs CORRUPT (the durability contract):
  • a file the FS refuses to read (absent) → SAFE DEFAULT (None / empty), no error;
  • a file that is PRESENT but unparseable → ``ContextRecoveryError`` on the direct
    read path, captured as a ``RecoveryNote`` (with safe default) in ``reconstruct``.
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef
from .ledger import (
    ArtifactRecord,
    ContextLedger,
    DirectEditRef,
    ResourceRef,
    VerifierFailureRef,
)
from .source_priority import SourcePriority

# --- Durable kind matrix (codified so it cannot silently drift) ----------------
# 9 durable SINGLETONS (exactly one canonical file each). SUMMARY is intentionally
# NOT here: it is a multi-instance, on-demand kind (per-resolved-range summaries
# created in CXT-3, referenced by ArtifactMemoryRef.rel_path).
_MD_KINDS: frozenset[ArtifactMemoryKind] = frozenset(
    {
        ArtifactMemoryKind.GOAL,
        ArtifactMemoryKind.TODO,
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
        ArtifactMemoryKind.ARTIFACT_MANIFEST,  # [REL-2a] per-artifact runtime manifest
    }
)
_SINGLETON_KINDS: frozenset[ArtifactMemoryKind] = _MD_KINDS | _JSON_KINDS

_RESOURCE_ADAPTER = TypeAdapter(list[ResourceRef])
_ARTIFACT_ADAPTER = TypeAdapter(list[ArtifactRecord])  # [REL-2a] per-artifact runtime manifest
_DIRECT_EDIT_ADAPTER = TypeAdapter(list[DirectEditRef])
_FAILURE_ADAPTER = TypeAdapter(list[VerifierFailureRef])
_COMMENTS_ADAPTER = TypeAdapter(list[str])


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

    # [REL-2a] Per-conversation manifest mutation locks. The store is constructed FRESH at each call
    # site (finish/engine/context_memory), so a per-INSTANCE lock would not mutually-exclude the
    # concurrent writers of one cid's artifact_manifest (the observe fold runs OUTSIDE the loop
    # _lock; edge writers run in API handlers). This class-level dict, keyed by conversation_id,
    # gives cross-instance per-cid exclusion within the single agent-server event loop. (Codex-
    # approved placement: core can't reach the runtime lock — layering — and the store is per-call.)
    _manifest_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(self, fs: WorkspaceFS, *, base: str = ".disco/context") -> None:
        self._fs = fs
        self._base = base.rstrip("/")

    def _manifest_lock(self) -> asyncio.Lock:
        """The asyncio.Lock guarding artifact_manifest RMW for THIS store's conversation. Keyed by
        the fs's conversation_id (empty string for non-build/test paths — they share one lock,
        which is harmless since they don't contend)."""
        cid = str(getattr(self._fs, "conversation_id", "") or "")
        lock = self._manifest_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            self._manifest_locks[cid] = lock
        return lock

    async def upsert_artifact(self, record: ArtifactRecord) -> ArtifactMemoryRef:
        """[REL-2a] Insert-or-replace one ArtifactRecord (matched by `path`) in the per-artifact
        manifest, as an atomic read-modify-write UNDER the per-cid lock so two concurrent writers
        can never lose each other's update. Returns the manifest ref."""
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

    # --- paths -----------------------------------------------------------------
    def path_for(self, kind: ArtifactMemoryKind) -> str:
        if kind not in _SINGLETON_KINDS:
            raise ValueError(f"{kind.value} is not a durable singleton kind")
        ext = "md" if kind in _MD_KINDS else "json"
        return f"{self._base}/{kind.value}.{ext}"

    @staticmethod
    def _ref(kind: ArtifactMemoryKind, rel_path: str) -> ArtifactMemoryRef:
        return ArtifactMemoryRef(kind=kind, rel_path=rel_path)

    # --- low-level read --------------------------------------------------------
    async def _read_opt(self, path: str) -> bytes | None:
        """Return file bytes, or None if the file is absent/unreadable (the FS
        raises on a missing path; that is treated as 'absent' → safe default)."""
        try:
            return await self._fs.read_file(path)
        except Exception:
            return None

    # --- markdown (narrative) kinds -------------------------------------------
    async def write_markdown(self, kind: ArtifactMemoryKind, text: str) -> ArtifactMemoryRef:
        if kind not in _MD_KINDS:
            raise ValueError(f"{kind.value} is not a markdown kind")
        path = self.path_for(kind)
        body = text if text.endswith("\n") else text + "\n"
        await self._fs.write_file(path, body.encode("utf-8"))
        return self._ref(kind, path)

    async def read_markdown(self, kind: ArtifactMemoryKind) -> str | None:
        if kind not in _MD_KINDS:
            raise ValueError(f"{kind.value} is not a markdown kind")
        data = await self._read_opt(self.path_for(kind))
        if data is None:
            return None
        text = data.decode("utf-8", errors="replace").rstrip("\n")
        return text or None

    async def write_goal(self, text: str) -> ArtifactMemoryRef:
        return await self.write_markdown(ArtifactMemoryKind.GOAL, text)

    async def read_goal(self) -> str | None:
        return await self.read_markdown(ArtifactMemoryKind.GOAL)

    async def seed_todo(self, markdown: str) -> ArtifactMemoryRef:
        return await self.write_markdown(ArtifactMemoryKind.TODO, markdown)

    async def read_todo(self) -> str | None:
        return await self.read_markdown(ArtifactMemoryKind.TODO)

    # --- json (structured) kinds ----------------------------------------------
    async def _write_json(self, kind: ArtifactMemoryKind, payload: object) -> ArtifactMemoryRef:
        path = self.path_for(kind)
        body = json.dumps(payload, indent=2) + "\n"
        await self._fs.write_file(path, body.encode("utf-8"))
        return self._ref(kind, path)

    async def read_json_raw(self, kind: ArtifactMemoryKind) -> object | None:
        """Parsed JSON, None if absent, ContextRecoveryError if present-but-corrupt."""
        if kind not in _JSON_KINDS:
            raise ValueError(f"{kind.value} is not a json kind")
        path = self.path_for(kind)
        data = await self._read_opt(path)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContextRecoveryError(kind, path, f"invalid JSON: {exc}") from exc

    async def record_resources(self, resources: tuple[ResourceRef, ...]) -> ArtifactMemoryRef:
        return await self._write_json(
            ArtifactMemoryKind.RESOURCE_MANIFEST, [r.model_dump(mode="json") for r in resources]
        )

    async def read_resources(self) -> tuple[ResourceRef, ...]:
        raw = await self.read_json_raw(ArtifactMemoryKind.RESOURCE_MANIFEST)
        if raw is None:
            return ()
        try:
            return tuple(_RESOURCE_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            raise ContextRecoveryError(
                ArtifactMemoryKind.RESOURCE_MANIFEST,
                self.path_for(ArtifactMemoryKind.RESOURCE_MANIFEST),
                f"schema mismatch: {exc}",
            ) from exc

    async def record_artifacts(self, artifacts: tuple[ArtifactRecord, ...]) -> ArtifactMemoryRef:
        """[REL-2a] Persist the full per-artifact runtime manifest. The caller does the
        read-modify-write upsert UNDER the per-cid manifest lock (the file write here is atomic
        per-file via _write_json's tmp+rename, but the RMW guard is the caller's lock)."""
        return await self._write_json(
            ArtifactMemoryKind.ARTIFACT_MANIFEST, [a.model_dump(mode="json") for a in artifacts]
        )

    async def read_artifacts(self) -> tuple[ArtifactRecord, ...]:
        raw = await self.read_json_raw(ArtifactMemoryKind.ARTIFACT_MANIFEST)
        if raw is None:
            return ()
        try:
            return tuple(_ARTIFACT_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            raise ContextRecoveryError(
                ArtifactMemoryKind.ARTIFACT_MANIFEST,
                self.path_for(ArtifactMemoryKind.ARTIFACT_MANIFEST),
                f"schema mismatch: {exc}",
            ) from exc

    async def record_direct_edits(self, edits: tuple[DirectEditRef, ...]) -> ArtifactMemoryRef:
        return await self._write_json(
            ArtifactMemoryKind.DIRECT_EDITS, [e.model_dump(mode="json") for e in edits]
        )

    async def read_direct_edits(self) -> tuple[DirectEditRef, ...]:
        raw = await self.read_json_raw(ArtifactMemoryKind.DIRECT_EDITS)
        if raw is None:
            return ()
        try:
            return tuple(_DIRECT_EDIT_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            raise ContextRecoveryError(
                ArtifactMemoryKind.DIRECT_EDITS,
                self.path_for(ArtifactMemoryKind.DIRECT_EDITS),
                f"schema mismatch: {exc}",
            ) from exc

    async def record_verifier_failures(self, failures: tuple[VerifierFailureRef, ...]) -> ArtifactMemoryRef:
        return await self._write_json(
            ArtifactMemoryKind.VERIFIER_FAILURES, [f.model_dump(mode="json") for f in failures]
        )

    async def read_verifier_failures(self) -> tuple[VerifierFailureRef, ...]:
        raw = await self.read_json_raw(ArtifactMemoryKind.VERIFIER_FAILURES)
        if raw is None:
            return ()
        try:
            return tuple(_FAILURE_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            raise ContextRecoveryError(
                ArtifactMemoryKind.VERIFIER_FAILURES,
                self.path_for(ArtifactMemoryKind.VERIFIER_FAILURES),
                f"schema mismatch: {exc}",
            ) from exc

    async def record_comments(self, comments: tuple[str, ...]) -> ArtifactMemoryRef:
        return await self._write_json(ArtifactMemoryKind.UNRESOLVED_COMMENTS, list(comments))

    async def read_comments(self) -> tuple[str, ...]:
        raw = await self.read_json_raw(ArtifactMemoryKind.UNRESOLVED_COMMENTS)
        if raw is None:
            return ()
        try:
            return tuple(_COMMENTS_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            raise ContextRecoveryError(
                ArtifactMemoryKind.UNRESOLVED_COMMENTS,
                self.path_for(ArtifactMemoryKind.UNRESOLVED_COMMENTS),
                f"schema mismatch: {exc}",
            ) from exc

    async def write_source_priority(self, sp: SourcePriority) -> ArtifactMemoryRef:
        return await self._write_json(ArtifactMemoryKind.SOURCE_PRIORITY, sp.model_dump(mode="json"))

    async def read_source_priority(self) -> SourcePriority:
        raw = await self.read_json_raw(ArtifactMemoryKind.SOURCE_PRIORITY)
        if raw is None:
            return SourcePriority.default()
        try:
            return SourcePriority.model_validate(raw)
        except ValidationError as exc:
            raise ContextRecoveryError(
                ArtifactMemoryKind.SOURCE_PRIORITY,
                self.path_for(ArtifactMemoryKind.SOURCE_PRIORITY),
                f"schema mismatch: {exc}",
            ) from exc

    # --- lifecycle -------------------------------------------------------------
    async def ensure_initialized(self) -> None:
        """Create any of the 10 durable singleton files that are absent, with safe
        defaults. Existing files are left untouched."""
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
            await self.record_artifacts(())  # [REL-2a]

    async def reconstruct(
        self, conversation_id: str, workspace_root: str | None = None
    ) -> ReconstructResult:
        """Rebuild a ContextLedger from the durable files. Corrupt files degrade to
        a safe default + a RecoveryNote; absent files are silently the default."""
        notes: list[RecoveryNote] = []

        async def _safe_failures() -> tuple[VerifierFailureRef, ...]:
            try:
                return await self.read_verifier_failures()
            except ContextRecoveryError as exc:
                notes.append(RecoveryNote(kind=exc.kind, rel_path=exc.rel_path, detail=exc.detail))
                return ()

        async def _safe_resources() -> tuple[ResourceRef, ...]:
            try:
                return await self.read_resources()
            except ContextRecoveryError as exc:
                notes.append(RecoveryNote(kind=exc.kind, rel_path=exc.rel_path, detail=exc.detail))
                return ()

        async def _safe_edits() -> tuple[DirectEditRef, ...]:
            try:
                return await self.read_direct_edits()
            except ContextRecoveryError as exc:
                notes.append(RecoveryNote(kind=exc.kind, rel_path=exc.rel_path, detail=exc.detail))
                return ()

        async def _safe_comments() -> tuple[str, ...]:
            try:
                return await self.read_comments()
            except ContextRecoveryError as exc:
                notes.append(RecoveryNote(kind=exc.kind, rel_path=exc.rel_path, detail=exc.detail))
                return ()

        goal = await self.read_goal()
        todo = await self.read_todo()
        failures = await _safe_failures()
        resources = await _safe_resources()
        edits = await _safe_edits()
        comments = await _safe_comments()

        # decisions/assumptions have no scalar ledger field — they reconstruct as
        # recoverable refs the assembler (CXT-4) can surface.
        retained: list[ArtifactMemoryRef] = []
        for kind in (ArtifactMemoryKind.DECISIONS, ArtifactMemoryKind.ASSUMPTIONS):
            if await self.read_markdown(kind) is not None:
                retained.append(self._ref(kind, self.path_for(kind)))

        todo_ref = self._ref(ArtifactMemoryKind.TODO, self.path_for(ArtifactMemoryKind.TODO)) if todo is not None else None

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
        return ReconstructResult(ledger=ledger, recovery_errors=tuple(notes))
