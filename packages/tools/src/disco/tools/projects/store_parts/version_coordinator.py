"""`_VersionStateCoordinator`: allocator (never-reuse seq), WAL-protected
pin/unpin, and version life-cycle.

Every write is durably sequenced — journal first, then metadata sidecar, then
index — so any crash leaves a single journal entry the next EX transaction
recovers idempotently. The allocator state file is published before its
migration marker so a crash never permanently poisons a project.

Four of this class's methods carried a cyclomatic-complexity violation
(`_read_pin_journal`, `_recover_pin_journal_if_pending`, `reserve_version_seq`,
`create_version`) and the class itself carried a logical-size violation. Both
are cleared the same way: each oversized method is decomposed into itself
plus several small, single-purpose free functions in this module — a pure
decomposition of the SAME branching/logic, not a relocation of it unchanged
(relocating alone would not have reduced any method's McCabe).

`_write_json_atomic` is resolved through `store`'s own binding at call time
(never a top-of-file import) because a test monkeypatches it directly on the
`store` facade module; see `store_parts/__init__.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disco.core.secret_paths import is_runtime_secret_path

from .json_atomic import _fsync_directory, _fsync_tree
from .pin_journal import (
    _find_version_record,
    _pin_journal_recovery_state,
    _prove_pin_journal_target_tree,
    _replace_version_index_row,
    read_pin_journal,
)
from .tree_scan import _scan_immutable_tree, _scan_tree, _tree_digest_from_hashes
from .verified_reader import _VerifiedVersionReader
from .version_serde import _version_to_dict

if TYPE_CHECKING:
    from ..store import VersionRecord, WorkspaceTreeFacts

_MAX_VERSION_BYTES = 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# `known_version_high_watermark` decomposition
# ---------------------------------------------------------------------------


def _compute_version_high_watermark(root: Path, cid: str, known: int) -> int:
    from disco.tools.projects import store

    versions = store._versions_dir(root, cid)
    if versions.is_symlink():
        raise store.StorageError(f"versions directory is symlinked: {versions}")
    if not versions.exists():
        return known
    if not versions.is_dir():
        raise store.StorageError(f"versions path is not a directory: {versions}")
    try:
        entries = list(versions.iterdir())
    except OSError as exc:
        raise store.StorageError(f"versions directory unreadable: {exc}") from exc
    for entry in entries:
        if entry.is_symlink():
            raise store.StorageError(f"versions directory contains symlink: {entry.name}")
        prefix, separator, _rest = entry.name.partition("-")
        if separator and prefix.isdecimal():
            known = max(known, int(prefix))
    return known


# ---------------------------------------------------------------------------
# `_copy_version_workspace` decomposition
# ---------------------------------------------------------------------------


def _copy_version_workspace_files(
    root: Path,
    cid: str,
    *,
    live_workspace: Path,
    dest_workspace: Path,
    live_hashes: dict[str, str],
    previous: VersionRecord | None,
) -> None:
    from disco.tools.projects import store

    previous_workspace: Path | None = None
    previous_hashes: dict[str, str] = {}
    if previous is not None:
        previous_workspace = store._version_dir_path(root, cid, previous) / store._WORKSPACE
        if previous_workspace.is_dir():
            previous_hashes, _ = _scan_tree(previous_workspace)

    for rel in sorted(live_hashes):
        source = live_workspace / rel
        target = dest_workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if previous_workspace is not None and previous_hashes.get(rel) == live_hashes[rel]:
            previous_source = previous_workspace / rel
            try:
                os.link(previous_source, target)
                continue
            except OSError:
                pass
        shutil.copy2(source, target)


# ---------------------------------------------------------------------------
# `_prune` / `_versions_total_bytes` decomposition
# ---------------------------------------------------------------------------


def _count_versions_total_bytes(root: Path, cid: str, records: list[VersionRecord]) -> int:
    from disco.tools.projects import store

    seen: set[tuple[int, int]] = set()
    total = 0
    for record in records:
        workspace = store._version_dir_path(root, cid, record) / store._WORKSPACE
        if not workspace.is_dir():
            continue
        for path in sorted(p for p in workspace.rglob("*") if p.is_file() and not p.is_symlink()):
            if is_runtime_secret_path(path.relative_to(workspace).as_posix()):
                continue
            st = path.stat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def _drop_unlabeled_overflow(
    records: list[VersionRecord],
    *,
    max_unlabeled: int,
    drop: Any,
) -> bool:
    changed = False
    prunable = [record for record in records if not record.label and not record.pinned]
    for record in prunable[:-max_unlabeled]:
        drop(record)
        records.remove(record)
        changed = True
    return changed


def _drop_over_byte_budget(
    records: list[VersionRecord],
    *,
    total_bytes: Any,
    version_byte_budget: int,
    drop: Any,
) -> bool:
    changed = False
    while total_bytes(records) > version_byte_budget:
        candidate = next(
            (record for record in records if not record.label and not record.pinned),
            None,
        )
        if candidate is None:
            break
        drop(candidate)
        records.remove(candidate)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# `reserve_version_seq` decomposition
# ---------------------------------------------------------------------------


def _validate_allocator_marker(marker_path: Path) -> bool:
    """Validate the (optional) allocator migration marker. Returns whether it
    exists."""
    from disco.tools.projects import store

    if marker_path.is_symlink():
        raise store.StorageError("version allocator marker is symlinked")
    marker_exists = marker_path.exists()
    if marker_exists:
        if not marker_path.is_file():
            raise store.StorageError("version allocator marker is not a regular file")
        try:
            marker = json.loads(marker_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise store.StorageError(f"version allocator marker unreadable: {exc}") from exc
        if marker != {"schema_version": 1}:
            raise store.StorageError("version allocator marker is malformed or tampered")
    return marker_exists


def _is_malformed_sequence_state(raw: object) -> bool:
    from disco.tools.projects import store

    legacy = isinstance(raw, dict) and raw.keys() == {"next_version_seq"}
    current = isinstance(raw, dict) and raw.keys() == {
        "schema_version",
        "next_version_seq",
    }
    return (
        not isinstance(raw, dict)
        or not (legacy or current)
        or (current and raw.get("schema_version") != 1)
        or type(raw.get("next_version_seq")) is not int
        or not store._safe_seq(raw["next_version_seq"])
    )


def _read_reserved_sequence(state_path: Path, *, high_watermark: int, marker_exists: bool) -> int:
    from disco.tools.projects import store

    if state_path.exists():
        if not state_path.is_file():
            raise store.StorageError("version sequence state is not a regular file")
        try:
            raw = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise store.StorageError(f"version sequence state unreadable: {exc}") from exc
        if _is_malformed_sequence_state(raw):
            raise store.StorageError("version sequence state unreadable: malformed payload")
        seq = raw["next_version_seq"]
        if seq <= high_watermark:
            raise store.StorageError("version sequence state is stale or tampered")
        return seq
    if marker_exists:
        raise store.StorageError("version sequence state is missing after allocator migration")
    return high_watermark + 1


# ---------------------------------------------------------------------------
# `create_version` decomposition
# ---------------------------------------------------------------------------


def _reserve_new_version_record(
    coordinator: _VersionStateCoordinator,
    cid: str,
    *,
    label: str,
    trigger: str,
    live_hashes: dict[str, str],
    total_bytes: int,
    pinned: bool,
) -> tuple[VersionRecord, Path, Path]:
    """Reserve a sequence, build the VersionRecord, and prove its version
    directory doesn't already exist. Returns ``(record, versions_dir,
    version_dir)``."""
    from disco.tools.projects import store

    digest = _tree_digest_from_hashes(live_hashes)
    seq = coordinator.reserve_version_seq(cid)
    record = store.VersionRecord(
        seq=seq,
        ts=store._now_iso(),
        label=label,
        trigger=trigger,
        file_count=len(live_hashes),
        total_bytes=total_bytes,
        tree_digest=digest,
        pinned=pinned,
    )
    versions = store._ensure_versions_dir(coordinator._root, cid)
    version_dir = store._version_dir_path(coordinator._root, cid, record)
    if version_dir.is_symlink() or version_dir.exists():
        raise store.StorageError(f"version directory already exists: {version_dir}")
    return record, versions, version_dir


def _assert_staged_facts_match_record(
    staged_facts: WorkspaceTreeFacts, record: VersionRecord
) -> None:
    from disco.tools.projects import store

    if (
        staged_facts.file_count != record.file_count
        or staged_facts.total_bytes != record.total_bytes
        or staged_facts.tree_digest != record.tree_digest
    ):
        raise store.StorageError("staged version bytes disagree with the source scan")


def _discard_staging_dir(staging_dir: Path) -> None:
    if staging_dir.exists() and not staging_dir.is_symlink():
        shutil.rmtree(staging_dir)


def _discard_unindexed_version_dir(
    version_dir: Path, *, published: bool, indexed: bool
) -> None:
    if published and not indexed and version_dir.exists() and not version_dir.is_symlink():
        shutil.rmtree(version_dir)


# ---------------------------------------------------------------------------
# `cut_verified_version_locked` decomposition
# ---------------------------------------------------------------------------


def _reuse_verified_version_if_unchanged(
    coordinator: _VersionStateCoordinator,
    cid: str,
    digest: str,
    pin: bool,
    records: list[VersionRecord],
) -> VersionRecord | None:
    """If the newest already-verified version has this exact tree (and does
    not share inodes with any other version's copy), reuse it — pinning it
    first if requested — instead of publishing a duplicate. Returns None when
    there is nothing to reuse, so the caller falls through to `create_version`."""
    newest = records[-1] if records else None
    if newest is None:
        return None
    verified_previous = coordinator._reader.verify_version(cid, newest.seq)
    if verified_previous.tree_digest != digest or coordinator._version_has_shared_inodes(
        cid, verified_previous
    ):
        return None
    candidate = verified_previous
    if pin and not candidate.pinned:
        candidate = coordinator.set_version_pinned_locked(cid, candidate.seq, True)
        candidate = coordinator._reader.verify_version(cid, candidate.seq)
    coordinator._prune(cid)
    return coordinator._reader.verify_version(cid, candidate.seq)


class _VersionStateCoordinator:
    """Allocator (never-reuse seq), WAL-protected pin/unpin, and version life-cycle.

    Every write is durably sequenced — journal first, then metadata sidecar, then
    index — so any crash leaves a single journal entry the next EX transaction
    recovers idempotently.  The allocator state file is published before its
    migration marker so a crash never permanently poisons a project.
    """

    def __init__(
        self,
        root: Path,
        reader: _VerifiedVersionReader,
        *,
        version_byte_budget: int = _MAX_VERSION_BYTES,
    ) -> None:
        self._root = root
        self._reader = reader
        self._version_byte_budget = version_byte_budget

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _tree_facts(root: Path) -> WorkspaceTreeFacts:
        from disco.tools.projects import store

        hashes, total_bytes = _scan_immutable_tree(root)
        return store.WorkspaceTreeFacts(
            file_count=len(hashes),
            total_bytes=total_bytes,
            tree_digest=_tree_digest_from_hashes(hashes),
        )

    def _version_has_shared_inodes(self, cid: str, record: VersionRecord) -> bool:
        from disco.tools.projects import store

        workspace = store._version_dir_path(self._root, cid, record) / store._WORKSPACE
        return any(
            path.lstat().st_nlink > 1
            for path in workspace.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def write_version_index(self, cid: str, records: list[VersionRecord]) -> None:
        from disco.tools.projects import store

        payload = [_version_to_dict(r) for r in sorted(records, key=lambda r: r.seq)]
        store._write_json_atomic(store._versions_index_path(self._root, cid), payload)

    # -- WAL journal (pin/unpin crash recovery) ---------------------------------

    def _write_pin_journal_durably(
        self,
        cid: str,
        before: VersionRecord,
        after: VersionRecord,
    ) -> None:
        from disco.tools.projects import store

        journal_path = store._pin_journal_path(self._root, cid)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(before),
            "after": _version_to_dict(after),
        }
        store._write_json_atomic(journal_path, payload)

    def _read_pin_journal(self, cid: str) -> tuple[VersionRecord, VersionRecord] | None:
        """Read the pin journal with full no-follow identity checks.

        Thin delegator: the journal module owns reading and validating its own
        durable record (`pin_journal.read_pin_journal`). Kept as a method
        because tests reach it through the coordinator instance.
        """
        return read_pin_journal(self._root, cid)

    def _recover_pin_journal_if_pending(self, cid: str) -> VersionRecord | None:
        """Check for and recover any pending pin journal.

        Must be called under the EX lock.  Valid reachable states:
          - after/after  → clean up journal (fully applied)
          - after/before → roll forward index (metadata applied, index stale)
          - before/before → roll the durable intent forward from the journal
          - before/after → fail closed (impossible ordering, disk tampered)

        Any other inconsistent state also fails closed.  The immutable tree is
        freshly proved before any roll-forward.
        """
        from disco.tools.projects import store

        journal = self._read_pin_journal(cid)
        if journal is None:
            return None

        before, after = journal
        _prove_pin_journal_target_tree(self._root, cid, after)

        (
            metadata_matches_after,
            metadata_matches_before,
            index_matches_after,
            index_matches_before,
            records,
        ) = _pin_journal_recovery_state(self._root, cid, self._reader, before, after)

        version_dir = store._version_dir_path(self._root, cid, after)
        metadata_path = version_dir / store._VERSION_METADATA

        if metadata_matches_after and index_matches_after:
            self._reader.verify_version_details(cid, after.seq)
            self._unlink_journal(cid)
            return after
        elif metadata_matches_after and index_matches_before:
            next_records = _replace_version_index_row(records, after)
            self.write_version_index(cid, next_records)
            self._reader.verify_version_details(cid, after.seq)
            self._unlink_journal(cid)
            return after
        elif metadata_matches_before and index_matches_before:
            store._write_json_atomic(metadata_path, _version_to_dict(after))
            redo_records = _replace_version_index_row(records, after)
            self.write_version_index(cid, redo_records)
            self._reader.verify_version_details(cid, after.seq)
            self._unlink_journal(cid)
            return after
        elif metadata_matches_before and index_matches_after:
            raise store.StorageError(
                "pin journal recovery failed: index was written but metadata was not — "
                "impossible ordering, disk may be tampered"
            )
        else:
            raise store.StorageError(
                "pin journal recovery failed: inconsistent metadata/index state"
            )

    def _unlink_journal(self, cid: str) -> None:
        from disco.tools.projects import store

        journal_path = store._pin_journal_path(self._root, cid)
        if journal_path.exists():
            if journal_path.is_symlink():
                raise store.StorageError("pin journal is symlinked — cannot unlink safely")
            journal_path.unlink()
            _fsync_directory(journal_path.parent)

    # -- sequence allocation ----------------------------------------------------

    def known_version_high_watermark(self, cid: str) -> int:
        known = max((record.seq for record in self._reader.read_version_index(cid)), default=0)
        return _compute_version_high_watermark(self._root, cid, known)

    def reserve_version_seq(self, cid: str) -> int:
        """Durably reserve a never-reused sequence before creating its tree.

        A crash may leave a gap, but pruning can never make a later cut recycle
        an already-issued sequence.  Projects created before this state file was
        introduced are migrated from all still-observable index rows/directories.
        """
        from disco.tools.projects import store

        store._require_plain_project_dir(self._root, cid)
        high_watermark = self.known_version_high_watermark(cid)
        state_path = store._version_sequence_path(self._root, cid)
        marker_path = store._version_allocator_marker_path(self._root, cid)
        marker_exists = _validate_allocator_marker(marker_path)
        seq = _read_reserved_sequence(
            state_path, high_watermark=high_watermark, marker_exists=marker_exists
        )

        store._write_json_atomic(
            state_path,
            {"schema_version": 1, "next_version_seq": seq + 1},
        )
        if not marker_exists:
            store._write_json_atomic(marker_path, {"schema_version": 1})
        return seq

    # -- pin / unpin (WAL-protected) --------------------------------------------

    def set_version_pinned_locked(
        self,
        cid: str,
        seq: int,
        pinned: bool,
    ) -> VersionRecord:
        from disco.tools.projects import store

        if not store._safe_seq(seq):
            raise store.StorageError(f"unsafe version seq: {seq!r}")
        self._reader.verify_version_details(cid, seq)
        records = self._reader.read_version_index(cid)
        current = _find_version_record(records, seq)
        if current is None:
            raise store.StorageError(f"unknown version seq: {seq!r}")

        desired = bool(pinned)
        if current.pinned == desired:
            return self._reader.verify_version(cid, seq)

        updated = replace(current, pinned=desired)
        metadata_path = store._version_dir_path(self._root, cid, updated) / store._VERSION_METADATA
        if not metadata_path.is_file():
            raise store.StorageError(f"version metadata missing: {seq!r}")

        self._write_pin_journal_durably(cid, current, updated)
        try:
            store._write_json_atomic(metadata_path, _version_to_dict(updated))
            next_records = _replace_version_index_row(records, updated)
            self.write_version_index(cid, next_records)
            self._reader.verify_version_details(cid, seq)
            self._unlink_journal(cid)
        except Exception:
            raise

        return self._reader.verify_version(cid, seq)

    # -- version life-cycle -----------------------------------------------------

    def _copy_version_workspace(
        self,
        cid: str,
        *,
        live_workspace: Path,
        dest_workspace: Path,
        live_hashes: dict[str, str],
        previous: VersionRecord | None,
    ) -> None:
        _copy_version_workspace_files(
            self._root,
            cid,
            live_workspace=live_workspace,
            dest_workspace=dest_workspace,
            live_hashes=live_hashes,
            previous=previous,
        )

    def create_version(
        self,
        cid: str,
        *,
        label: str,
        trigger: str,
        live_workspace: Path,
        live_hashes: dict[str, str],
        total_bytes: int,
        records: list[VersionRecord],
        previous: VersionRecord | None,
        pinned: bool,
        prune: bool,
    ) -> VersionRecord:
        """Stage, prove, and publish one new version index row."""
        from disco.tools.projects import store

        record, versions, version_dir = _reserve_new_version_record(
            self,
            cid,
            label=label,
            trigger=trigger,
            live_hashes=live_hashes,
            total_bytes=total_bytes,
            pinned=pinned,
        )

        staging_dir = Path(tempfile.mkdtemp(prefix=f".{record.seq:03d}-staging-", dir=versions))
        dest_workspace = staging_dir / store._WORKSPACE
        published = False
        indexed = False
        try:
            dest_workspace.mkdir(parents=False, exist_ok=False)
            self._copy_version_workspace(
                cid,
                live_workspace=live_workspace,
                dest_workspace=dest_workspace,
                live_hashes=live_hashes,
                previous=previous,
            )
            staged_facts = self._tree_facts(dest_workspace)
            _assert_staged_facts_match_record(staged_facts, record)
            store._write_json_atomic(
                staging_dir / store._VERSION_METADATA, _version_to_dict(record)
            )
            _fsync_tree(staging_dir)
            staging_dir.replace(version_dir)
            published = True
            _fsync_directory(versions)
            published_facts = self._tree_facts(version_dir / store._WORKSPACE)
            if published_facts != staged_facts:
                raise store.StorageError("published version bytes disagree with staged proof")
            self.write_version_index(cid, [*records, record])
            indexed = True
        except Exception:
            _discard_staging_dir(staging_dir)
            _discard_unindexed_version_dir(version_dir, published=published, indexed=indexed)
            raise
        finally:
            _discard_staging_dir(staging_dir)

        if prune:
            self._prune(cid)
        return record

    def cut_version_locked(
        self,
        cid: str,
        *,
        label: str = "",
        trigger: str,
        live_workspace: Path,
    ) -> VersionRecord | None:
        live_hashes, total_bytes = _scan_tree(live_workspace)
        digest = _tree_digest_from_hashes(live_hashes)
        records = self._reader.existing_version_records(cid)
        newest = records[-1] if records else None
        if newest is not None and newest.tree_digest == digest:
            return None
        return self.create_version(
            cid,
            label=label,
            trigger=trigger,
            live_workspace=live_workspace,
            live_hashes=live_hashes,
            total_bytes=total_bytes,
            records=records,
            previous=newest,
            pinned=False,
            prune=True,
        )

    def cut_verified_version_locked(
        self,
        cid: str,
        *,
        label: str = "",
        trigger: str,
        pin: bool = False,
        live_workspace: Path,
    ) -> VersionRecord | None:

        live_hashes, total_bytes = _scan_immutable_tree(live_workspace)
        digest = _tree_digest_from_hashes(live_hashes)
        records = self._reader.read_version_index(cid)
        reused = _reuse_verified_version_if_unchanged(self, cid, digest, pin, records)
        if reused is not None:
            return reused

        candidate = self.create_version(
            cid,
            label=label,
            trigger=trigger,
            live_workspace=live_workspace,
            live_hashes=live_hashes,
            total_bytes=total_bytes,
            records=records,
            previous=None,
            pinned=pin,
            prune=False,
        )
        candidate = self._reader.verify_version(cid, candidate.seq)
        self._prune(cid)
        return self._reader.verify_version(cid, candidate.seq)

    # -- prune ------------------------------------------------------------------

    def _versions_total_bytes(self, cid: str, records: list[VersionRecord]) -> int:
        return _count_versions_total_bytes(self._root, cid, records)

    def _drop_version(self, cid: str, record: VersionRecord) -> None:
        from disco.tools.projects import store

        version_dir = store._version_dir_path(self._root, cid, record)
        if version_dir.exists():
            shutil.rmtree(version_dir)

    def _prune(self, cid: str) -> None:
        from disco.tools.projects import store

        def drop(record: VersionRecord) -> None:
            self._drop_version(cid, record)

        def total_bytes(current_records: list[VersionRecord]) -> int:
            return self._versions_total_bytes(cid, current_records)

        records = self._reader.existing_version_records(cid)
        changed = len(records) != len(self._reader.read_version_index(cid))

        overflow_changed = _drop_unlabeled_overflow(
            records, max_unlabeled=store._MAX_UNLABELED, drop=drop
        )
        budget_changed = _drop_over_byte_budget(
            records,
            total_bytes=total_bytes,
            version_byte_budget=self._version_byte_budget,
            drop=drop,
        )
        changed = changed or overflow_changed or budget_changed

        if changed:
            self.write_version_index(cid, records)
