"""Bulk-archive materialization interior for `snapshot_workspace`: validates
and extracts a backend-provided tar archive into the staging tree without
trusting member paths or types.

Split out of `archive.py` to bring `_write_bulk_archive`'s cyclomatic
complexity back under budget: decomposed into the duplicate/conflict check,
the per-member staging decision, and the outer accumulation loop. Every
guard and error message is reproduced verbatim and in original order. (The
opened tarfile handle is renamed `tar` here, since the original local name
`archive` would shadow the parent-module import every callable in this file
needs.)
"""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from disco.core.secret_paths import is_runtime_secret_path


def _reject_duplicate_or_conflicting_path(rel: str, seen: set[str]) -> None:
    from disco.tools.projects import archive

    if rel in seen:
        raise archive.WorkspaceArchiveError(f"workspace archive contains duplicate path {rel!r}")
    rel_parts = PurePosixPath(rel).parts
    ancestors = [PurePosixPath(*rel_parts[:i]).as_posix() for i in range(1, len(rel_parts))]
    if any(ancestor in seen for ancestor in ancestors) or any(
        prior.startswith(rel + "/") for prior in seen
    ):
        raise archive.WorkspaceArchiveError(
            f"workspace archive contains conflicting path prefix {rel!r}"
        )


@dataclass
class _BulkMemberOutcome:
    """One tar member's staging result. `oversized` is the only skip reason
    that feeds the caller's consecutive-failure abort counter — matching the
    original's ``consecutive_failures`` bookkeeping exactly."""

    rel: str
    bytes_written: int = 0
    skip_reason: str | None = None
    oversized: bool = False


def _stage_bulk_member_bytes(staging: Path, rel: str, data: bytes) -> None:
    target = staging / rel
    current = staging
    for part in PurePosixPath(rel).parts[:-1]:
        current /= part
        if current.exists() and not current.is_dir():
            current.unlink()
        current.mkdir(exist_ok=True)
    if target.is_dir():
        shutil.rmtree(target)
    target.write_bytes(data)


def _stage_bulk_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    staging: Path,
    seen: set[str],
    *,
    max_depth: int,
    max_file_bytes: int,
) -> _BulkMemberOutcome:
    from disco.tools.projects import archive

    rel = archive._archive_relpath(member.name, max_depth=max_depth)
    _reject_duplicate_or_conflicting_path(rel, seen)
    seen.add(rel)
    if not member.isreg():
        raise archive.WorkspaceArchiveError(f"workspace archive contains non-regular entry {rel!r}")
    if any(part in archive._SNAPSHOT_EXCLUDED_DIRS for part in PurePosixPath(rel).parts):
        return _BulkMemberOutcome(rel=rel, skip_reason=f"{rel}: dependency/cache path excluded")
    if is_runtime_secret_path(rel):
        return _BulkMemberOutcome(rel=rel, skip_reason=f"{rel}: runtime secret path excluded")
    if member.size > max_file_bytes:
        return _BulkMemberOutcome(
            rel=rel,
            skip_reason=f"{rel}: {member.size} bytes exceeds the {max_file_bytes}-byte cap",
            oversized=True,
        )
    source = tar.extractfile(member)
    if source is None:
        raise archive.WorkspaceArchiveError(f"workspace archive cannot read {rel!r}")
    data = source.read(max_file_bytes + 1)
    if len(data) != member.size:
        raise archive.WorkspaceArchiveError(
            f"workspace archive truncated {rel!r}: expected {member.size}, got {len(data)}"
        )
    _stage_bulk_member_bytes(staging, rel, data)
    return _BulkMemberOutcome(rel=rel, bytes_written=len(data))


def _write_bulk_archive(
    archive_path: Path,
    staging: Path,
    *,
    max_depth: int,
    max_file_bytes: int,
) -> tuple[list[str], int, list[str]]:
    """Validate and materialize a backend archive without trusting tar paths/types."""
    from disco.tools.projects import archive

    paths: list[str] = []
    skipped: list[str] = []
    total_bytes = 0
    consecutive_failures = 0
    seen: set[str] = set()
    try:
        tar = tarfile.open(archive_path, mode="r:")
    except (tarfile.TarError, EOFError) as exc:
        raise archive.WorkspaceArchiveError(f"workspace archive is invalid: {exc}") from exc
    with tar:
        for member in tar:
            outcome = _stage_bulk_member(
                tar, member, staging, seen, max_depth=max_depth, max_file_bytes=max_file_bytes
            )
            if outcome.skip_reason is not None:
                skipped.append(outcome.skip_reason)
                if outcome.oversized:
                    consecutive_failures += 1
                    if consecutive_failures >= archive._SNAPSHOT_MAX_CONSECUTIVE_FAILURES:
                        raise archive.WorkspaceArchiveError(
                            f"{consecutive_failures} consecutive failures (last: {outcome.rel!r}: "
                            "oversized) — transport presumed dead, aborting snapshot"
                        )
                continue
            paths.append(outcome.rel)
            total_bytes += outcome.bytes_written
            consecutive_failures = 0
    paths.sort()
    return paths, total_bytes, skipped
