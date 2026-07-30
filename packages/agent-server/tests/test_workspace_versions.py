from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
from disco.agent_server.routes.conversations import make_conversations_router
from disco.agent_server.routes.preview import make_preview_router
from disco.agent_server.runtime import ConversationRuntime
from disco.agent_server.workspace_commit import resolve_committed_workspace
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceRestoredEvent,
    WorkspaceVersionEvent,
)
from disco.tools.projects import ProjectStore
from disco.tools.sandbox.base import ExecResult
from fastapi import FastAPI

CID = "conv_versions"


class _MemorySession:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.clears = 0

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        assert "rm -rf" in cmd
        self.files.clear()
        self.clears += 1
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path.strip("/")] = data

    async def read_file(self, path: str) -> bytes:
        norm = path.strip("./")
        if norm in self.files:
            return self.files[norm]
        raise FileNotFoundError(norm)

    async def list_dir(self, path: str) -> list[str]:
        norm = "" if path in ("", ".") else path.strip("/")
        prefix = f"{norm}/" if norm else ""
        children: set[str] = set()
        for rel in self.files:
            if not rel.startswith(prefix):
                continue
            rest = rel[len(prefix) :]
            if rest:
                children.add(rest.split("/", 1)[0])
        if not children and norm and norm not in self.files:
            raise FileNotFoundError(norm)
        return sorted(children)


def _runtime(store: SqliteEventStore, project_root: Path) -> ConversationRuntime:
    rt = ConversationRuntime(store, router=MagicMock())
    cfg = MagicMock()
    cfg.projects.projects_root = str(project_root)
    rt._config_store = MagicMock()
    rt._config_store.load.return_value = cfg
    return rt


def _conversation_app(store: SqliteEventStore, rt: ConversationRuntime) -> FastAPI:
    app = FastAPI()
    app.include_router(make_conversations_router(store, rt))
    return app


def _write_workspace(ps: ProjectStore, cid: str, files: dict[str, bytes]) -> None:
    workspace = ps.path_for(cid)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, data in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total += len(data)
    ps.write_manifest(
        cid,
        title="Versioned build",
        owner_id="local",
        created_at="2026-07-03T00:00:00+00:00",
        file_count=len(files),
        total_bytes=total,
    )


async def test_restore_endpoint_409_while_running(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store, tmp_path)
    app = _conversation_app(store, rt)

    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/conversations/{CID}/versions/1/restore")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "conversation_running"


async def test_restore_endpoint_404_unknown_seq(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store, tmp_path)
    app = _conversation_app(store, rt)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/conversations/{CID}/versions/99/restore")

    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "version_not_found"


async def test_restore_endpoint_appends_event_and_cuts_new_version(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store, tmp_path)
    ps = rt._projects.current_project_store()
    _write_workspace(ps, CID, {"index.html": b"old"})
    old = ps.cut_version(CID, trigger="turn")
    assert old is not None
    _write_workspace(ps, CID, {"index.html": b"new"})
    current = ps.cut_version(CID, trigger="turn")
    assert current is not None

    session = _MemorySession({"index.html": b"new", "stale.txt": b"delete me"})
    rt._executors[CID] = cast(Any, SimpleNamespace(_sandbox=session))
    app = _conversation_app(store, rt)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/conversations/{CID}/versions/{old.seq}/restore")
        listed = await client.get(f"/conversations/{CID}/versions")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "restored": old.seq,
        "new_version": 3,
        "tree_digest": old.tree_digest,
    }
    assert session.clears == 1
    assert session.files == {"index.html": b"old"}
    events = await store.get_events(CID)
    restored = [e for e in events if isinstance(e, WorkspaceRestoredEvent)]
    assert len(restored) == 1
    assert restored[0].version_seq == old.seq
    assert restored[0].tree_digest == old.tree_digest
    restored_idx = events.index(restored[0])
    assert restored_idx + 1 < len(events)
    notice = events[restored_idx + 1]
    assert isinstance(notice, MessageEvent)
    assert notice.source == EventSource.ENVIRONMENT
    assert notice.message.role == "user"
    assert notice.message.content.startswith("<system-reminder>")
    assert (
        f"workspace back to version {old.seq} (digest {old.tree_digest})" in notice.message.content
    )
    assert "NO LONGER EXIST on disk" in notice.message.content
    assert notice.message.content.endswith("</system-reminder>")
    versions = ps.list_versions(CID)
    assert [v.seq for v in versions] == [3, 2, 1]
    assert versions[0].trigger == "restore"
    assert versions[0].tree_digest == old.tree_digest
    assert versions[2].seq == old.seq
    assert versions[2].pinned is True

    assert listed.status_code == 200
    listed_old = next(row for row in listed.json()["versions"] if row["seq"] == old.seq)
    assert listed_old["pinned"] is True


