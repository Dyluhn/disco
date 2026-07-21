"""Fail-closed proof tests for immutable ProjectStore versions."""

from __future__ import annotations

import io
import json
import os
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from disco.tools.projects.store import (
    ProjectStore,
    StorageError,
    VerifiedVersionHandle,
    VersionRecord,
    WorkspaceTreeFacts,
)


def _write(workspace: Path, rel: str, data: bytes) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _version_dir(root: Path, cid: str, record: VersionRecord) -> Path:
    return root / cid / "versions" / f"{record.seq:03d}-{record.tree_digest[:12]}"


def test_cut_verified_version_pins_before_prune_and_reuses_exact_record(
    tmp_path: Path,
) -> None:
    store = ProjectStore(str(tmp_path), version_byte_budget=0)
    cid = "verified_pin"
    workspace = store.path_for(cid)
    _write(workspace, "app.txt", b"sealed bytes")

    facts = store.inspect_workspace(cid)
    record = store.cut_verified_version(cid, trigger="finish", pin=True)

    assert record is not None
    assert record.pinned is True
    assert facts == WorkspaceTreeFacts(
        file_count=record.file_count,
        total_bytes=record.total_bytes,
        tree_digest=record.tree_digest,
    )
    assert store.verify_version(cid, record.seq) == record
    assert _version_dir(tmp_path, cid, record).is_dir()

    reused = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert reused == record
    assert [item.seq for item in store.list_versions(cid)] == [record.seq]


def test_sequence_never_reuses_ids_removed_by_pruning(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path), version_byte_budget=0)
    cid = "monotonic_after_prune"
    workspace = store.path_for(cid)

    _write(workspace, "value.txt", b"one")
    first = store.cut_version(cid, trigger="auto")
    assert first is not None and first.seq == 1
    assert store.list_versions(cid) == []

    _write(workspace, "value.txt", b"two")
    second = store.cut_version(cid, trigger="auto")
    assert second is not None and second.seq == 2
    assert store.list_versions(cid) == []

    _write(workspace, "value.txt", b"sealed")
    third = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert third is not None and third.seq == 3
    state = json.loads((tmp_path / cid / "version-sequence.json").read_text())
    assert state["next_version_seq"] == 4
    assert state.get("schema_version") == 1


