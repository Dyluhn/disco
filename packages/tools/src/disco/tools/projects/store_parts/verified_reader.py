"""`_VerifiedVersionReader`: strict version reading, payload verification,
and fd-proof trees.

Every read is a fresh proof against the filesystem. This collaborator never
caches counts, never trusts directory-name digests, and rejects symlinked or
missing directories, entries, or metadata at every layer.

`verify_version_details` is split into itself plus
`_assert_facts_match_record` — a pure decomposition of the same branching
that brings its cyclomatic complexity back under budget; relocating the
method unchanged would not have.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .json_atomic import _read_json_regular_nofollow
from .tree_scan import _scan_immutable_tree_details, _tree_digest_from_hashes
from .version_serde import _payload_matches_record, _version_from_dict

if TYPE_CHECKING:
    from ..store import VersionRecord, WorkspaceTreeFacts


def _assert_facts_match_record(
    facts: WorkspaceTreeFacts, record: VersionRecord, seq: int
) -> None:
    from disco.tools.projects import store

    if (
        facts.file_count != record.file_count
        or facts.total_bytes != record.total_bytes
        or facts.tree_digest != record.tree_digest
    ):
        raise store.StorageError(f"version workspace disagrees with metadata: {seq!r}")


class _VerifiedVersionReader:
    """Strict version reading, payload verification, and fd-proof trees.

    Every read is a fresh proof against the filesystem. This collaborator
    never caches counts, never trusts directory-name digests, and rejects
    symlinked or missing directories, entries, or metadata at every layer.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def read_version_index(self, cid: str) -> list[VersionRecord]:
        from disco.tools.projects import store

        index = store._versions_index_path(self._root, cid)
        raw = _read_json_regular_nofollow(
            index,
            label="versions index",
            missing_ok=True,
        )
        if raw is store._MISSING_JSON:
            return []
        if not isinstance(raw, list):
            raise store.StorageError("versions index unreadable: expected a list")
        records: list[VersionRecord] = []
        seen_seqs: set[int] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise store.StorageError("versions index unreadable: malformed row")
            try:
                record = _version_from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise store.StorageError(
                    f"versions index unreadable: malformed row: {exc}"
                ) from exc
            if (
                not _payload_matches_record(item, record)
                or not store._safe_seq(record.seq)
                or record.seq in seen_seqs
                or record.file_count < 0
                or record.total_bytes < 0
                or len(record.tree_digest) != 64
                or any(char not in "0123456789abcdef" for char in record.tree_digest)
            ):
                raise store.StorageError("versions index unreadable: invalid or duplicate row")
            seen_seqs.add(record.seq)
            records.append(record)
        records.sort(key=lambda r: r.seq)
        return records

    def existing_version_records(self, cid: str) -> list[VersionRecord]:
        from disco.tools.projects import store

        records: list[VersionRecord] = []
        for record in self.read_version_index(cid):
            version_dir = store._version_dir_path(self._root, cid, record)
            workspace = version_dir / store._WORKSPACE
            if version_dir.is_symlink() or workspace.is_symlink():
                raise store.StorageError(
                    f"version directory or workspace is symlinked: {record.seq}"
                )
            if workspace.is_dir():
                records.append(record)
        return records

    def verify_version(self, cid: str, seq: int) -> VersionRecord:
        """Return a version only after fresh tree, index, sidecar, and byte proof."""
        record, _details = self.verify_version_details(cid, seq)
        return record

    def verify_version_details(
        self,
        cid: str,
        seq: int,
    ) -> tuple[VersionRecord, dict[str, tuple[str, int]]]:
        """Return a version and exact file manifest after all fresh proofs.

        This method never trusts cached counts or the directory-name digest.  It
        rejects missing/pruned trees, symlinked directories or entries, secret
        files, malformed metadata, and any disagreement between all three views.
        """
        from disco.tools.projects import store

        if not store._safe_seq(seq):
            raise store.StorageError(f"unsafe version seq: {seq!r}")
        store._require_plain_project_dir(self._root, cid)
        versions = store._versions_dir(self._root, cid)
        if versions.is_symlink() or not versions.is_dir():
            raise store.StorageError("versions directory is missing or symlinked")
        records = self.read_version_index(cid)
        matches = [record for record in records if record.seq == seq]
        if len(matches) != 1:
            raise store.StorageError(f"unknown or duplicate version seq: {seq!r}")
        record = matches[0]

        version_dir = store._version_dir_path(self._root, cid, record)
        workspace = version_dir / store._WORKSPACE
        metadata = version_dir / store._VERSION_METADATA
        if version_dir.is_symlink() or not version_dir.is_dir():
            raise store.StorageError(f"version directory missing or symlinked: {seq!r}")
        if workspace.is_symlink() or not workspace.is_dir():
            raise store.StorageError(f"version workspace missing or symlinked: {seq!r}")
        raw_metadata = _read_json_regular_nofollow(
            metadata,
            label=f"version metadata {seq!r}",
        )
        if not isinstance(raw_metadata, dict) or not _payload_matches_record(raw_metadata, record):
            raise store.StorageError(f"version metadata disagrees with index: {seq!r}")

        details, total_bytes = _scan_immutable_tree_details(workspace)
        hashes = {rel: digest for rel, (digest, _size) in details.items()}
        facts = store.WorkspaceTreeFacts(
            file_count=len(details),
            total_bytes=total_bytes,
            tree_digest=_tree_digest_from_hashes(hashes),
        )
        _assert_facts_match_record(facts, record, seq)
        return record, details

    def open_version_workspace_fd(
        self,
        cid: str,
        record: VersionRecord,
    ) -> int:
        from disco.tools.projects import store

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        opened: list[int] = []
        try:
            current = os.open(self._root, flags)
            opened.append(current)
            for segment in (
                cid,
                store._VERSIONS,
                store._version_dir_path(self._root, cid, record).name,
                store._WORKSPACE,
            ):
                current = os.open(segment, flags, dir_fd=current)
                opened.append(current)
            workspace_fd = opened.pop()
            return workspace_fd
        except OSError as exc:
            raise store.StorageError(f"verified version path changed: {exc}") from exc
        finally:
            for fd in reversed(opened):
                os.close(fd)
