"""Pin/unpin WAL journal crash recovery — fault-injection tests.

Split from `test_project_version_proof.py` (module logical-line budget) —
purely mechanical: every test below moved verbatim, in original order.
Shared imports and the `_write`/`_version_dir` helpers live in
`_project_version_support.py` (a name pytest does not collect).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from _project_version_support import _write
from disco.tools.projects.store import (
    ProjectStore,
    StorageError,
)


def test_noop_pin_returns_verified_without_journal(tmp_path: Path) -> None:
    """Pinning an already-pinned version is a no-op that creates no journal."""
    store = ProjectStore(str(tmp_path))
    cid = "noop_pin"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None and record.pinned is True

    journal = tmp_path / cid / ".versions.pin.journal"
    assert not journal.exists()

    result = store.set_version_pinned(cid, record.seq, True)
    assert result == record
    assert not journal.exists()


def test_noop_unpin_returns_verified_without_journal(tmp_path: Path) -> None:
    """Unpinning an already-unpinned version is a no-op with no journal."""
    store = ProjectStore(str(tmp_path))
    cid = "noop_unpin"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None and record.pinned is False

    journal = tmp_path / cid / ".versions.pin.journal"
    assert not journal.exists()

    result = store.set_version_pinned(cid, record.seq, False)
    assert result == record
    assert not journal.exists()


def test_pin_journal_crash_before_metadata_recovery(tmp_path: Path) -> None:
    """Journal written, crash before metadata — recovery returns before state."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "crash_before_meta"
    _write(store.path_for(cid), "f.txt", b"crash data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None and record.pinned is False

    pinned = replace(record, pinned=True)
    journal_payload = {
        "schema_version": 1,
        "conversation_id": cid,
        "before": _version_to_dict(record),
        "after": _version_to_dict(pinned),
    }
    _write_json_atomic(_pin_journal_path(tmp_path, cid), journal_payload)

    recovered = ProjectStore(str(tmp_path))
    result = recovered.set_version_pinned(cid, record.seq, True)
    assert result is not None
    assert result.pinned is True
    assert not _pin_journal_path(tmp_path, cid).exists()


def test_pin_journal_crash_before_index_recovery(tmp_path: Path) -> None:
    """Journal + metadata written, crash before index — roll-forward succeeds."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_dir_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "crash_before_idx"
    _write(store.path_for(cid), "f.txt", b"crash data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None and record.pinned is False

    pinned = replace(record, pinned=True)
    journal_payload = {
        "schema_version": 1,
        "conversation_id": cid,
        "before": _version_to_dict(record),
        "after": _version_to_dict(pinned),
    }
    _write_json_atomic(_pin_journal_path(tmp_path, cid), journal_payload)
    _write_json_atomic(
        _version_dir_path(tmp_path, cid, record) / "version.json",
        _version_to_dict(pinned),
    )

    recovered = ProjectStore(str(tmp_path))
    result = recovered.set_version_pinned(cid, record.seq, True)
    assert result is not None
    assert result.pinned is True
    assert not _pin_journal_path(tmp_path, cid).exists()


def test_pin_journal_recovers_a_nonfirst_index_row(tmp_path: Path) -> None:
    """Recovery must inspect the journal's row, not merely the first index row."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_dir_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "recover_second_row"
    workspace = store.path_for(cid)
    _write(workspace, "f.txt", b"first")
    first = store.cut_verified_version(cid, trigger="finish", pin=True)
    _write(workspace, "f.txt", b"second")
    second = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert first is not None and second is not None and second.seq > first.seq

    pinned = replace(second, pinned=True)
    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(second),
            "after": _version_to_dict(pinned),
        },
    )
    _write_json_atomic(
        _version_dir_path(tmp_path, cid, second) / "version.json",
        _version_to_dict(pinned),
    )

    recovered = ProjectStore(str(tmp_path)).set_version_pinned(cid, second.seq, True)
    assert recovered == pinned
    assert not _pin_journal_path(tmp_path, cid).exists()