def test_missing_project_version_listing_and_delete_remain_idempotent(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    assert store.list_versions("missing_project") == []
    assert store.delete("missing_project") is False

    empty = store.path_for("project_without_versions")
    empty.mkdir(parents=True)
    assert store.list_versions("project_without_versions") == []


def test_concurrent_double_delete_returns_one_true_and_one_false(tmp_path: Path) -> None:
    cid = "double_delete"
    store = ProjectStore(str(tmp_path))
    _write(store.path_for(cid), "f.txt", b"data")
    start = threading.Barrier(2)

    def remove() -> bool:
        independent = ProjectStore(str(tmp_path))
        start.wait()
        return independent.delete(cid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(
            future.result(timeout=10) for future in [pool.submit(remove) for _ in range(2)]
        )
    assert results == [False, True]


def test_stale_sequence_state_fails_closed(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "stale_sequence"
    workspace = store.path_for(cid)
    _write(workspace, "value.txt", b"one")
    first = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert first is not None

    state = tmp_path / cid / "version-sequence.json"
    state.write_text(json.dumps({"next_version_seq": first.seq}))
    _write(workspace, "value.txt", b"two")

    with pytest.raises(StorageError, match="stale or tampered"):
        store.cut_verified_version(cid, trigger="finish", pin=True)


@pytest.mark.parametrize("tamper", ["tree", "metadata", "index", "prune"])
def test_verify_version_rejects_tamper_or_prune(tmp_path: Path, tamper: str) -> None:
    store = ProjectStore(str(tmp_path))
    cid = f"tamper_{tamper}"
    workspace = store.path_for(cid)
    _write(workspace, "app.txt", b"trusted")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    version_dir = _version_dir(tmp_path, cid, record)

    if tamper == "tree":
        (version_dir / "workspace" / "app.txt").write_bytes(b"changed")
    elif tamper == "metadata":
        metadata_path = version_dir / "version.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["total_bytes"] = record.total_bytes + 1
        metadata_path.write_text(json.dumps(metadata))
    elif tamper == "index":
        index_path = tmp_path / cid / "versions.json"
        index = json.loads(index_path.read_text())
        index[0]["file_count"] = record.file_count + 1
        index_path.write_text(json.dumps(index))
    else:
        shutil.rmtree(version_dir)

    with pytest.raises(StorageError):
        store.verify_version(cid, record.seq)


@pytest.mark.parametrize("symlink", ["entry", "workspace", "version"])
def test_verify_version_rejects_symlinked_version_surfaces(tmp_path: Path, symlink: str) -> None:
    store = ProjectStore(str(tmp_path))
    cid = f"symlink_{symlink}"
    workspace = store.path_for(cid)
    _write(workspace, "app.txt", b"trusted")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    version_dir = _version_dir(tmp_path, cid, record)

    if symlink == "entry":
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"not part of the version")
        (version_dir / "workspace" / "linked.txt").symlink_to(outside)
    elif symlink == "workspace":
        real_workspace = version_dir / "workspace-real"
        (version_dir / "workspace").replace(real_workspace)
        (version_dir / "workspace").symlink_to(real_workspace, target_is_directory=True)
    else:
        real_version = tmp_path / f"{cid}-real-version"
        version_dir.replace(real_version)
        version_dir.symlink_to(real_version, target_is_directory=True)

    with pytest.raises(StorageError, match="symlink"):
        store.verify_version(cid, record.seq)


def test_metadata_path_replacement_cannot_escape_fresh_version_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disco.tools.projects import store as store_module

    store = ProjectStore(str(tmp_path))
    cid = "metadata_path_replace"
    _write(store.path_for(cid), "app.txt", b"trusted")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    metadata = _version_dir(tmp_path, cid, record) / "version.json"
    external = tmp_path / "external-version.json"
    external.write_bytes(metadata.read_bytes())
    real_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal replaced
        if not replaced and Path(path) == metadata:
            metadata.unlink()
            metadata.symlink_to(external)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "open", replace_before_open)
    with pytest.raises(StorageError, match="symlinked|opened safely"):
        store.verify_version(cid, record.seq)
    assert replaced and metadata.is_symlink()


def test_staged_bytes_are_proved_before_index_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disco.tools.projects.store import _VersionStateCoordinator

    original_copy = _VersionStateCoordinator._copy_version_workspace

    def corrupting_copy(
        self: _VersionStateCoordinator,
        cid: str,
        *,
        live_workspace: Path,
        dest_workspace: Path,
        live_hashes: dict[str, str],
        previous: VersionRecord | None,
    ) -> None:
        original_copy(
            self,
            cid,
            live_workspace=live_workspace,
            dest_workspace=dest_workspace,
            live_hashes=live_hashes,
            previous=previous,
        )
        (dest_workspace / "app.txt").write_bytes(b"corrupted during copy")

    monkeypatch.setattr(_VersionStateCoordinator, "_copy_version_workspace", corrupting_copy)

    cid = "staged_proof"
    store = ProjectStore(str(tmp_path))
    workspace = store.path_for(cid)
    _write(workspace, "app.txt", b"source bytes")

    with pytest.raises(StorageError, match="staged version bytes"):
        store.cut_verified_version(cid, trigger="finish", pin=True)

    assert not (tmp_path / cid / "versions.json").exists()
    assert not [
        path for path in (tmp_path / cid / "versions").iterdir() if not path.name.startswith(".")
    ]

    monkeypatch.undo()

    # The failed reservation is deliberately a gap, never a reusable identity.
    healthy = ProjectStore(str(tmp_path))
    record = healthy.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None and record.seq == 2
    assert healthy.verify_version(cid, record.seq) == record


def test_inspect_workspace_rejects_reachable_symlink(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "inspect_symlink"
    workspace = store.path_for(cid)
    _write(workspace, "app.txt", b"source")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (workspace / "link.txt").symlink_to(outside)

    with pytest.raises(StorageError, match="symlink"):
        store.inspect_workspace(cid)


# ---------------------------------------------------------------------------
# Regression tests for confirmed defects (K6d)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Lock-scoped VerifiedVersionHandle contract (K6d)
# ---------------------------------------------------------------------------


def test_open_verified_version_read_bytes_returns_exact_bytes(
    tmp_path: Path,
) -> None:
    """Valid ``read_bytes`` on every manifest path returns the exact bytes
    that were committed during the verified cut."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_read_bytes"
    workspace = store.path_for(cid)
    _write(workspace, "alpha.txt", b"Hello, World!")
    _write(workspace, "nested/sub/deep.bin", b"\x00\x01\x02\xfe\xff")
    _write(workspace, "empty.dat", b"")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    with store.open_verified_version(cid, record.seq) as handle:
        assert handle.read_bytes("alpha.txt") == b"Hello, World!"
        assert handle.read_bytes("nested/sub/deep.bin") == b"\x00\x01\x02\xfe\xff"
        assert handle.read_bytes("empty.dat") == b""


def test_open_verified_version_rejects_unlisted_and_unsafe_paths(
    tmp_path: Path,
) -> None:
    """Paths that are not in the captured manifest, or that are unsafe
    (absolute, ``..``, null bytes), all raise ``StorageError``."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_unsafe_paths"
    _write(store.path_for(cid), "safe.txt", b"safe content")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    with store.open_verified_version(cid, record.seq) as handle:
        # Not in manifest
        with pytest.raises(StorageError, match="not in the verified version"):
            handle.read_bytes("nonexistent.txt")

        # Unsafe path forms
        with pytest.raises(StorageError, match="not a safe relative path|invalid"):
            handle.read_bytes("/etc/passwd")
        with pytest.raises(StorageError, match="not a safe relative path|invalid"):
            handle.read_bytes("../escape.txt")
        with pytest.raises(StorageError, match="not a safe relative path|invalid"):
            handle.read_bytes("sub/../safe.txt")
        with pytest.raises(StorageError, match="invalid"):
            handle.read_bytes("safe.txt\x00leak")


def test_open_verified_version_rejects_file_replaced_after_handle_open(
    tmp_path: Path,
) -> None:
    """Replacing a file on disk after the handle is created cannot return
    the changed bytes — the size check or the SHA-256 check must raise."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_replace"
    workspace = store.path_for(cid)
    _write(workspace, "mrk.txt", b"original content")  # 16 bytes
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    vw = store.version_workspace_path(cid, record.seq)

    with store.open_verified_version(cid, record.seq) as handle:
        (vw / "mrk.txt").write_bytes(b"different content")
        # The size differs (16 vs 18) so the size check fires first.
        with pytest.raises(StorageError, match="no longer the expected regular file|changed"):
            handle.read_bytes("mrk.txt")


def test_open_verified_version_added_file_not_in_handle_outputs(
    tmp_path: Path,
) -> None:
    """A file created in the version workspace directory *after* the handle
    is created must not appear in ``.files``, ``iter_bytes``, or ``zip_bytes``."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_added"
    workspace = store.path_for(cid)
    _write(workspace, "original.txt", b"original")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    vw = store.version_workspace_path(cid, record.seq)

    with store.open_verified_version(cid, record.seq) as handle:
        (vw / "added.txt").write_bytes(b"I should not be visible")

        # .files is a frozen snapshot
        assert not any(f.path == "added.txt" for f in handle.files), (
            "Added file leaked into handle.files"
        )

        # iter_bytes yields only manifest entries
        iter_paths = {entry.path for entry, _ in handle.iter_bytes()}
        assert "added.txt" not in iter_paths, "Added file leaked into iter_bytes"

        # zip_bytes contains only manifest entries
        zf = zipfile.ZipFile(io.BytesIO(handle.zip_bytes()))
        try:
            assert "added.txt" not in zf.namelist(), "Added file leaked into zip_bytes"
            assert zf.read("original.txt") == b"original"
        finally:
            zf.close()


def test_open_verified_version_zip_bytes_matches_manifest_exactly(
    tmp_path: Path,
) -> None:
    """``zip_bytes`` returns a ZIP archive that contains every manifest file
    with the exact captured bytes and nothing else."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_zip_exact"
    workspace = store.path_for(cid)
    payloads = {
        "a.txt": b"alpha content",
        "nested/b.dat": b"\xde\xad\xbe\xef",
        "c.yaml": b"key: value\n",
    }
    for rel, data in payloads.items():
        _write(workspace, rel, data)
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    with store.open_verified_version(cid, record.seq) as handle:
        zip_data = handle.zip_bytes()

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        assert set(zf.namelist()) == set(payloads), (
            f"ZIP paths {set(zf.namelist())} do not match manifest {set(payloads)}"
        )
        for rel, expected in payloads.items():
            actual = zf.read(rel)
            assert actual == expected, (
                f"ZIP content mismatch for {rel}: got {len(actual)}b, expected {len(expected)}b"
            )


def test_open_verified_version_closed_handle_raises(
    tmp_path: Path,
) -> None:
    """Any operation on a closed handle raises ``StorageError``."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_closed"
    _write(store.path_for(cid), "data.txt", b"inaccessible")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    handle: VerifiedVersionHandle | None = None
    with store.open_verified_version(cid, record.seq) as h:
        handle = h
    assert handle is not None
    # Handle is now closed (context manager exit called handle.close())
    with pytest.raises(StorageError, match="closed"):
        handle.read_bytes("data.txt")


def test_open_verified_version_rejects_symlink_after_handle_created(
    tmp_path: Path,
) -> None:
    """A regular file replaced with a symlink (or a special file) after the
    handle is created must be rejected — the O_NOFOLLOW open or the S_ISREG
    check prevents reading through an unexpected entry type."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_symlink"
    workspace = store.path_for(cid)
    _write(workspace, "target.txt", b"original")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    vw = store.version_workspace_path(cid, record.seq)

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside data")

    with store.open_verified_version(cid, record.seq) as handle:
        # Replace the regular file with a symlink
        (vw / "target.txt").unlink()
        (vw / "target.txt").symlink_to(outside)

        with pytest.raises(StorageError, match="changed|no longer the expected"):
            handle.read_bytes("target.txt")


def test_open_verified_version_rejects_fifo_after_handle_created(
    tmp_path: Path,
) -> None:
    """A FIFO replacement is rejected without blocking for a writer."""
    store = ProjectStore(str(tmp_path))
    cid = "handle_fifo"
    _write(store.path_for(cid), "target.txt", b"original")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None
    vw = store.version_workspace_path(cid, record.seq)

    with store.open_verified_version(cid, record.seq) as handle:
        (vw / "target.txt").unlink()
        os.mkfifo(vw / "target.txt")

        with pytest.raises(StorageError, match="no longer the expected regular file|changed"):
            handle.read_bytes("target.txt")
        (vw / "target.txt").unlink()


def test_open_verified_version_shared_handle_blocks_exclusive(
    tmp_path: Path,
) -> None:
    """A shared handle acquired via ``open_verified_version`` (which holds a
    shared ``_version_transaction``) blocks any exclusive transaction until
    the handle is released, without relying on sleep-based ordering."""
    store = ProjectStore(str(tmp_path))
    cid = "shared_block_excl"
    _write(store.path_for(cid), "f.txt", b"data")
    record = store.cut_verified_version(cid, trigger="finish", pin=True)
    assert record is not None

    handle_held = threading.Event()
    waiter_attempting = threading.Event()
    release_handle = threading.Event()
    exclusive_acquired = threading.Event()

    def holder() -> None:
        with store.open_verified_version(cid, record.seq):
            handle_held.set()
            if not waiter_attempting.wait(timeout=10):
                return
            release_handle.wait(timeout=10)

    def excl_waiter() -> None:
        if not handle_held.wait(timeout=10):
            return
        waiter_attempting.set()
        with store._version_transaction(cid, exclusive=True):
            exclusive_acquired.set()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=excl_waiter)
    t1.start()
    t2.start()
    assert handle_held.wait(timeout=10)
    assert waiter_attempting.wait(timeout=10)
    assert not exclusive_acquired.is_set(), "exclusive lock entered while handle was held"
    release_handle.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive(), "holder thread did not finish"
    assert not t2.is_alive(), "excl_waiter thread did not finish"

    assert exclusive_acquired.is_set(), "exclusive waiter never acquired after release"


# ---------------------------------------------------------------------------
# Pin/unpin WAL journal crash recovery — fault-injection tests
# ---------------------------------------------------------------------------


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
