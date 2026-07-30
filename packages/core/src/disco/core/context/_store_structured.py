"""ArtifactMemoryStore structured-kind collaborators.

Extracted from ``store.py`` so the store class stays under the public-method
limit. These functions implement the JSON (structured) kind read/write
operations over a ``WorkspaceFS``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef
from .ledger import (
    ArtifactRecord,
    DirectEditRef,
    ResourceRef,
    VerifierFailureRef,
)
from .source_priority import SourcePriority

_RESOURCE_ADAPTER = TypeAdapter(list[ResourceRef])
_ARTIFACT_ADAPTER = TypeAdapter(list[ArtifactRecord])
_DIRECT_EDIT_ADAPTER = TypeAdapter(list[DirectEditRef])
_FAILURE_ADAPTER = TypeAdapter(list[VerifierFailureRef])
_COMMENTS_ADAPTER = TypeAdapter(list[str])


class _WritableFS(Protocol):
    async def write_file(self, path: str, data: bytes) -> None: ...


_PathFor = Callable[[ArtifactMemoryKind], str]
_RefFactory = Callable[[ArtifactMemoryKind, str], ArtifactMemoryRef]
_ReadOptional = Callable[[str], Awaitable[bytes | None]]
_ReadJSON = Callable[[ArtifactMemoryKind], Awaitable[object | None]]
_WriteJSON = Callable[[ArtifactMemoryKind, object], Awaitable[ArtifactMemoryRef]]


async def write_json_kind(
    fs: _WritableFS,
    kind: ArtifactMemoryKind,
    payload: object,
    path_for_fn: _PathFor,
    ref_fn: _RefFactory,
) -> ArtifactMemoryRef:
    """Write a JSON-kind file."""
    path = path_for_fn(kind)
    body = json.dumps(payload, indent=2) + "\n"
    await fs.write_file(path, body.encode("utf-8"))
    return ref_fn(kind, path)


async def read_json_raw(
    kind: ArtifactMemoryKind,
    json_kinds: frozenset[ArtifactMemoryKind],
    path_for_fn: _PathFor,
    read_opt_fn: _ReadOptional,
    recovery_error_cls: type[Exception],
) -> object | None:
    """Parsed JSON, None if absent, ContextRecoveryError if present-but-corrupt."""
    if kind not in json_kinds:
        raise ValueError(f"{kind.value} is not a json kind")
    path = path_for_fn(kind)
    data = await read_opt_fn(path)
    if data is None:
        return None
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise recovery_error_cls(kind, path, f"invalid JSON: {exc}") from exc


def _validate_or_raise[T](
    raw: object,
    adapter: TypeAdapter[list[T]],
    kind: ArtifactMemoryKind,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> tuple[T, ...]:
    """Validate raw JSON with an adapter, or raise ContextRecoveryError."""
    try:
        return tuple(adapter.validate_python(raw))
    except ValidationError as exc:
        raise recovery_error_cls(
            kind,
            path_for_fn(kind),
            f"schema mismatch: {exc}",
        ) from exc


async def record_resources(
    resources: tuple[ResourceRef, ...],
    write_json_fn: _WriteJSON,
) -> ArtifactMemoryRef:
    return await write_json_fn(
        ArtifactMemoryKind.RESOURCE_MANIFEST, [r.model_dump(mode="json") for r in resources]
    )


async def read_resources(
    read_json_fn: _ReadJSON,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> tuple[ResourceRef, ...]:
    raw = await read_json_fn(ArtifactMemoryKind.RESOURCE_MANIFEST)
    if raw is None:
        return ()
    return _validate_or_raise(
        raw,
        _RESOURCE_ADAPTER,
        ArtifactMemoryKind.RESOURCE_MANIFEST,
        path_for_fn,
        recovery_error_cls,
    )


async def record_artifacts(
    artifacts: tuple[ArtifactRecord, ...],
    write_json_fn: _WriteJSON,
) -> ArtifactMemoryRef:
    return await write_json_fn(
        ArtifactMemoryKind.ARTIFACT_MANIFEST, [a.model_dump(mode="json") for a in artifacts]
    )


async def read_artifacts(
    read_json_fn: _ReadJSON,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> tuple[ArtifactRecord, ...]:
    raw = await read_json_fn(ArtifactMemoryKind.ARTIFACT_MANIFEST)
    if raw is None:
        return ()
    return _validate_or_raise(
        raw,
        _ARTIFACT_ADAPTER,
        ArtifactMemoryKind.ARTIFACT_MANIFEST,
        path_for_fn,
        recovery_error_cls,
    )


async def record_direct_edits(
    edits: tuple[DirectEditRef, ...],
    write_json_fn: _WriteJSON,
) -> ArtifactMemoryRef:
    return await write_json_fn(
        ArtifactMemoryKind.DIRECT_EDITS, [e.model_dump(mode="json") for e in edits]
    )


async def read_direct_edits(
    read_json_fn: _ReadJSON,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> tuple[DirectEditRef, ...]:
    raw = await read_json_fn(ArtifactMemoryKind.DIRECT_EDITS)
    if raw is None:
        return ()
    return _validate_or_raise(
        raw, _DIRECT_EDIT_ADAPTER, ArtifactMemoryKind.DIRECT_EDITS, path_for_fn, recovery_error_cls
    )


async def record_verifier_failures(
    failures: tuple[VerifierFailureRef, ...],
    write_json_fn: _WriteJSON,
) -> ArtifactMemoryRef:
    return await write_json_fn(
        ArtifactMemoryKind.VERIFIER_FAILURES, [f.model_dump(mode="json") for f in failures]
    )


async def read_verifier_failures(
    read_json_fn: _ReadJSON,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> tuple[VerifierFailureRef, ...]:
    raw = await read_json_fn(ArtifactMemoryKind.VERIFIER_FAILURES)
    if raw is None:
        return ()
    return _validate_or_raise(
        raw, _FAILURE_ADAPTER, ArtifactMemoryKind.VERIFIER_FAILURES, path_for_fn, recovery_error_cls
    )


async def record_comments(
    comments: tuple[str, ...],
    write_json_fn: _WriteJSON,
) -> ArtifactMemoryRef:
    return await write_json_fn(ArtifactMemoryKind.UNRESOLVED_COMMENTS, list(comments))


async def read_comments(
    read_json_fn: _ReadJSON,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> tuple[str, ...]:
    raw = await read_json_fn(ArtifactMemoryKind.UNRESOLVED_COMMENTS)
    if raw is None:
        return ()
    return _validate_or_raise(
        raw,
        _COMMENTS_ADAPTER,
        ArtifactMemoryKind.UNRESOLVED_COMMENTS,
        path_for_fn,
        recovery_error_cls,
    )


async def write_source_priority(
    sp: SourcePriority,
    write_json_fn: _WriteJSON,
) -> ArtifactMemoryRef:
    return await write_json_fn(ArtifactMemoryKind.SOURCE_PRIORITY, sp.model_dump(mode="json"))


async def read_source_priority(
    read_json_fn: _ReadJSON,
    path_for_fn: _PathFor,
    recovery_error_cls: type[Exception],
) -> SourcePriority:
    raw = await read_json_fn(ArtifactMemoryKind.SOURCE_PRIORITY)
    if raw is None:
        return SourcePriority.default()
    try:
        return SourcePriority.model_validate(raw)
    except ValidationError as exc:
        raise recovery_error_cls(
            ArtifactMemoryKind.SOURCE_PRIORITY,
            path_for_fn(ArtifactMemoryKind.SOURCE_PRIORITY),
            f"schema mismatch: {exc}",
        ) from exc
