"""Imported binary/source trees survive the actual container batch writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.tools.projects import rehydrate_workspace
from disco.tools.projects.archive import WorkspaceArchiveError
from disco.tools.sandbox._container_parts.file_batch import parse_file_batch_result
from disco.tools.sandbox.file_batch import SandboxFileBatchResult
from test_container_file_batch import _run_guest


class GuestBatch:
    def __init__(self, root: Path):
        self.root = root
        self.calls = 0

    async def _commit_file_batch(self, mutations):
        self.calls += 1
        _, result = _run_guest(self.root, [(item.path, item.after) for item in mutations])
        return parse_file_batch_result(json.dumps(result).encode())

    async def read_file(self, path):
        return (self.root / "workspace" / path).read_bytes()

    async def write_file(self, path, data):
        raise AssertionError("batch must not fall back to individual writes")


@pytest.mark.asyncio
async def test_rehydrate_binary_import_and_resume_uses_verified_guest_batch(tmp_path):
    src = tmp_path / "snapshot"
    src.mkdir()
    files = {
        ".pmx/screenshots/0007-fill.png": b"\x89PNG\x00\xff" * 128,
        "index.html": b"<h1>Original imported app</h1>",
        "styles.css": b"body { color: navy; }",
        "app.js": b'localStorage.setItem("test", "preserved")',
    }
    for path, data in files.items():
        dest = src / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    guest_root = tmp_path / "guest"
    guest_root.mkdir()
    sandbox = GuestBatch(guest_root)
    assert await rehydrate_workspace(sandbox, src) == len(files)
    assert {path: await sandbox.read_file(path) for path in files} == files
    # Repeated hydration has no transitions; identical-file evidence still passes.
    assert await rehydrate_workspace(sandbox, src) == len(files)
    assert sandbox.calls == 2


@pytest.mark.asyncio
async def test_batch_jail_refusal_does_not_fallback_or_overwrite_external_file(tmp_path):
    src = tmp_path / "snapshot"
    (src / "escape").mkdir(parents=True)
    (src / "escape" / "owned.txt").write_bytes(b"overwrite")
    (src / "safe.txt").write_bytes(b"must not land")
    guest_root = tmp_path / "guest"
    (guest_root / "workspace").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "owned.txt").write_bytes(b"preserved")
    (guest_root / "workspace" / "escape").symlink_to(external, target_is_directory=True)
    with pytest.raises(WorkspaceArchiveError, match="rehydrate"):
        await rehydrate_workspace(GuestBatch(guest_root), src)
    assert (external / "owned.txt").read_bytes() == b"preserved"
    assert not (guest_root / "workspace" / "safe.txt").exists()


@pytest.mark.asyncio
async def test_missing_batch_transition_requires_matching_actual_bytes(tmp_path):
    src = tmp_path / "snapshot"
    src.mkdir()
    (src / "index.html").write_bytes(b"original")

    class MissingEvidence:
        async def _commit_file_batch(self, mutations):
            return SandboxFileBatchResult(changes=())

        async def read_file(self, path):
            return b"wrong bytes"

    with pytest.raises(WorkspaceArchiveError, match="differs from snapshot"):
        await rehydrate_workspace(MissingEvidence(), src)
