"""Pin/unpin WAL journal validation and crash-recovery decision logic.

Split out of `version_coordinator.py` (which was, with these included, over
the module logical-line budget) — a cohesive unit on its own: reading and
validating the durable journal record, proving the tree it points at is
still reachable, and determining which of the three durable views
(journal / metadata sidecar / index row) is ahead so recovery can roll the
lagging ones forward. `_VersionStateCoordinator._read_pin_journal` and
`._recover_pin_journal_if_pending` compose these; the decomposition (not mere
relocation) is what brought their cyclomatic complexity back under budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .version_serde import _payload_matches_record, _version_from_dict, _version_to_dict

if TYPE_CHECKING:
    from ..store import VersionRecord
    from .verified_reader import _VerifiedVersionReader


# ---------------------------------------------------------------------------
# `_read_pin_journal` decomposition
# ---------------------------------------------------------------------------


def read_pin_journal(root: Path, cid: str) -> tuple[VersionRecord, VersionRecord] | None:
    """Read the pin journal with full no-follow identity checks.

    Validates the canonical schema, conversation_id, that before/after are
    valid VersionRecords, and that they differ ONLY in the ``pinned`` bool.
    Returns ``None`` if no journal exists.  Raises ``StorageError`` for any
    malformed, tampered, or symlinked journal.

    Owned here rather than on `_VersionStateCoordinator`: reading and
    validating this module's own durable record is this module's authority,
    and the composition below is entirely of helpers defined in this file.
    The coordinator keeps a thin `_read_pin_journal` delegator because tests
    reach it through the instance.
    """
    from disco.tools.projects import store

    store._require_plain_project_dir(root, cid)
    raw = store._read_json_regular_nofollow(
        store._pin_journal_path(root, cid),
        label="pin journal",
        missing_ok=True,
    )
    if raw is store._MISSING_JSON:
        return None

    _validate_pin_journal_envelope(raw, cid)
    before_raw = raw.get("before")
    after_raw = raw.get("after")
    _validate_pin_journal_before_after_shape(before_raw, after_raw)
    return _coerce_and_verify_pin_journal_records(before_raw, after_raw)


def _validate_pin_journal_envelope(raw: object, cid: str) -> None:
    """Validate the pin journal's canonical top-level JSON shape."""
    from disco.tools.projects import store

    if not isinstance(raw, dict):
        raise store.StorageError("pin journal is malformed — expected a JSON object")
    if raw.keys() != {"schema_version", "conversation_id", "before", "after"}:
        raise store.StorageError("pin journal is malformed: non-canonical top-level fields")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
        raise store.StorageError("pin journal has unknown schema_version")
    if raw.get("conversation_id") != cid:
        raise store.StorageError("pin journal conversation_id mismatch")


def _validate_pin_journal_before_after_shape(before_raw: object, after_raw: object) -> None:
    """Validate before/after are dicts with the canonical VersionRecord key
    set and bool-typed ``pinned`` fields that actually differ."""
    from disco.tools.projects import store

    if not isinstance(before_raw, dict) or not isinstance(after_raw, dict):
        raise store.StorageError("pin journal is malformed — missing before/after version records")

    # Validate key sets and pinned types in the raw journal JSON before any
    # coercion so that malformed entries fail closed before recovery logic.
    expected_record_keys = _version_to_dict(
        store.VersionRecord(1, "", "", "", 0, 0, "0" * 64, False)
    ).keys()
    if before_raw.keys() != expected_record_keys or after_raw.keys() != expected_record_keys:
        raise store.StorageError(
            "pin journal is malformed: before/after have different shapes or "
            "non-canonical fields"
        )
    if before_raw.keys() != after_raw.keys():
        raise store.StorageError("pin journal before/after have different shapes")

    raw_before_pinned = before_raw.get("pinned")
    raw_after_pinned = after_raw.get("pinned")
    if type(raw_before_pinned) is not bool or type(raw_after_pinned) is not bool:
        raise store.StorageError("pin journal pinned fields are not bool type")
    if raw_before_pinned == raw_after_pinned:
        raise store.StorageError("pin journal before/after pinned values are identical — tampered")


