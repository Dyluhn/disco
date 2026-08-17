"""Build-project persistence — the load-bearing round-trip + graceful failure.

The headline assertion (test_workspace_roundtrip): a sandbox is snapshotted to
disk → a FRESH sandbox is rehydrated from that disk tree → read_file returns
the same bytes for every file. The "saved-and-reloaded" property is only real
if the bytes actually round-trip.
"""

from __future__ import annotations

import asyncio
import errno
import io
import os
import tarfile
from pathlib import Path

import pytest
from disco.tools.projects import (
    ProjectStore,
    StorageStatus,
    is_runtime_secret_path,
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
                children.add(k[len(prefix) :].split("/", 1)[0])
        if not children:
            raise FileNotFoundError(path)
        return sorted(children)

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise IsADirectoryError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


class _BulkSandbox:
    def __init__(
        self,
        members: dict[str, bytes],
        *,
        link: str | None = None,
        preserve: list[str] | None = None,
    ) -> None:
        self.members = members
        self.link = link
        self.preserve = preserve or []
        self.export_calls = 0

    async def export_workspace_archive(
        self, destination: Path, *, max_depth: int, max_file_bytes: int
    ) -> tuple[list[str], list[str]]:
        self.export_calls += 1
        with tarfile.open(destination, "w:") as archive:
            for name, data in self.members.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if self.link is not None:
                member = tarfile.TarInfo(self.link)
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                archive.addfile(member)
        return [], self.preserve

    async def list_dir(self, _path: str) -> list[str]:
        raise AssertionError("bulk snapshot fell back to per-node list")

    async def read_file(self, _path: str) -> bytes:
        raise AssertionError("bulk snapshot fell back to per-node read")


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


async def test_bulk_snapshot_uses_one_archive_and_reapplies_host_filters(tmp_path: Path) -> None:
    bulk = _BulkSandbox(
        {
            "index.html": b"<h1>bulk</h1>",
            "src/app.js": b"console.log('ok')\n",
            ".env": b"SECRET=must-not-persist",
            "node_modules/pkg/index.js": b"cache",
        }
    )

    result = await snapshot_workspace(bulk, tmp_path / "snap")

    assert bulk.export_calls == 1
    assert result.paths == ["index.html", "src/app.js"]
    assert (tmp_path / "snap" / "index.html").read_bytes() == b"<h1>bulk</h1>"
    assert not (tmp_path / "snap" / ".env").exists()
    assert not (tmp_path / "snap" / "node_modules").exists()


@pytest.mark.parametrize("bulk", [False, True])
async def test_snapshot_preserves_host_owned_deployment_records_over_sandbox(
    tmp_path: Path,
    bulk: bool,
) -> None:
    dest = tmp_path / "snap"
    record = dest / ".disco" / "cloudflare" / "deployments" / "receipt.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b"host-signed-record")
    sandbox_files = {
        "index.html": b"fresh app",
        ".disco/cloudflare/deployments/receipt.json": b"stale sandbox copy",
        ".disco/cloudflare/other.json": b"sandbox-owned sibling",
    }
    source = _BulkSandbox(sandbox_files) if bulk else _FakeSandbox(sandbox_files)

    result = await snapshot_workspace(source, dest)

    assert record.read_bytes() == b"host-signed-record"
    assert (dest / "index.html").read_bytes() == b"fresh app"
    assert (dest / ".disco" / "cloudflare" / "other.json").read_bytes() == (
        b"sandbox-owned sibling"
    )
    assert ".disco/cloudflare/deployments/receipt.json" in result.paths


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("blocking_path", [".disco", ".disco/cloudflare"])
async def test_host_owned_record_wins_over_fresh_file_shaped_ancestor(
    tmp_path: Path,
    bulk: bool,
    blocking_path: str,
) -> None:
    dest = tmp_path / "snap"
    record = dest / ".disco" / "cloudflare" / "deployments" / "receipt.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b"host-signed-record")
    files = {"index.html": b"fresh app", blocking_path: b"sandbox blocker"}
    source = _BulkSandbox(files) if bulk else _FakeSandbox(files)

    result = await snapshot_workspace(source, dest)

    assert record.read_bytes() == b"host-signed-record"
    assert (dest / "index.html").read_bytes() == b"fresh app"
    assert ".disco/cloudflare/deployments/receipt.json" in result.paths


@pytest.mark.parametrize("bulk", [False, True])
async def test_first_snapshot_reserves_host_owned_namespace_from_ancestor_file(
    tmp_path: Path,
    bulk: bool,
) -> None:
    files = {"index.html": b"app", ".disco": b"sandbox blocker"}
    source = _BulkSandbox(files) if bulk else _FakeSandbox(files)
    dest = tmp_path / "snap"

    result = await snapshot_workspace(source, dest)

    assert (dest / "index.html").read_bytes() == b"app"
    assert not (dest / ".disco").is_file()
    assert result.paths == ["index.html"]


@pytest.mark.parametrize("bulk", [False, True])
async def test_first_snapshot_discards_untrusted_sandbox_host_owned_record(
    tmp_path: Path,
    bulk: bool,
) -> None:
    files = {
        "index.html": b"app",
        ".disco/cloudflare/deployments/forged.json": b"sandbox-forged",
    }
    source = _BulkSandbox(files) if bulk else _FakeSandbox(files)
    dest = tmp_path / "snap"

    result = await snapshot_workspace(source, dest)

    assert (dest / "index.html").read_bytes() == b"app"
    assert not (dest / ".disco" / "cloudflare" / "deployments").exists()
    assert result.paths == ["index.html"]


async def test_bulk_snapshot_rejects_links_without_replacing_last_good_tree(tmp_path: Path) -> None:
    from disco.tools.projects.archive import WorkspaceArchiveError

    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "index.html").write_bytes(b"last-good")
    bulk = _BulkSandbox({"new.txt": b"new"}, link="escape")

    with pytest.raises(WorkspaceArchiveError, match="non-regular"):
        await snapshot_workspace(bulk, dest)

    files = {
        path.relative_to(dest).as_posix(): path.read_bytes()
        for path in dest.rglob("*")
        if path.is_file()
    }
    assert files == {"index.html": b"last-good"}
    assert not list(tmp_path.glob(".snap.snapshot-*"))


