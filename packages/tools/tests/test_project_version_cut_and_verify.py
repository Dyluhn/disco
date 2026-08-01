"""Core cut/verify/tamper-detection proof tests for immutable ProjectStore versions.

Split from `test_project_version_proof.py` (module logical-line budget) —
purely mechanical: every test below moved verbatim, in original order.
Shared imports and the `_write`/`_version_dir` helpers live in
`_project_version_support.py` (a name pytest does not collect).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _project_version_support import _version_dir, _write
from disco.tools.projects.store import (
    ProjectStore,
    StorageError,
    VersionRecord,
    WorkspaceTreeFacts,
)


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