def _coerce_and_verify_pin_journal_records(
    before_raw: dict[str, Any], after_raw: dict[str, Any]
) -> tuple[VersionRecord, VersionRecord]:
    """Parse before/after into VersionRecords and prove they differ ONLY in
    ``pinned`` — the last, semantic layer of pin-journal tamper detection."""
    from disco.tools.projects import store

    try:
        before = _version_from_dict(before_raw)
        after = _version_from_dict(after_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise store.StorageError(f"pin journal version record malformed: {exc}") from exc

    if not _payload_matches_record(before_raw, before) or not _payload_matches_record(
        after_raw, after
    ):
        raise store.StorageError("pin journal before/after records are non-canonical")
    before_dict = _version_to_dict(before)
    after_dict = _version_to_dict(after)
    differing = [key for key in before_dict if before_dict[key] != after_dict[key]]
    if differing != ["pinned"]:
        raise store.StorageError(
            f"pin journal before/after differ in non-pinned fields: {differing}"
        )

    return before, after


# ---------------------------------------------------------------------------
# `_recover_pin_journal_if_pending` decomposition
# ---------------------------------------------------------------------------


def _prove_pin_journal_target_tree(root: Path, cid: str, after: VersionRecord) -> None:
    """Prove the immutable tree the journal's ``after`` record points at is
    still reachable before any roll-forward. Deliberately does NOT call
    `verify_version_details` — that enforces metadata/index consistency,
    which is exactly the inconsistency being recovered from."""
    from disco.tools.projects import store

    try:
        store._require_plain_project_dir(root, cid)
        version_dir = store._version_dir_path(root, cid, after)
        workspace = version_dir / store._WORKSPACE
        if version_dir.is_symlink() or not version_dir.is_dir():
            raise store.StorageError("version directory missing or symlinked")
        if workspace.is_symlink() or not workspace.is_dir():
            raise store.StorageError("version workspace missing or symlinked")
        details, total_bytes = store._scan_immutable_tree_details(workspace)
        hashes = {rel: digest for rel, (digest, _size) in details.items()}
        if (
            len(details) != after.file_count
            or total_bytes != after.total_bytes
            or store._tree_digest_from_hashes(hashes) != after.tree_digest
        ):
            raise store.StorageError("version workspace disagrees with journal after record")
    except store.StorageError as exc:
        raise store.StorageError(
            f"pin journal recovery failed: tree verification failed: {exc}"
        ) from exc


def _pin_journal_recovery_state(
    root: Path,
    cid: str,
    reader: _VerifiedVersionReader,
    before: VersionRecord,
    after: VersionRecord,
) -> tuple[bool, bool, bool, bool, list[VersionRecord]]:
    """Determine whether the pin journal's metadata sidecar and index row
    currently reflect `before` or `after`, and return the current index."""
    from disco.tools.projects import store

    version_dir = store._version_dir_path(root, cid, after)
    metadata_path = version_dir / store._VERSION_METADATA
    raw_metadata = store._read_json_regular_nofollow(
        metadata_path,
        label="version metadata during pin recovery",
    )
    metadata_matches_after = False
    metadata_matches_before = False
    if isinstance(raw_metadata, dict):
        metadata_matches_after = _payload_matches_record(raw_metadata, after)
        metadata_matches_before = _payload_matches_record(raw_metadata, before)

    # Validate the complete index before any recovery write. Duplicate,
    # malformed, or non-canonical non-target rows make the state tampered,
    # not a partially applied transaction.
    records = reader.read_version_index(cid)
    target_record = next((record for record in records if record.seq == after.seq), None)
    index_matches_after = False
    index_matches_before = False
    if target_record is not None:
        index_matches_after = target_record == after
        index_matches_before = target_record == before

    return (
        metadata_matches_after,
        metadata_matches_before,
        index_matches_after,
        index_matches_before,
        records,
    )


def _replace_version_index_row(
    records: list[VersionRecord], after: VersionRecord
) -> list[VersionRecord]:
    """Roll a version index forward so its `after.seq` row reads `after`.
    Raises if that seq is absent — the row must already exist, it is only
    being brought up to date."""
    from disco.tools.projects import store

    next_records: list[VersionRecord] = []
    found = False
    for record in records:
        if record.seq == after.seq:
            next_records.append(after)
            found = True
        else:
            next_records.append(record)
    if not found:
        raise store.StorageError("pin journal recovery failed: version not found in index")
    return next_records


def _find_version_record(records: list[VersionRecord], seq: int) -> VersionRecord | None:
    for record in records:
        if record.seq == seq:
            return record
    return None