@pytest.mark.parametrize(
    "members",
    [
        {"a": b"file", "a/b": b"child"},
        {"a/b": b"child", "a": b"file"},
    ],
)
async def test_bulk_snapshot_rejects_file_prefix_conflicts_atomically(
    tmp_path: Path, members: dict[str, bytes]
) -> None:
    from disco.tools.projects.archive import WorkspaceArchiveError

    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "index.html").write_bytes(b"last-good")

    with pytest.raises(WorkspaceArchiveError, match="conflicting path prefix"):
        await snapshot_workspace(_BulkSandbox(members), dest)

    assert (dest / "index.html").read_bytes() == b"last-good"
    assert not list(tmp_path.glob(".snap.snapshot-*"))


async def test_preserved_path_cannot_follow_host_link_outside_snapshot(tmp_path: Path) -> None:
    from disco.tools.projects.archive import WorkspaceArchiveError

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"never-copy")
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceArchiveError, match="traverse preserved path"):
        await snapshot_workspace(_BulkSandbox({}, preserve=["link/secret.txt"]), dest)

    assert (outside / "secret.txt").read_bytes() == b"never-copy"
    assert (dest / "link").is_symlink()
    assert not list(tmp_path.glob(".snap.snapshot-*"))


async def test_first_snapshot_ignores_unavailable_preserve_without_prior_tree(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "snap"

    result = await snapshot_workspace(
        _BulkSandbox({"index.html": b"first-good"}, preserve=["unreadable.txt"]), dest
    )

    assert result.paths == ["index.html"]
    assert (dest / "index.html").read_bytes() == b"first-good"
    assert not list(tmp_path.glob(".snap.snapshot-*"))


async def test_preserved_host_hardlink_is_rejected_without_publication(tmp_path: Path) -> None:
    from disco.tools.projects.archive import WorkspaceArchiveError

    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"never-copy")
    dest = tmp_path / "snap"
    dest.mkdir()
    os.link(outside, dest / "alias.txt")

    with pytest.raises(WorkspaceArchiveError, match="hardlinked"):
        await snapshot_workspace(_BulkSandbox({"index.html": b"new"}, preserve=["alias.txt"]), dest)

    assert outside.read_bytes() == b"never-copy"
    assert not (dest / "index.html").exists()
    assert not list(tmp_path.glob(".snap.snapshot-*"))


async def test_bulk_snapshot_cancellation_leaves_old_tree_and_no_transaction(
    tmp_path: Path,
) -> None:
    class _CanceledBulk(_BulkSandbox):
        async def export_workspace_archive(
            self, destination: Path, *, max_depth: int, max_file_bytes: int
        ) -> tuple[list[str], list[str]]:
            destination.write_bytes(b"partial")
            raise asyncio.CancelledError

    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "index.html").write_bytes(b"last-good")

    with pytest.raises(asyncio.CancelledError):
        await snapshot_workspace(_CanceledBulk({}), dest)

    assert (dest / "index.html").read_bytes() == b"last-good"
    assert not list(tmp_path.glob(".snap.snapshot-*"))


