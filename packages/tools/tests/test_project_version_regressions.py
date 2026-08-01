"""Regression tests for confirmed defects (K6d) in immutable ProjectStore versions.

Split from `test_project_version_proof.py` (module logical-line budget) —
purely mechanical: every test below moved verbatim, in original order.
Shared imports and the `_write`/`_version_dir` helpers live in
`_project_version_support.py` (a name pytest does not collect).
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _project_version_support import _write
from disco.tools.projects.store import (
    ProjectStore,
    StorageError,
)


def test_pathname_replacement_during_immutable_hash_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file replaced at the same filesystem path between the lstat() check
    and the os.open() call inside _scan_immutable_tree must raise StorageError.

    This guards against a TOCTOU race where pathname replacement swaps the
    file (different inode / content) after the entry's type is verified but
    before the O_NOFOLLOW fd is opened for hashing.
    """
    store = ProjectStore(str(tmp_path))
    cid = "pathname_replace"
    workspace = store.path_for(cid)
    _write(workspace, "alpha.txt", b"original alpha")
    _write(workspace, "beta.txt", b"original beta")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    vw = store.version_workspace_path(cid, record.seq)
    alpha = vw / "alpha.txt"
    real_open = os.open
    swapped = False

    def replace_between_lstat_and_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal swapped
        if not swapped and isinstance(path, (str, os.PathLike)) and Path(path) == alpha:
            replacement = alpha.with_name("replacement.tmp")
            replacement.write_bytes(b"replaced alpha")
            replacement.replace(alpha)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_between_lstat_and_open)

    # _scan_immutable_tree — called by verify_version — must raise when
    # the inode behind the path does not match what lstat() reported before
    # the fd was opened.
    with pytest.raises(StorageError, match="changed before hashing|workspace disagrees"):
        store.verify_version(cid, record.seq)
    assert swapped is True


def test_versions_ancestor_symlink_rejected(tmp_path: Path) -> None:
    """Replacing the versions ancestor directory with a symlink must fail
    closed — Store operations MUST NOT traverse through a symlinked
    conversation directory that points outside the projects root."""
    store = ProjectStore(str(tmp_path))
    cid = "ancestor_symlink"
    workspace = store.path_for(cid)
    _write(workspace, "app.txt", b"sealed")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    versions = tmp_path / cid / "versions"
    external = tmp_path / f"{cid}_real_versions"
    versions.rename(external)
    versions.symlink_to(external, target_is_directory=True)

    # Any operation that touches the version ancestry must reject the symlink.
    with pytest.raises(StorageError, match="missing or symlinked|symlink"):
        store.verify_version(cid, record.seq)
    with pytest.raises(StorageError, match="missing or symlinked|symlink"):
        store.list_versions(cid)


def test_planted_json_temp_symlink_not_overwritten(tmp_path: Path) -> None:
    """A symlink planted at a JSON target path (e.g. versions.json → victim)
    must NOT overwrite its victim when _write_json_atomic runs, and the
    planted symlink must not survive the write (it is replaced by the
    new regular JSON file)."""
    from disco.tools.projects.store import _write_json_atomic

    victim = tmp_path / "victim.txt"
    victim.write_text("victim content")

    target = tmp_path / "versions.json"
    target.symlink_to(victim)

    _write_json_atomic(target, {"seq": 1, "pinned": False})

    # Victim must remain intact (os.replace replaces the directory entry,
    # not the symlink target).
    assert victim.read_text() == "victim content"

    # The target must now be a regular file containing the JSON payload.
    assert target.is_file()
    assert not target.is_symlink()
    assert json.loads(target.read_text()) == {"seq": 1, "pinned": False}


