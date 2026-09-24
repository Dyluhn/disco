"""A failed import restore cannot execute or replace the saved source tree."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from disco.agent_server.lifecycle import LifecycleManager
from disco.agent_server.lifecycle_rehydration import Rehydration
from disco.core import ConversationStatus, StatusEvent
from disco.tools.projects import ProjectStore
from disco.tools.projects.archive import WorkspaceArchiveError


@pytest.mark.asyncio
async def test_failed_restore_preserves_source_blocks_snapshots_and_retries(tmp_path):
    ps = ProjectStore(str(tmp_path))
    cid = "conv_import_restore"
    source = ps.path_for(cid)
    (source / ".pmx/screenshots").mkdir(parents=True)
    files = {
        ".pmx/screenshots/0007-fill.png": b"\x89PNG original",
        "index.html": b"<h1>Imported source</h1>",
        "styles.css": b"body { color: blue; }",
    }
    for name, data in files.items():
        (source / name).write_bytes(data)
    ps.write_manifest(
        cid,
        title="import",
        owner_id="local",
        created_at=None,
        file_count=len(files),
        total_bytes=sum(map(len, files.values())),
    )

    class Session:
        fail = True
        files = {}

        async def write_file(self, path, data):
            if self.fail and path.endswith("0007-fill.png"):
                raise OSError("cannot resolve real target")
            self.files[path] = data

    session = Session()
    executor = SimpleNamespace(_sandbox=session)
    notifier = SimpleNamespace(emit=AsyncMock())
    hydration = Rehydration(
        SimpleNamespace(current_project_store=lambda: ps),
        SimpleNamespace(executor=lambda _: executor),
        notifier,
        None,
    )
    with pytest.raises(WorkspaceArchiveError, match="could not be restored"):
        await hydration._maybe_rehydrate(cid)
    assert cid not in hydration._rehydrated
    assert not hydration.snapshot_allowed(cid)
    manager = LifecycleManager.__new__(LifecycleManager)
    manager._rehydration = hydration
    manager._persistence = SimpleNamespace(
        capture_workspace=AsyncMock(),
        commit_finished_workspace=AsyncMock(),
    )
    await manager._maybe_snapshot(cid, trigger="suspend")
    with pytest.raises(WorkspaceArchiveError, match="restoration is incomplete"):
        await manager._capture_workspace(cid, trigger="manual")
    with pytest.raises(WorkspaceArchiveError, match="restoration is incomplete"):
        await manager.commit_finished_workspace(
            cid, StatusEvent(status=ConversationStatus.FINISHED)
        )
    manager._persistence.capture_workspace.assert_not_awaited()
    manager._persistence.commit_finished_workspace.assert_not_awaited()
    assert {name: (source / name).read_bytes() for name in files} == files
    session.fail = False
    await hydration._maybe_rehydrate(cid)
    assert hydration.snapshot_allowed(cid)
    assert session.files == files
    assert cid in hydration._rehydrated
    session.files["index.html"] = b"new in-progress edit"
    await hydration._maybe_rehydrate(cid)
    assert session.files["index.html"] == b"new in-progress edit"


@pytest.mark.asyncio
async def test_no_session_does_not_mark_hydrated(tmp_path):
    ps = ProjectStore(str(tmp_path))
    hydration = Rehydration(
        SimpleNamespace(current_project_store=lambda: ps),
        SimpleNamespace(executor=lambda _: None),
        SimpleNamespace(emit=AsyncMock()),
        None,
    )
    await hydration._maybe_rehydrate("conv_not_ready")
    assert "conv_not_ready" not in hydration._rehydrated