def test_imported_provenance_is_recorded_and_survives_resnapshot(tmp_path: Path) -> None:
    """The `imported` release-provenance bit is durable: set once at import time, it
    is PRESERVED across later re-snapshots (which rewrite the manifest without it) —
    a re-snapshot that dropped it would silently demote an imported project's release
    assessment from needs_review back to not_web."""
    store = ProjectStore(str(tmp_path))
    cid = "conv_imported"
    workspace = store.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("imported")

    # import-time write records the provenance.
    store.write_manifest(
        cid,
        title="t",
        owner_id="local",
        created_at=None,
        file_count=1,
        total_bytes=8,
        imported=True,
    )
    assert store.get(cid).imported is True
    assert store.list_projects()[0].imported is True

    # a re-snapshot (no `imported` arg) MUST preserve it, not reset it to False.
    store.write_manifest(
        cid,
        title="t",
        owner_id="local",
        created_at=None,
        file_count=1,
        total_bytes=8,
    )
    assert store.get(cid).imported is True

    # an explicit False clears it; a fresh (never-imported) project defaults False.
    store.write_manifest(
        cid,
        title="t",
        owner_id="local",
        created_at=None,
        file_count=1,
        total_bytes=8,
        imported=False,
    )
    assert store.get(cid).imported is False

    fresh = "conv_fresh"
    (store.path_for(fresh)).mkdir(parents=True, exist_ok=True)
    (store.path_for(fresh) / "a").write_text("x")
    store.write_manifest(
        fresh,
        title=None,
        owner_id="local",
        created_at=None,
        file_count=1,
        total_bytes=1,
    )
    assert store.get(fresh).imported is False


def test_release_intent_sidecar_round_trip_and_fail_closed(tmp_path: Path) -> None:
    """Store-level contract for the host-owned release-intent sidecar: it lives
    NEXT TO manifest.json (outside the workspace/ tree), round-trips typed,
    reports an ABSENT sidecar as None, fails CLOSED (StorageError) on a corrupt
    sidecar rather than silently reporting 'no intent', honors the poisoned-id
    guard, and is removed by delete() together with the rest of the project."""
    from disco.core.release.spec import ReleaseIntent
    from disco.tools.projects import StorageError

    store = ProjectStore(str(tmp_path))
    cid = "conv_release"
    workspace = store.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "server.js").write_text("x")

    # absent sidecar -> None (a never-declared intent is not an error)
    assert store.read_release_intent(cid) is None

    intent = ReleaseIntent(
        start_cmd=("node", "server.js"),
        required_env=("API_BASE_URL", "SESSION_SECRET"),
    )
    sidecar = store.write_release_intent(cid, intent)
    # host-owned location: next to manifest.json, OUTSIDE workspace/
    assert sidecar == store.release_intent_for(cid)
    assert sidecar.parent == store.manifest_for(cid).parent
    assert "workspace" not in sidecar.relative_to(tmp_path).parts
    # typed round-trip through the persisted JSON
    assert store.read_release_intent(cid) == intent

    # corrupt sidecar -> fail closed, never a silent None
    sidecar.write_bytes(b"{ not valid json")
    with pytest.raises(StorageError, match="release intent unreadable"):
        store.read_release_intent(cid)

    # poisoned id -> rejected before any path is derived
    with pytest.raises(StorageError, match="unsafe conversation_id"):
        store.release_intent_for("../escape")

    # delete() removes the whole project dir, sidecar included
    assert store.delete(cid) is True
    assert not sidecar.exists()


def test_runtime_secret_path_classifier_keeps_only_explicit_templates() -> None:
    for path in (
        ".env",
        ".env.local",
        "nested/.ENV.production.local",
        ".dev.vars",
        "worker/.dev.vars.production",
    ):
        assert is_runtime_secret_path(path)
    for path in (
        ".env.example",
        ".env.sample",
        "nested/.dev.vars.dist",
        ".dev.vars.template",
        "src/environment.ts",
    ):
        assert not is_runtime_secret_path(path)


async def test_runtime_secret_files_never_persist_or_rehydrate(tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / ".dev.vars").write_bytes(b"STALE_SECRET=must-disappear")
    source = _FakeSandbox(
        {
            "index.html": b"ok",
            ".dev.vars": b"STRIPE_WEBHOOK_SECRET=whsec_real",
            "nested/.env.production": b"STRIPE_KEY=rk_real",
            ".dev.vars.example": b"STRIPE_WEBHOOK_SECRET=replace-me",
        }
    )

    result = await snapshot_workspace(source, dest)

    assert set(result.paths) == {"index.html", ".dev.vars.example"}
    assert not (dest / ".dev.vars").exists()
    assert not (dest / "nested" / ".env.production").exists()
    fresh = _FakeSandbox()
    assert await rehydrate_workspace(fresh, dest) == 2
    assert set(fresh.files) == {"index.html", ".dev.vars.example"}