def test_predictable_legacy_temp_symlink_cannot_redirect_json_write(tmp_path: Path) -> None:
    """A symlink planted at a predictable temp JSON path must not survive
    the atomic write — mkstemp uses O_EXCL so a pre-existing symlink causes
    the call to fail rather than follow the symlink."""
    from disco.tools.projects.store import _write_json_atomic

    target = tmp_path / "version-sequence.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("victim data")
    legacy_tmp = target.with_suffix(target.suffix + ".tmp")
    legacy_tmp.symlink_to(victim)

    # _write_json_atomic must replace the symlink without modifying victim.
    _write_json_atomic(target, {"safe": True})
    assert victim.read_text() == "victim data"
    assert json.loads(target.read_text()) == {"safe": True}
    assert not legacy_tmp.exists()


def test_independent_store_instances_dont_clobber_sequence(tmp_path: Path) -> None:
    """Two independent ProjectStore instances (or concurrent calls) must not
    publish the same version sequence number or silently drop an index row.

    Each worker starts together, but writes and cuts its distinct tree inside
    the same transaction used by the public cut methods. Both the allocator and
    index publication must therefore remain serialized across store instances.
    """
    root = str(tmp_path)
    cid = "race_seq"
    num_threads = 6
    store = ProjectStore(root)
    workspace = store.path_for(cid)
    workspace.mkdir(parents=True)
    start = threading.Barrier(num_threads)

    def cut(i: int) -> int:
        independent = ProjectStore(root)
        start.wait()
        # The mutation is deliberately inside the same storage transaction as
        # the cut. Each independent instance therefore publishes a distinct,
        # stable tree while all other instances wait on the OS/process lock.
        with independent._version_transaction(cid, exclusive=True):
            _write(workspace, "value.txt", f"thread-{i}".encode())
            record = independent._cut_verified_version_locked(
                cid,
                trigger="finish",
                pin=True,
            )
        assert record is not None
        return record.seq

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(cut, i) for i in range(num_threads)]
        seqs = [future.result(timeout=10) for future in futures]

    # All seqs and index rows are unique because the transaction serializes the
    # complete mutation/allocation/publication boundary.
    assert len(set(seqs)) == len(seqs), (
        f"Duplicate sequence numbers ({len(seqs)} cuts → "
        f"{len(set(seqs))} unique): {seqs}. "
        "Cross-process lock (_version_transaction) is not protecting "
        "_reserve_version_seq."
    )
    records = store.list_versions(cid)
    assert {record.seq for record in records} == set(seqs)


def test_allocator_state_deleted_after_prune_fails_closed(tmp_path: Path) -> None:
    """Deleting allocator state after all versions have been
    pruned must fail closed (StorageError) rather than silently resetting
    the sequence counter and reusing version identities."""
    store = ProjectStore(str(tmp_path), version_byte_budget=0)
    cid = "alloc_deleted"
    workspace = store.path_for(cid)
    _write(workspace, "a.txt", b"first")
    v1 = store.cut_version(cid, trigger="auto")
    assert v1 is not None and v1.seq == 1
    assert store.list_versions(cid) == []
    state = tmp_path / cid / "version-sequence.json"
    marker = tmp_path / cid / "version-allocator-v1.json"
    assert state.is_file() and marker.is_file()
    state.unlink()

    _write(workspace, "b.txt", b"second")

    # After fix: the store refuses to reuse seq 1 and raises StorageError.
    # Before fix: the store falls through to seq = high_watermark + 1 = 1
    # and silently reuses the sequence identity.
    with pytest.raises(StorageError, match="stale|sequence|allocator|tampered|unsafe"):
        store.cut_verified_version(cid, trigger="finish", pin=True)


