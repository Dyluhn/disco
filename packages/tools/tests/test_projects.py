"""Build-project persistence — the load-bearing round-trip + graceful failure.

The headline assertion (test_workspace_roundtrip): a sandbox is snapshotted to
disk → a FRESH sandbox is rehydrated from that disk tree → read_file returns
the same bytes for every file. The "saved-and-reloaded" property is only real
if the bytes actually round-trip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from disco.tools.projects import (
    ProjectStore,
    StorageStatus,
    rehydrate_workspace,
    snapshot_workspace,
    validate_root,
    zip_workspace,
)


class _FakeSandbox:
    """A minimal SandboxInstance-shaped double: a flat path → bytes map that
    supports the three I/O methods snapshot/rehydrate go through."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})

    async def list_dir(self, path: str) -> list[str]:
        if path == "." or path == "":
            return sorted({k.split("/", 1)[0] for k in self.files})
        prefix = path + "/"
        children: set[str] = set()
        for k in self.files:
            if k == path:
                raise OSError("not a directory")
            if k.startswith(prefix):
                children.add(k[len(prefix):].split("/", 1)[0])
        if not children:
            raise FileNotFoundError(path)
        return sorted(children)

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise IsADirectoryError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


# ---- validate_root: the graceful-failure foundation -----------------------


def test_validate_root_unset() -> None:
    assert validate_root("") == StorageStatus.UNSET
    assert validate_root("   ") == StorageStatus.UNSET


def test_validate_root_missing(tmp_path: Path) -> None:
    assert validate_root(str(tmp_path / "does_not_exist")) == StorageStatus.NOT_FOUND


def test_validate_root_not_a_directory(tmp_path: Path) -> None:
    f = tmp_path / "afile"
    f.write_text("hi")
    assert validate_root(str(f)) == StorageStatus.NOT_A_DIRECTORY


def test_validate_root_ok(tmp_path: Path) -> None:
    assert validate_root(str(tmp_path)) == StorageStatus.OK


def test_validate_root_not_writable(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)  # r-x only
    try:
        # On filesystems where chmod is enforced the probe write fails.
        # Some CI runners run as root, where the probe succeeds regardless;
        # skip there rather than report a flake.
        if os.geteuid() == 0:
            pytest.skip("running as root — chmod read-only is not enforced")
        assert validate_root(str(locked)) == StorageStatus.NOT_WRITABLE
    finally:
        os.chmod(locked, 0o700)


# ---- round-trip: the headline -----------------------------------------------


async def test_workspace_roundtrip(tmp_path: Path) -> None:
    """The load-bearing property: a snapshot followed by a rehydrate into a
    FRESH sandbox returns exactly the same bytes for every file."""
    payload = {
        "index.html": b"<h1>ok</h1>",
        "src/app.py": b'print("hi")',
        "src/util/helpers.py": b"def f():\n    return 42\n",
        "img/logo.png": b"\x89PNG\r\n\x1a\nbinary-bytes-not-utf8",
    }
    source = _FakeSandbox(payload)
    store = ProjectStore(str(tmp_path))
    cid = "conv_roundtrip"

    # snapshot OUT
    result = await snapshot_workspace(source, store.path_for(cid))
    assert result.file_count == len(payload)
    assert result.total_bytes == sum(len(v) for v in payload.values())
    assert set(result.paths) == set(payload)

    # rehydrate IN to a fresh box
    fresh = _FakeSandbox()
    count = await rehydrate_workspace(fresh, store.path_for(cid))
    assert count == len(payload)
    # the load-bearing assertion: every file's bytes match exactly.
    for path, expected in payload.items():
        actual = await fresh.read_file(path)
        assert actual == expected, f"{path}: bytes diverged on round-trip"

    # the manifest is the cheap row that powers the list view
    store.write_manifest(
        cid,
        title="Round-trip demo",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=result.file_count,
        total_bytes=result.total_bytes,
    )
    rows = store.list_projects()
    assert len(rows) == 1 and rows[0].conversation_id == cid
    assert rows[0].file_count == len(payload)
    assert rows[0].files_missing is False


# ---- graceful failure -------------------------------------------------------


async def test_files_missing_when_workspace_deleted(tmp_path: Path) -> None:
    """A manifest without its workspace tree must surface files_missing=True,
    not silently report success."""
    store = ProjectStore(str(tmp_path))
    cid = "conv_orphan"
    # snapshot then nuke the workspace, leaving the manifest behind
    source = _FakeSandbox({"a.txt": b"x"})
    await snapshot_workspace(source, store.path_for(cid))
    store.write_manifest(
        cid,
        title="Orphan",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=1,
        total_bytes=1,
    )
    import shutil

    shutil.rmtree(store.path_for(cid))
    rows = store.list_projects()
    assert len(rows) == 1
    assert rows[0].files_missing is True
    # rehydrating an empty/missing tree must be a silent no-op (return 0),
    # not an exception — graceful failure all the way through.
    fresh = _FakeSandbox()
    n = await rehydrate_workspace(fresh, store.path_for(cid))
    assert n == 0


def test_list_projects_empty_when_unset(tmp_path, monkeypatch) -> None:
    # E4: an unset root resolves to the auto-created default; isolate it to an empty
    # tmp data dir so "unset" lists empty (the real ~/.local/share default may have
    # projects from actual use).
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    store = ProjectStore("")
    assert store.list_projects() == []