async def test_snapshot_removes_legacy_links_before_writing(tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    dest.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"host-value")
    (dest / "index.html").symlink_to(outside)

    await snapshot_workspace(_FakeSandbox({"index.html": b"sandbox-value"}), dest)

    assert outside.read_bytes() == b"host-value"
    assert not (dest / "index.html").is_symlink()
    assert (dest / "index.html").read_bytes() == b"sandbox-value"


async def test_rehydrate_and_zip_skip_legacy_symlinks(tmp_path: Path) -> None:
    import io
    import zipfile

    src = tmp_path / "workspace"
    src.mkdir()
    (src / "safe.txt").write_bytes(b"safe")
    outside = tmp_path / "host-secret.txt"
    outside.write_bytes(b"must-not-cross-boundary")
    (src / "leak.txt").symlink_to(outside)

    fresh = _FakeSandbox()
    assert await rehydrate_workspace(fresh, src) == 1
    assert fresh.files == {"safe.txt": b"safe"}
    blob = b"".join(zip_workspace(src))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.namelist() == ["safe.txt"]


async def test_snapshot_preserves_last_good_file_on_transient_read_failure(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "snap"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "locked.bin").write_bytes(b"last-good")
    source = _FlakySandbox(
        {"index.html": b"fresh", "data/locked.bin": b"unreadable"},
        broken={"data/locked.bin"},
    )

    await snapshot_workspace(source, dest)

    assert (dest / "data" / "locked.bin").read_bytes() == b"last-good"


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


def test_zip_excludes_runtime_secret_files_but_keeps_templates(tmp_path: Path) -> None:
    import io
    import zipfile

    src = tmp_path / "workspace"
    src.mkdir()
    (src / ".env").write_bytes(b"SECRET=real")
    (src / ".dev.vars.example").write_bytes(b"SECRET=replace-me")
    blob = b"".join(zip_workspace(src))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.namelist() == [".dev.vars.example"]


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
    result = await snapshot_workspace(_FakeSandbox(payload), tmp_path / "snap", max_file_bytes=32)
    assert result.paths == ["small.txt"]
    assert len(result.skipped) == 1 and "huge.bin" in result.skipped[0]


async def test_snapshot_excludes_the_python_user_site(tmp_path: Path) -> None:
    """PEP 370 user site is the same reproducible dependency tree as node_modules.

    Band-probe trial 002 could never be sealed: a Playwright install under
    `.local/lib/python3.14/site-packages` puts a 118MB `driver/node` in the
    workspace, far past the 32MB sandbox transfer cap, and the final seal refuses
    any tree containing an unreadable entry. `.venv` was already excluded, so the
    identical tree was in scope purely for living outside a virtualenv.
    """
    payload = {
        "index.html": b"<h1>ok</h1>",
        ".local/lib/python3.14/site-packages/playwright/__init__.py": b"x = 1",
        ".local/lib/python3.14/site-packages/playwright/driver/node": b"\x7fELF",
        ".local/bin/playwright": b"#!/bin/sh",
        "src/app.py": b"print('hi')",
    }
    result = await snapshot_workspace(_FakeSandbox(payload), tmp_path / "snap")

    assert set(result.paths) == {"index.html", ".local/bin/playwright", "src/app.py"}
    assert result.skipped == []
    assert not (tmp_path / "snap" / ".local" / "lib").exists()
    # Negative control: excluding site-packages must not exclude all of .local,
    # which holds real user-owned files (here, a console script).
    assert (tmp_path / "snap" / ".local" / "bin" / "playwright").exists()


async def test_unreadable_entry_reports_the_read_error_not_the_descend_error(
    tmp_path: Path,
) -> None:
    """The message must say why the READ failed, not why the fallback failed.

    The walker tries `read_file`, then `list_dir` as a "maybe it's a directory"
    fallback. It used to report only the fallback's error — and for any ordinary
    file that is always ENOTDIR, so a 32MB transfer-cap EFBIG and an unrelated
    failure on a small built CSS both surfaced as the same eleven words. An
    unreadable entry fails the final seal, so the discarded exception was the
    entire diagnosis.
    """

    class _CapSandbox(_FakeSandbox):
        async def read_file(self, path: str) -> bytes:
            if path == "big.bin":
                raise OSError(errno.EFBIG, "read_file 'big.bin' exceeds the transfer cap")
            return await super().read_file(path)

    payload = {"index.html": b"<h1>ok</h1>", "big.bin": b"\x00"}
    result = await snapshot_workspace(_CapSandbox(payload), tmp_path / "snap")

    assert result.paths == ["index.html"]
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.startswith("big.bin: ")
    assert "exceeds the transfer cap" in entry, entry
    assert "OSError" in entry, entry
    # The fallback's verdict may still appear, but it must not be the only thing.
    assert entry.index("exceeds the transfer cap") < len(entry)
