"""Lock-scoped VerifiedVersionHandle contract tests (K6d).

Split from `test_project_version_proof.py` (module logical-line budget) —
purely mechanical: every test below moved verbatim, in original order.
Shared imports and the `_write`/`_version_dir` helpers live in
`_project_version_support.py` (a name pytest does not collect).
"""

from __future__ import annotations

import io
import os
import threading
import zipfile
from pathlib import Path

import pytest
from _project_version_support import _write
from disco.tools.projects.store import (
    ProjectStore,
    StorageError,
    VerifiedVersionHandle,
)


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
