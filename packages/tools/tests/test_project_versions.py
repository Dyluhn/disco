"""Versioned ProjectStore snapshots: pure disk tests, no sandbox required."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.tools.projects.store import ProjectStore, StorageError, tree_digest


def _write(workspace: Path, rel: str, data: bytes) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _index(root: Path, cid: str) -> list[dict[str, object]]:
    raw = json.loads((root / cid / "versions.json").read_text())
    assert isinstance(raw, list)
    return raw


def _version_workspace(root: Path, cid: str, seq: int, digest: str) -> Path:
    return root / cid / "versions" / f"{seq:03d}-{digest[:12]}" / "workspace"


def _set_pinned(root: Path, cid: str, seq: int) -> None:
    index_path = root / cid / "versions.json"
    rows = _index(root, cid)
    digest = ""
    for row in rows:
        if row["seq"] == seq:
            row["pinned"] = True
            digest = str(row["tree_digest"])
    index_path.write_text(json.dumps(rows, indent=2))

    metadata = root / cid / "versions" / f"{seq:03d}-{digest[:12]}" / "version.json"
    payload = json.loads(metadata.read_text())
    assert isinstance(payload, dict)
    payload["pinned"] = True
    metadata.write_text(json.dumps(payload, indent=2))


def test_cut_version_creates_metadata_and_dedups_unchanged_tree(
    tmp_path: Path,
) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv_versions"
    workspace = store.path_for(cid)
    _write(workspace, "app.py", b"print('hi')\n")

    record = store.cut_version(cid, label="first", trigger="manual")

    assert record is not None
    assert record.seq == 1
    assert record.label == "first"
    assert record.trigger == "manual"
    assert record.file_count == 1
    assert record.total_bytes == len(b"print('hi')\n")
    assert record.tree_digest == tree_digest(workspace)

    version_dir = tmp_path / cid / "versions" / f"001-{record.tree_digest[:12]}"
    assert (version_dir / "workspace" / "app.py").read_bytes() == b"print('hi')\n"
    assert json.loads((version_dir / "version.json").read_text()) == _index(
        tmp_path, cid
    )[0]

    assert store.cut_version(cid, trigger="manual") is None
    assert sorted((tmp_path / cid / "versions").iterdir()) == [version_dir]


def test_cut_version_hardlinks_unchanged_files(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv_hardlinks"
    workspace = store.path_for(cid)
    _write(workspace, "stable.txt", b"same")
    _write(workspace, "changed.txt", b"before")

    first = store.cut_version(cid, trigger="auto")
    assert first is not None
    _write(workspace, "changed.txt", b"after")
    second = store.cut_version(cid, trigger="auto")
    assert second is not None

    first_workspace = _version_workspace(tmp_path, cid, first.seq, first.tree_digest)
    second_workspace = _version_workspace(tmp_path, cid, second.seq, second.tree_digest)

    assert (first_workspace / "stable.txt").stat().st_ino == (
        second_workspace / "stable.txt"
    ).stat().st_ino
    assert (first_workspace / "changed.txt").stat().st_ino != (
        second_workspace / "changed.txt"
    ).stat().st_ino


def test_list_versions_ordering_fields_and_resolver(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv_list"
    workspace = store.path_for(cid)
    _write(workspace, "file.txt", b"one")
    first = store.cut_version(cid, label="alpha", trigger="manual")
    assert first is not None
    _write(workspace, "file.txt", b"two")
    second = store.cut_version(cid, trigger="auto")
    assert second is not None

    versions = store.list_versions(cid)

    assert [version.seq for version in versions] == [2, 1]
    assert versions[0].label == ""
    assert versions[0].trigger == "auto"
    assert versions[0].pinned is False
    assert versions[1].label == "alpha"
    assert versions[1].file_count == 1
    assert store.version_workspace_path(cid, first.seq) == _version_workspace(
        tmp_path, cid, first.seq, first.tree_digest
    )
    with pytest.raises(StorageError):
        store.version_workspace_path(cid, 999)


def test_pruning_keeps_latest_unlabeled_plus_labeled_and_pinned(
    tmp_path: Path,
) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv_prune"
    workspace = store.path_for(cid)

    _write(workspace, "file.txt", b"labeled")
    labeled = store.cut_version(cid, label="keep", trigger="manual")
    assert labeled is not None

    _write(workspace, "file.txt", b"pinned")
    pinned = store.cut_version(cid, trigger="manual")
    assert pinned is not None
    _set_pinned(tmp_path, cid, pinned.seq)

    for n in range(22):
        _write(workspace, "file.txt", f"unlabeled-{n:02d}".encode("ascii"))
        assert store.cut_version(cid, trigger="auto") is not None

    seqs = {version.seq for version in store.list_versions(cid)}
    assert labeled.seq in seqs
    assert pinned.seq in seqs
    assert 3 not in seqs
    assert 4 not in seqs
    assert set(range(5, 25)).issubset(seqs)
    assert len([row for row in _index(tmp_path, cid) if not row.get("label")]) == 21


def test_byte_budget_pruning_counts_hardlinks_once(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path), version_byte_budget=10)
    cid = "conv_budget"
    workspace = store.path_for(cid)

    _write(workspace, "payload.bin", b"1111")
    first = store.cut_version(cid, trigger="auto")
    assert first is not None
    _write(workspace, "payload.bin", b"2222")
    second = store.cut_version(cid, trigger="auto")
    assert second is not None
    _write(workspace, "payload.bin", b"3333")
    third = store.cut_version(cid, trigger="auto")
    assert third is not None

    assert [version.seq for version in store.list_versions(cid)] == [3, 2]
    assert not _version_workspace(tmp_path, cid, first.seq, first.tree_digest).exists()
    assert _version_workspace(tmp_path, cid, second.seq, second.tree_digest).exists()
    assert _version_workspace(tmp_path, cid, third.seq, third.tree_digest).exists()

    labeled_store = ProjectStore(str(tmp_path / "labeled"), version_byte_budget=4)
    labeled_cid = "conv_labeled_budget"
    labeled_workspace = labeled_store.path_for(labeled_cid)
    _write(labeled_workspace, "payload.bin", b"large")
    labeled = labeled_store.cut_version(
        labeled_cid, label="release", trigger="manual"
    )
    assert labeled is not None
    _write(labeled_workspace, "payload.bin", b"tiny")
    assert labeled_store.cut_version(labeled_cid, trigger="auto") is not None
    assert [version.seq for version in labeled_store.list_versions(labeled_cid)] == [1]


def test_poisoned_cid_and_seq_rejected(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    for bad_cid in ("..", "../etc", "/abs", "a/b", "x\x00y"):
        with pytest.raises(StorageError):
            store.list_versions(bad_cid)
        with pytest.raises(StorageError):
            store.version_workspace_path(bad_cid, 1)

    for bad_seq in (0, -1, "../1"):
        with pytest.raises(StorageError):
            store.version_workspace_path("conv_safe", bad_seq)
