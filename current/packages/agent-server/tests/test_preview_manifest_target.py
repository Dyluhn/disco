"""F03/F04: completed previews are rooted at the selected app deliverable.

These are route-level integration tests over a real ProjectStore and event
store.  The workspace deliberately contains a stale root scaffold so any
fallback to ``workspace/index.html`` is immediately visible.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from disco.agent_server.routes.preview import make_preview_router
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    FinalWorkspaceSeal,
    ResourceKey,
    SqliteEventStore,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI


class _RestartedRuntime:
    """A fresh app process: persisted snapshots exist; no sandbox survived."""

    def __init__(self, project_store: ProjectStore) -> None:
        self._project_store = project_store
        self.wake_calls: list[tuple[str, int]] = []

    async def wake_for_preview(self, cid8: str, port: int) -> None:
        self.wake_calls.append((cid8, port))
        return None

    def live_session(self, conversation_id: str) -> None:
        return None

    def _current_project_store(self) -> ProjectStore:
        return self._project_store

    @property
    def projects(self) -> SimpleNamespace:
        return SimpleNamespace(current_project_store=self._current_project_store)


def _replace_workspace(ps: ProjectStore, cid: str, files: dict[str, bytes]) -> None:
    workspace = ps.path_for(cid)
    if workspace.exists():
        import shutil

        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    for rel, body in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    ps.write_manifest(
        cid,
        title="Selected preview",
        owner_id="local",
        created_at="2026-07-14T00:00:00+00:00",
        file_count=len(files),
        total_bytes=sum(len(body) for body in files.values()),
    )


async def _commit(
    store: SqliteEventStore,
    ps: ProjectStore,
    cid: str,
    *,
    entry: str,
) -> int:
    await store.append(
        cid,
        DeliverableEvent(title="Verified app", path=entry, artifact_kind="app"),
    )
    terminal = await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    assert terminal.seq is not None
    version = ps.cut_verified_version(cid, trigger="finish", pin=True)
    assert version is not None
    await store.append(
        cid,
        WorkspaceVersionEvent(
            version_seq=version.seq,
            tree_digest=version.tree_digest,
            trigger="finish",
            final_seal=FinalWorkspaceSeal(
                scope=ResourceKey(namespace="workspace.tree", identifier=cid),
                terminal_seq=terminal.seq,
                latest_effect_seq=None,
                version_seq=version.seq,
                tree_digest=version.tree_digest,
                file_count=version.file_count,
                total_bytes=version.total_bytes,
            ),
        ),
    )
    return version.seq


def _app(store: SqliteEventStore, runtime: _RestartedRuntime) -> FastAPI:
    app = FastAPI()
    app.include_router(make_preview_router(store, cast(Any, runtime)))
    return app


@pytest.mark.parametrize("surface", ["build", "agent"])
async def test_completed_preview_uses_selected_entry_and_all_relative_assets(
    tmp_path: Path,
    surface: str,
) -> None:
    cid = f"conv_manifest_{surface}"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local", surface=surface)
    ps = ProjectStore(str(tmp_path / surface))
    _replace_workspace(
        ps,
        cid,
        {
            "index.html": b"STALE ROOT SCAFFOLD",
            "release/index.html": (
                b'<link rel="stylesheet" href="assets/theme.css?v=7">'
                b'<script src="development/scripts/app.js"></script>'
                b'<img src="media/hero image.svg">LATEST SELECTED APP'
            ),
            "release/assets/theme.css": (
                b'@font-face{font-family:x;src:url("../fonts/app.woff2#face")}'
            ),
            "release/scripts/app.js": b"globalThis.SELECTED_APP = true;",
            "release/media/hero image.svg": b"<svg>encoded asset</svg>",
            "release/fonts/app.woff2": b"font-bytes",
            "release/docs/index.html": b"NESTED ROUTE",
        },
    )
    await _commit(store, ps, cid, entry="release/index.html")
    # A post-commit change to the mutable mirror must never alter the completed
    # preview. The immutable version is the only authority for FINISHED.
    (ps.path_for(cid) / "release" / "index.html").write_bytes(b"MUTATED LIVE MIRROR")

    # Constructing a fresh runtime is the application-restart boundary.
    runtime = _RestartedRuntime(ProjectStore(str(tmp_path / surface)))
    transport = httpx.ASGITransport(app=_app(store, runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get(f"/conversations/{cid}/preview-app/")
        css = await client.get(
            f"/conversations/{cid}/preview-app/assets/theme.css?v=7#ignored-by-http"
        )
        script = await client.get(f"/conversations/{cid}/preview-app//scripts/app.js")
        image = await client.get(f"/conversations/{cid}/preview-app/media/hero%20image.svg")
        font = await client.get(f"/conversations/{cid}/preview-app/fonts/app.woff2")
        nested = await client.get(f"/conversations/{cid}/preview-app/docs/")

    assert root.status_code == 200
    assert b"MUTATED LIVE MIRROR" not in root.content
    assert b"LATEST SELECTED APP" in root.content
    assert b"STALE ROOT SCAFFOLD" not in root.content
    assert css.status_code == 200 and css.content.startswith(b"@font-face")
    assert script.status_code == 200 and script.content == b"globalThis.SELECTED_APP = true;"
    assert image.status_code == 200 and image.content == b"<svg>encoded asset</svg>"
    assert font.status_code == 200 and font.content == b"font-bytes"
    assert nested.status_code == 200 and b"NESTED ROUTE" in nested.content
    # A committed snapshot is authoritative; no stale live server is even woken.
    assert runtime.wake_calls == []
    store.close()


async def test_later_revision_and_historical_version_keep_their_own_entries(
    tmp_path: Path,
) -> None:
    cid = "conv_manifest_revisions"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local", surface="build")
    ps = ProjectStore(str(tmp_path))

    _replace_workspace(
        ps,
        cid,
        {"index.html": b"STALE", "v1/index.html": b"SELECTED VERSION ONE"},
    )
    v1 = await _commit(store, ps, cid, entry="v1/index.html")

    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    _replace_workspace(
        ps,
        cid,
        {"index.html": b"STALE AGAIN", "v2/index.html": b"SELECTED VERSION TWO"},
    )
    v2 = await _commit(store, ps, cid, entry="v2")
    assert v2 > v1

    runtime = _RestartedRuntime(ProjectStore(str(tmp_path)))
    transport = httpx.ASGITransport(app=_app(store, runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        current = await client.get(f"/conversations/{cid}/preview-app/")
        historical = await client.get(f"/conversations/{cid}/preview-app/?version={v1}")

    assert current.status_code == 200 and b"SELECTED VERSION TWO" in current.content
    assert historical.status_code == 200 and b"SELECTED VERSION ONE" in historical.content
    assert b"STALE" not in current.content
    assert b"STALE" not in historical.content
    store.close()


async def test_unchanged_finish_reuses_version_but_commits_latest_app_entry(
    tmp_path: Path,
) -> None:
    cid = "conv_manifest_reused_version"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local", surface="build")
    ps = ProjectStore(str(tmp_path))
    _replace_workspace(
        ps,
        cid,
        {
            "old/index.html": b"OLD APP ENTRY",
            "new/index.html": b"CURRENT APP ENTRY",
        },
    )
    first = await _commit(store, ps, cid, entry="old/index.html")

    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    second = await _commit(store, ps, cid, entry="new/index.html")
    assert second == first

    runtime = _RestartedRuntime(ProjectStore(str(tmp_path)))
    transport = httpx.ASGITransport(app=_app(store, runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        current = await client.get(f"/conversations/{cid}/preview-app/")
        selected = await client.get(f"/conversations/{cid}/preview-app/?version={second}")

    assert current.status_code == 200 and b"CURRENT APP ENTRY" in current.content
    assert selected.status_code == 200 and b"CURRENT APP ENTRY" in selected.content
    assert b"OLD APP ENTRY" not in current.content
    assert b"OLD APP ENTRY" not in selected.content

    # An interrupted later finalization may publish an internal checkpoint for
    # the reused version after selecting a different entry. Historical preview
    # remains bound to the latest public version marker, never that checkpoint.
    await store.append(
        cid,
        DeliverableEvent(title="Interrupted", path="old/index.html", artifact_kind="app"),
    )
    await store.append(
        cid,
        WorkspaceVersionEvent(
            version_seq=second,
            tree_digest=ps.verify_version(cid, second).tree_digest,
            trigger="finalizing:999",
        ),
    )
    interrupted_transport = httpx.ASGITransport(app=_app(store, runtime))
    async with httpx.AsyncClient(
        transport=interrupted_transport,
        base_url="http://test",
    ) as client:
        interrupted = await client.get(f"/conversations/{cid}/preview-app/?version={second}")
    assert interrupted.status_code == 200
    assert b"CURRENT APP ENTRY" in interrupted.content
    assert b"OLD APP ENTRY" not in interrupted.content
    store.close()


async def test_missing_selected_entry_never_falls_back_and_traversal_stays_closed(
    tmp_path: Path,
) -> None:
    cid = "conv_manifest_missing"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local", surface="agent")
    ps = ProjectStore(str(tmp_path))
    _replace_workspace(ps, cid, {"index.html": b"STALE SECRET SCAFFOLD"})
    await _commit(store, ps, cid, entry="deleted-release/index.html")

    runtime = _RestartedRuntime(ProjectStore(str(tmp_path)))
    transport = httpx.ASGITransport(app=_app(store, runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get(f"/conversations/{cid}/preview-app/")
        traversal = await client.get(f"/conversations/{cid}/preview-app/%2e%2e/%2e%2e/etc/passwd")
        encoded_once = await client.get(
            f"/conversations/{cid}/preview-app/%252e%252e/not-a-traversal"
        )

    assert missing.status_code == 404
    assert b"STALE SECRET SCAFFOLD" not in missing.content
    assert traversal.status_code == 404
    assert encoded_once.status_code == 404
    assert b"root:" not in traversal.content
    store.close()


async def test_finished_legacy_marker_never_serves_mutable_bytes(tmp_path: Path) -> None:
    cid = "conv_manifest_unsealed"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local", surface="build")
    ps = ProjectStore(str(tmp_path))
    _replace_workspace(ps, cid, {"index.html": b"UNSEALED MUTABLE"})
    await store.append(
        cid,
        DeliverableEvent(title="Unsealed", path="index.html", artifact_kind="app"),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    version = ps.cut_version(cid, trigger="finish")
    assert version is not None
    await store.append(
        cid,
        WorkspaceVersionEvent(
            version_seq=version.seq,
            tree_digest=version.tree_digest,
            trigger="finish",
        ),
    )

    runtime = _RestartedRuntime(ProjectStore(str(tmp_path)))
    transport = httpx.ASGITransport(app=_app(store, runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/conversations/{cid}/preview-app/")

    assert response.status_code == 503
    assert b"UNSEALED MUTABLE" not in response.content
    store.close()