def test_pin_journal_crash_after_index_cleanup(tmp_path: Path) -> None:
    """All three (journal, metadata, index) written — recovery just cleans up."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_dir_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "crash_after_idx"
    _write(store.path_for(cid), "f.txt", b"crash data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None and record.pinned is False

    pinned = replace(record, pinned=True)
    journal_payload = {
        "schema_version": 1,
        "conversation_id": cid,
        "before": _version_to_dict(record),
        "after": _version_to_dict(pinned),
    }
    _write_json_atomic(_pin_journal_path(tmp_path, cid), journal_payload)
    _write_json_atomic(
        _version_dir_path(tmp_path, cid, record) / "version.json",
        _version_to_dict(pinned),
    )
    # Write index with pinned=True
    _write_json_atomic(
        tmp_path / cid / "versions.json",
        [_version_to_dict(pinned)],
    )

    recovered = ProjectStore(str(tmp_path))
    # Shared readers fail closed until an exclusive transaction verifies and
    # retires the durable intent, even when both data files reached `after`.
    with pytest.raises(StorageError, match="pending pin journal"):
        recovered.verify_version(cid, record.seq)
    assert _pin_journal_path(tmp_path, cid).exists()

    # Any EX transaction triggers recovery
    recovered.set_version_pinned(cid, record.seq, True)
    assert not _pin_journal_path(tmp_path, cid).exists()
    final = recovered.verify_version(cid, record.seq)
    assert final.pinned is True


@pytest.mark.parametrize("crash_phase", ["metadata", "index", "verify"])
def test_production_pin_transaction_replays_after_each_crash_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    """Fault the real journal→metadata→index→verify path and replay its intent."""
    from disco.tools.projects import store as store_module

    store = ProjectStore(str(tmp_path))
    cid = f"production_crash_{crash_phase}"
    _write(store.path_for(cid), "f.txt", b"durable intent")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None
    journal_path = store_module._pin_journal_path(tmp_path, cid)

    real_write = store_module._write_json_atomic
    real_verify = store_module._VerifiedVersionReader.verify_version_details
    failed = False

    def faulting_write(path: Path, payload: object) -> None:
        nonlocal failed
        should_fail = (
            not failed
            and journal_path.exists()
            and (
                (crash_phase == "metadata" and path.name == "version.json")
                or (crash_phase == "index" and path.name == "versions.json")
            )
        )
        if should_fail:
            failed = True
            raise RuntimeError(f"injected {crash_phase} crash")
        real_write(path, payload)

    def faulting_verify(self, conversation_id: str, seq: int):  # noqa: ANN001, ANN202
        nonlocal failed
        if crash_phase == "verify" and journal_path.exists() and not failed:
            failed = True
            raise RuntimeError("injected verify crash")
        return real_verify(self, conversation_id, seq)

    monkeypatch.setattr(store_module, "_write_json_atomic", faulting_write)
    monkeypatch.setattr(
        store_module._VerifiedVersionReader,
        "verify_version_details",
        faulting_verify,
    )
    with pytest.raises(RuntimeError, match="injected"):
        store.set_version_pinned(cid, record.seq, True)
    assert failed and journal_path.is_file()

    # Every shared reader rejects the mixed or not-yet-retired state.
    for read in (
        lambda: ProjectStore(str(tmp_path)).verify_version(cid, record.seq),
        lambda: ProjectStore(str(tmp_path)).list_versions(cid),
    ):
        with pytest.raises(StorageError, match="pending pin journal"):
            read()

    monkeypatch.undo()
    recovered = ProjectStore(str(tmp_path))
    # An unrelated next exclusive transaction performs recovery; callers do
    # not need to recursively reacquire the lock or repeat the same command.
    with recovered._version_transaction(cid, exclusive=True):
        pass
    assert recovered.verify_version(cid, record.seq).pinned is True
    assert not journal_path.exists()


def test_pin_journal_idempotent_recovery(tmp_path: Path) -> None:
    """Recovering the same journal twice (or through any EX transaction) is safe."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_dir_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "idempotent_recov"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    pinned = replace(record, pinned=True)
    journal_payload = {
        "schema_version": 1,
        "conversation_id": cid,
        "before": _version_to_dict(record),
        "after": _version_to_dict(pinned),
    }
    _write_json_atomic(_pin_journal_path(tmp_path, cid), journal_payload)
    _write_json_atomic(
        _version_dir_path(tmp_path, cid, record) / "version.json",
        _version_to_dict(pinned),
    )

    recovered = ProjectStore(str(tmp_path))
    result = recovered.set_version_pinned(cid, record.seq, True)
    assert result.pinned is True
    assert not _pin_journal_path(tmp_path, cid).exists()

    # A second EX transaction (cut_version) is also a no-op for the journal
    _write(recovered.path_for(cid), "g.txt", b"new")
    recovered.cut_version(cid, trigger="auto")
    assert not _pin_journal_path(tmp_path, cid).exists()