def test_unsafe_conversation_id_rejected(tmp_path: Path) -> None:
    """Defense in depth against a poisoned cid — the conversations table
    enforces a UUID shape, but the store path code must not allow `..` or `/`
    as a project identifier."""
    from disco.tools.projects import StorageError

    store = ProjectStore(str(tmp_path))
    for bad in ("..", "../etc", "/abs", "a/b", "x\x00y"):
        with pytest.raises(StorageError):
            store.path_for(bad)


# ---- zip download -----------------------------------------------------------


async def test_zip_contains_real_files(tmp_path: Path) -> None:
    import io
    import zipfile

    store = ProjectStore(str(tmp_path))
    cid = "conv_zip"
    source = _FakeSandbox({"a.txt": b"alpha", "sub/b.txt": b"beta-binary"})
    await snapshot_workspace(source, store.path_for(cid))
    blob = b"".join(zip_workspace(store.path_for(cid)))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = sorted(zf.namelist())
        assert names == ["a.txt", "sub/b.txt"]
        assert zf.read("a.txt") == b"alpha"
        assert zf.read("sub/b.txt") == b"beta-binary"


# ---- snapshot resilience (the dc-02 live-rung defect) ------------------------


class _FlakySandbox(_FakeSandbox):
    """_FakeSandbox plus scripted per-path failures: any path listed in
    `broken` fails BOTH read_file and list_dir (the dropped-transport shape —
    the probe can neither read it nor descend into it)."""

    def __init__(self, files: dict[str, bytes], broken: set[str]) -> None:
        super().__init__(files)
        self.broken = set(broken)

    async def list_dir(self, path: str) -> list[str]:
        if path in self.broken:
            raise OSError(32, "Broken pipe")
        return await super().list_dir(path)

    async def read_file(self, path: str) -> bytes:
        if path in self.broken:
            raise OSError(32, "Broken pipe")
        return await super().read_file(path)


async def test_snapshot_excludes_dependency_caches(tmp_path: Path) -> None:
    """node_modules/.pnpm-store/__pycache__ are never walked — the lockfile is
    the snapshot of the dependency tree, the store itself is reproducible (and
    walking it per-file over ssh is what broke the dc-02 live rung)."""
    payload = {
        "index.html": b"<h1>ok</h1>",
        "package.json": b"{}",
        "node_modules/react/index.js": b"module.exports = {}",
        ".pnpm-store/v11/files/00/abc": b"blob",
        "src/__pycache__/app.cpython-313.pyc": b"\x00",
        "src/app.py": b"print('hi')",
    }
    result = await snapshot_workspace(_FakeSandbox(payload), tmp_path / "snap")
    assert set(result.paths) == {"index.html", "package.json", "src/app.py"}
    assert result.skipped == []
    assert not (tmp_path / "snap" / "node_modules").exists()
    assert not (tmp_path / "snap" / ".pnpm-store").exists()


async def test_snapshot_tolerates_unreadable_entries(tmp_path: Path) -> None:
    """One unreadable file no longer aborts the snapshot: everything else is
    mirrored, the casualty is recorded in result.skipped, and the caller can
    still write the manifest (the project stays revivable)."""
    payload = {
        "index.html": b"<h1>ok</h1>",
        "data/readings.csv": b"a,b\n1,2\n",
        "data/locked.bin": b"unreadable",
    }
    flaky = _FlakySandbox(payload, broken={"data/locked.bin"})
    result = await snapshot_workspace(flaky, tmp_path / "snap")
    assert set(result.paths) == {"index.html", "data/readings.csv"}
    assert len(result.skipped) == 1
    assert "data/locked.bin" in result.skipped[0]
    assert "Broken pipe" in result.skipped[0]


async def test_snapshot_dead_transport_aborts(tmp_path: Path) -> None:
    """A dropped pipe fails EVERY call — after the consecutive-failure cap the
    snapshot aborts loudly instead of grinding through thousands of doomed
    round-trips. (Distinct from the one-bad-file case above.)"""
    from disco.tools.projects.archive import (
        _SNAPSHOT_MAX_CONSECUTIVE_FAILURES,
        WorkspaceArchiveError,
    )

    n = _SNAPSHOT_MAX_CONSECUTIVE_FAILURES + 2
    payload = {f"dir/f{i:03}": b"x" for i in range(n)}
    flaky = _FlakySandbox(payload, broken={f"dir/f{i:03}" for i in range(n)})
    with pytest.raises(WorkspaceArchiveError, match="transport presumed dead"):
        await snapshot_workspace(flaky, tmp_path / "snap")


async def test_snapshot_oversized_file_skipped_not_fatal(tmp_path: Path) -> None:
    """An oversized artifact is skipped + recorded, not a snapshot-killer: the
    cap's job is to bound disk usage, not to hold the whole project hostage."""
    payload = {"small.txt": b"ok", "huge.bin": b"x" * 64}
    result = await snapshot_workspace(
        _FakeSandbox(payload), tmp_path / "snap", max_file_bytes=32
    )
    assert result.paths == ["small.txt"]
    assert len(result.skipped) == 1 and "huge.bin" in result.skipped[0]