def test_first_allocator_migration_recovers_if_marker_write_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State-first migration may leave a safe sequence gap, never a poison marker."""

    import disco.tools.projects.store as store_module

    store = ProjectStore(str(tmp_path))
    cid = "allocator_marker_crash"
    _write(store.path_for(cid), "app.txt", b"first attempt")
    real_write = store_module._write_json_atomic
    failed = False

    def fail_first_marker(path: Path, payload: object) -> None:
        nonlocal failed
        if path.name == "version-allocator-v1.json" and not failed:
            failed = True
            raise OSError("injected crash before allocator marker publication")
        real_write(path, payload)

    monkeypatch.setattr(store_module, "_write_json_atomic", fail_first_marker)
    with pytest.raises(OSError, match="injected crash"):
        store.cut_verified_version(cid, trigger="finish", pin=True)

    project = tmp_path / cid
    assert (project / "version-sequence.json").is_file()
    assert not (project / "version-allocator-v1.json").exists()

    recovered = ProjectStore(str(tmp_path)).cut_verified_version(
        cid,
        trigger="finish",
        pin=True,
    )
    assert recovered is not None and recovered.seq == 2
    assert (project / "version-allocator-v1.json").is_file()


def test_immutable_version_no_hardlinked_inode_sharing(tmp_path: Path) -> None:
    """Immutable versions must not share hardlinked inodes.  When
    _copy_version_workspace uses os.link for unchanged files, writing
    through the copy in one version would alter the content visible
    through a pinned newer (or older) version."""
    store = ProjectStore(str(tmp_path))
    cid = "hardlink_inode"
    workspace = store.path_for(cid)

    _write(workspace, "shared.txt", b"content preserved across versions")
    v1 = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert v1 is not None

    _write(workspace, "unique.txt", b"unique to v2")
    v2 = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert v2 is not None and v2.seq == 2

    v1_workspace = store.version_workspace_path(cid, v1.seq)
    v2_workspace = store.version_workspace_path(cid, v2.seq)

    v1_ino = (v1_workspace / "shared.txt").stat().st_ino
    v2_ino = (v2_workspace / "shared.txt").stat().st_ino

    # After fix: each version owns an independent copy (different inode).
    # Before fix: os.link creates a hardlink (same inode), so a write
    # through one version's file mutates the other's.
    assert v1_ino != v2_ino, (
        "Versions share a hardlinked inode; writing through one version's "
        "workspace would corrupt the other version's content."
    )


def test_unsupported_cross_process_primitive_fails_by_name(tmp_path: Path) -> None:
    """When POSIX flock (fcntl) is unavailable, the cross-process proof
    context manager must raise StorageError with an error message that
    names the missing primitive."""
    from disco.tools.projects.store import _fcntl

    store = ProjectStore(str(tmp_path))
    cid = "cross_proc_proof"
    workspace = store.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)

    if _fcntl is None:
        with pytest.raises(StorageError, match="flock|unsupported"):
            with store._version_transaction(cid, exclusive=True):
                pass
    else:
        # On platforms with fcntl, verify the transaction acquires and
        # releases without error; the failure contract is the message
        # pattern shown when fcntl is absent.
        with store._version_transaction(cid, exclusive=False):
            pass
        with store._version_transaction(cid, exclusive=True):
            pass

        # Verify mutual exclusion: two exclusive acquisitions from
        # different threads must be serialized.
        entered: list[int] = []
        errors: list[Exception] = []
        first_acquired = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_acquired = threading.Event()

        def acquire(who: int) -> None:
            try:
                if who == 2:
                    first_acquired.wait(timeout=10)
                    second_attempting.set()
                with store._version_transaction(cid, exclusive=True):
                    entered.append(who)
                    if who == 1:
                        first_acquired.set()
                        release_first.wait(timeout=10)
                    else:
                        second_acquired.set()
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=acquire, args=(1,))
        t2 = threading.Thread(target=acquire, args=(2,))
        t1.start()
        t2.start()
        assert first_acquired.wait(timeout=10)
        assert second_attempting.wait(timeout=10)
        assert not second_acquired.is_set()
        release_first.set()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Unexpected errors during lock test: {errors}"
        assert not t1.is_alive() and not t2.is_alive()
        # t1 should have entered before t2 (mutual exclusion via flock)
        assert entered == [1, 2], (
            f"Expected sequential entry [1, 2] but got {entered}; "
            "cross-process lock may not be serializing"
        )
