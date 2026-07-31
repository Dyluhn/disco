"""Deterministic, bounded OOXML/OPC structural validation."""

from __future__ import annotations

import pathlib
import zipfile
import zlib
from dataclasses import dataclass

from ._opc_relationships import (  # noqa: F401 — compatibility re-exports
    _OPC_REL_NS,
    _ForbiddenXmlDeclaration,
    _relationship_problems,
    _relationship_xml_root,
    _RelationshipLimits,
)
from ._opc_stream import (  # noqa: F401 — compatibility re-exports
    _ZIP_LOCAL_HEADER,
    _ZIP_LOCAL_SIGNATURE,
    _member_stream_problem,
)

_MAX_OPC_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_OPC_MEMBERS = 10_000
_MAX_OPC_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_OPC_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_OPC_COMPRESSION_RATIO = 1_000
_MAX_OPC_RELATIONSHIP_BYTES = 4 * 1024 * 1024
_MAX_OPC_RELATIONSHIPS = 10_000
_MAX_OPC_TARGET_CHARS = 2_048
_MAX_OPC_NAME_CHARS = 1_024
_MAX_OPC_PROBLEMS = 100


@dataclass(frozen=True)
class _OpcLimits:
    archive_bytes: int
    members: int
    member_bytes: int
    total_bytes: int
    compression_ratio: int
    relationship_bytes: int
    relationships: int
    target_chars: int
    name_chars: int
    problems: int

    def relationship_limits(self) -> _RelationshipLimits:
        return _RelationshipLimits(
            part_bytes=self.relationship_bytes,
            relationships=self.relationships,
            target_chars=self.target_chars,
            problems=self.problems,
        )


def _default_limits() -> _OpcLimits:
    return _OpcLimits(
        archive_bytes=_MAX_OPC_ARCHIVE_BYTES,
        members=_MAX_OPC_MEMBERS,
        member_bytes=_MAX_OPC_MEMBER_BYTES,
        total_bytes=_MAX_OPC_TOTAL_BYTES,
        compression_ratio=_MAX_OPC_COMPRESSION_RATIO,
        relationship_bytes=_MAX_OPC_RELATIONSHIP_BYTES,
        relationships=_MAX_OPC_RELATIONSHIPS,
        target_chars=_MAX_OPC_TARGET_CHARS,
        name_chars=_MAX_OPC_NAME_CHARS,
        problems=_MAX_OPC_PROBLEMS,
    )


def _member_names_or_problem(
    members: list[zipfile.ZipInfo], limits: _OpcLimits
) -> tuple[list[str], str | None]:
    if len(members) > limits.members:
        return (
            [],
            "pptx archive has too many members for structural validation "
            f"({len(members)} > {limits.members})",
        )
    names = [member.filename for member in members]
    if any(len(name) > limits.name_chars for name in names):
        return (
            names,
            "pptx archive contains a member name longer than the "
            f"structural-validation limit ({limits.name_chars} characters)",
        )
    if len(set(names)) != len(names):
        return names, "pptx archive contains duplicate part names"
    return names, None


def _member_budget_problem(
    members: list[zipfile.ZipInfo], limits: _OpcLimits
) -> str | None:
    total_size = 0
    for member in members:
        if member.file_size > limits.member_bytes:
            return (
                f"pptx member {member.filename} exceeds the uncompressed-size "
                f"limit ({member.file_size} > {limits.member_bytes})"
            )
        total_size += member.file_size
        if total_size > limits.total_bytes:
            return (
                "pptx archive exceeds the total uncompressed-size limit "
                f"({limits.total_bytes} bytes)"
            )
        ratio = member.file_size / max(member.compress_size, 1)
        if member.file_size > 0 and ratio > limits.compression_ratio:
            return (
                f"pptx member {member.filename} exceeds the compression-ratio "
                f"limit ({limits.compression_ratio}:1)"
            )
    return None


def _archive_integrity_problems(path: str, limits: _OpcLimits) -> list[str]:
    archive_size = pathlib.Path(path).stat().st_size
    if archive_size > limits.archive_bytes:
        return [
            "pptx archive exceeds the structural-validation size limit "
            f"({limits.archive_bytes} bytes)"
        ]
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        member_names, manifest_problem = _member_names_or_problem(
            members, limits
        )
        if manifest_problem is not None:
            return [manifest_problem]
        budget_problem = _member_budget_problem(members, limits)
        if budget_problem is not None:
            return [budget_problem]
        if archive.fp is None:
            return ["pptx archive closed before structural validation"]
        for member in members:
            stream_problem = _member_stream_problem(archive.fp, member)
            if stream_problem is not None:
                return [stream_problem]
        return _relationship_problems(
            archive, set(member_names), limits.relationship_limits()
        )


def _opc_integrity_problems(
    path: str, *, limits: _OpcLimits | None = None
) -> list[str]:
    """Return bounded ZIP-stream and relationship-integrity evidence."""
    try:
        return _archive_integrity_problems(path, limits or _default_limits())
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        return ["pptx is not a valid zip archive (not an OOXML package)"]
    except (NotImplementedError, RuntimeError, ValueError, zlib.error) as exc:
        return [f"pptx archive could not be safely inspected: {exc}"]
    except OSError as exc:
        return [f"pptx could not be read: {exc}"]