async def test_restore_preserves_current_host_owned_deployment_record(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")
    rt = _runtime(store, tmp_path)
    ps = rt._projects.current_project_store()
    _write_workspace(ps, CID, {"index.html": b"old"})
    old = ps.cut_verified_version(CID, trigger="turn", pin=True)
    assert old is not None
    record_path = ".disco/cloudflare/deployments/signed.json"
    _write_workspace(
        ps,
        CID,
        {"index.html": b"current", record_path: b"host-signed-record"},
    )
    assert ps.cut_verified_version(CID, trigger="turn") is not None
    session = _MemorySession({"index.html": b"current"})
    rt._executors[CID] = cast(Any, SimpleNamespace(_sandbox=session))

    result = await rt.restore_workspace_version(CID, old.seq)

    assert session.files == {"index.html": b"old"}
    assert (ps.path_for(CID) / record_path).read_bytes() == b"host-signed-record"
    assert result["new_version"] is not None
    with ps.open_verified_version(CID, result["new_version"]) as verified:
        assert verified.read_bytes("index.html") == b"old"
        assert verified.read_bytes(record_path) == b"host-signed-record"


async def test_restore_of_finished_build_publishes_a_fresh_exact_seal(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")
    rt = _runtime(store, tmp_path)
    ps = rt._projects.current_project_store()
    _write_workspace(ps, CID, {"index.html": b"old"})
    old = ps.cut_verified_version(CID, trigger="turn", pin=True)
    assert old is not None

    session = _MemorySession({"index.html": b"new"})
    rt._executors[CID] = cast(Any, SimpleNamespace(_sandbox=session))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    await rt._lifecycle.commit_finished_workspace(
        CID,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    before = [
        event
        for event in await store.get_events(CID)
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(before) == 1

    app = _conversation_app(store, rt)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/conversations/{CID}/versions/{old.seq}/restore")

    assert response.status_code == 200, response.text
    events = await store.get_events(CID)
    committed = resolve_committed_workspace(events, ps, CID)
    assert response.json()["new_version"] == committed.record.seq
    assert (
        len(
            [
                event
                for event in events
                if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
            ]
        )
        == 2
    )
    with ps.open_verified_version(CID, committed.record.seq) as verified:
        assert verified.read_bytes("index.html") == b"old"


class _PreviewRuntime:
    def __init__(self, ps: ProjectStore) -> None:
        self._ps = ps

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        return None

    def live_session(self, conversation_id: str):
        return None

    def project_store(self) -> ProjectStore:
        return self._ps


async def test_preview_version_query_serves_version_bytes(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    _write_workspace(ps, CID, {"index.html": b"historical"})
    version = ps.cut_version(CID, trigger="turn")
    assert version is not None
    _write_workspace(ps, CID, {"index.html": b"current"})

    app = FastAPI()
    app.include_router(make_preview_router(store, cast(Any, _PreviewRuntime(ps))))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/conversations/{CID}/preview-app/?version={version.seq}")

    assert resp.status_code == 200
    assert resp.content.startswith(b"historical")


async def test_preview_version_query_404_for_unknown_version(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))

    app = FastAPI()
    app.include_router(make_preview_router(store, cast(Any, _PreviewRuntime(ps))))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/conversations/{CID}/preview-app/?version=404")

    assert resp.status_code == 404


class _LivePreviewRuntime(_PreviewRuntime):
    """Sandbox AWAKE: wake_for_preview resolves an upstream. A ?version request
    must STILL serve the version snapshot — the live proxy answering under a
    "viewing vN" banner would be a false affordance (caught live 2026-07-03)."""

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        return "http://127.0.0.1:59999"  # nothing listens; proxying = test failure


async def test_preview_version_query_bypasses_live_proxy(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    _write_workspace(ps, CID, {"index.html": b"historical"})
    version = ps.cut_version(CID, trigger="turn")
    assert version is not None
    _write_workspace(ps, CID, {"index.html": b"current"})

    app = FastAPI()
    app.include_router(make_preview_router(store, cast(Any, _LivePreviewRuntime(ps))))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        versioned = await client.get(f"/conversations/{CID}/preview-app/?version={version.seq}")
        missing = await client.get(f"/conversations/{CID}/preview-app/?version=99")

    assert versioned.status_code == 200
    assert versioned.content.startswith(b"historical")
    assert b"disco-element-mention-picker:v1" in versioned.content
    # Unknown version with a LIVE sandbox: 404, never a silent live-proxy answer.
    assert missing.status_code == 404