def test_unpin_journal_crash_recovery(tmp_path: Path) -> None:
    """Unpin (pinned→unpinned) crash recovery works identically in the
    before/before state."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "unpin_crash"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None and record.pinned is True

    unpinned = replace(record, pinned=False)
    journal_payload = {
        "schema_version": 1,
        "conversation_id": cid,
        "before": _version_to_dict(record),
        "after": _version_to_dict(unpinned),
    }
    _write_json_atomic(_pin_journal_path(tmp_path, cid), journal_payload)

    recovered = ProjectStore(str(tmp_path))
    result = recovered.set_version_pinned(cid, record.seq, False)
    assert result.pinned is False
    assert not _pin_journal_path(tmp_path, cid).exists()


def test_pin_journal_malformed_schema_version_rejected(tmp_path: Path) -> None:
    """A journal with an unsupported schema_version is rejected."""
    from disco.tools.projects.store import _pin_journal_path, _write_json_atomic

    store = ProjectStore(str(tmp_path))
    cid = "bad_schema"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {"schema_version": 99, "conversation_id": cid, "before": {}, "after": {}},
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="schema_version"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_identical_before_after_rejected(tmp_path: Path) -> None:
    """A journal with before == after is tampered and must be rejected."""
    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "identical_journal"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    d = _version_to_dict(record)
    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": d,
            "after": d,
        },
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="identical"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_non_pinned_diff_rejected(tmp_path: Path) -> None:
    """A journal where before/after differ in fields other than pinned is
    tampered and must be rejected."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "non_pinned_diff"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    tampered = replace(record, pinned=True, label="fake label")
    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(record),
            "after": _version_to_dict(tampered),
        },
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="non-pinned"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_symlinked_rejected(tmp_path: Path) -> None:
    """A symlinked journal file must be rejected."""
    from disco.tools.projects.store import _pin_journal_path

    store = ProjectStore(str(tmp_path))
    cid = "symlink_journal"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    journal = _pin_journal_path(tmp_path, cid)
    outside = tmp_path / "outside.txt"
    outside.write_text("not a journal")
    journal.symlink_to(outside)

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="symlinked"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_wrong_conversation_rejected(tmp_path: Path) -> None:
    """A journal with a mismatched conversation_id is rejected."""
    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "wrong_cid"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": "different-cid",
            "before": _version_to_dict(record),
            "after": _version_to_dict(record),
        },
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="conversation_id"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_missing_before_after_rejected(tmp_path: Path) -> None:
    """A journal without before/after keys is malformed and must be rejected."""
    from disco.tools.projects.store import _pin_journal_path, _write_json_atomic

    store = ProjectStore(str(tmp_path))
    cid = "missing_before"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {"schema_version": 1, "conversation_id": cid},
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="malformed"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_reversed_state_fails_closed(tmp_path: Path) -> None:
    """before/after in the journal are reversed (metadata=before, index=after) —
    this is an impossible state and must fail closed."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "reversed_state"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    pinned = replace(record, pinned=True)
    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(record),
            "after": _version_to_dict(pinned),
        },
    )
    # Write index with after (pinned) but metadata with before (unpinned)
    # This is the impossible ordering: index=after, metadata=before
    _write_json_atomic(
        tmp_path / cid / "versions.json",
        [_version_to_dict(pinned)],
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="impossible"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_duplicate_index_fails_before_any_recovery_write(tmp_path: Path) -> None:
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_dir_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "duplicate_index_recovery"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None
    after = replace(record, pinned=True)
    journal = _pin_journal_path(tmp_path, cid)
    metadata = _version_dir_path(tmp_path, cid, record) / "version.json"
    before_metadata = metadata.read_bytes()
    _write_json_atomic(
        journal,
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(record),
            "after": _version_to_dict(after),
        },
    )
    _write_json_atomic(
        tmp_path / cid / "versions.json",
        [_version_to_dict(record), _version_to_dict(record)],
    )

    with pytest.raises(StorageError, match="duplicate"):
        ProjectStore(str(tmp_path)).set_version_pinned(cid, record.seq, True)
    assert metadata.read_bytes() == before_metadata
    assert journal.is_file()


def test_pin_journal_not_regular_file_rejected(tmp_path: Path) -> None:
    """A journal that is a directory (not a regular file) is rejected."""
    from disco.tools.projects.store import _pin_journal_path

    store = ProjectStore(str(tmp_path))
    cid = "dir_journal"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    _pin_journal_path(tmp_path, cid).mkdir()

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="not a regular file"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_recovery_proves_immutable_tree(tmp_path: Path) -> None:
    """Recovery must prove the immutable tree before rolling forward — if the
    version was pruned, recovery fails closed."""
    from dataclasses import replace

    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_dir_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "pruned_tree"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    pinned = replace(record, pinned=True)
    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(record),
            "after": _version_to_dict(pinned),
        },
    )
    _write_json_atomic(
        _version_dir_path(tmp_path, cid, record) / "version.json",
        _version_to_dict(pinned),
    )

    # Prune the version tree directory
    version_dir = _version_dir_path(tmp_path, cid, record)
    shutil.rmtree(version_dir)

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="tree verification failed"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_successful_pin_cleans_up_journal(tmp_path: Path) -> None:
    """After a successful pin, no journal remains on disk."""
    from disco.tools.projects.store import _pin_journal_path

    store = ProjectStore(str(tmp_path))
    cid = "clean_pin"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None and record.pinned is False

    assert not _pin_journal_path(tmp_path, cid).exists()
    result = store.set_version_pinned(cid, record.seq, True)
    assert result.pinned is True
    assert not _pin_journal_path(tmp_path, cid).exists()

    # Unpin also cleans up
    result2 = store.set_version_pinned(cid, record.seq, False)
    assert result2.pinned is False
    assert not _pin_journal_path(tmp_path, cid).exists()


def test_pin_journal_crash_recovery_validates_pinned_bool_type(tmp_path: Path) -> None:
    """A journal with pinned as non-bool (e.g. 1 instead of True) is rejected."""
    from disco.tools.projects.store import _pin_journal_path, _write_json_atomic

    store = ProjectStore(str(tmp_path))
    cid = "non_bool_pin"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": {
                "seq": record.seq,
                "ts": record.ts,
                "label": "",
                "trigger": "",
                "file_count": 1,
                "total_bytes": 4,
                "tree_digest": record.tree_digest,
                "pinned": False,
            },
            "after": {
                "seq": record.seq,
                "ts": record.ts,
                "label": "",
                "trigger": "",
                "file_count": 1,
                "total_bytes": 4,
                "tree_digest": record.tree_digest,
                "pinned": 1,
            },
        },
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="not bool"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_malformed_version_record_rejected(tmp_path: Path) -> None:
    """A journal with a malformed version record (missing seq) is rejected."""
    from disco.tools.projects.store import _pin_journal_path, _write_json_atomic

    store = ProjectStore(str(tmp_path))
    cid = "malformed_record"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": {"pinned": False},
            "after": {"pinned": True},
        },
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="malformed"):
        recovered.set_version_pinned(cid, record.seq, True)


def test_pin_journal_json_null_is_not_mistaken_for_missing(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "null_journal"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None
    journal = tmp_path / cid / ".versions.pin.journal"
    journal.write_text("null", encoding="utf-8")

    with pytest.raises(StorageError, match="malformed"):
        ProjectStore(str(tmp_path)).set_version_pinned(cid, record.seq, True)
    assert journal.read_text(encoding="utf-8") == "null"


def test_pin_journal_different_before_after_key_sets_rejected(tmp_path: Path) -> None:
    """A journal where before and after have different key sets is tampered."""
    from disco.tools.projects.store import (
        _pin_journal_path,
        _version_to_dict,
        _write_json_atomic,
    )

    store = ProjectStore(str(tmp_path))
    cid = "diff_keys"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=False)
    assert record is not None

    after_dict = _version_to_dict(record)
    del after_dict["label"]
    _write_json_atomic(
        _pin_journal_path(tmp_path, cid),
        {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(record),
            "after": after_dict,
        },
    )

    recovered = ProjectStore(str(tmp_path))
    with pytest.raises(StorageError, match="different shapes"):
        recovered.set_version_pinned(cid, record.seq, True)
