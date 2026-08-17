from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from disco.agent_server.auth import AgentAuthMiddleware
from disco.agent_server.routes.conversations import make_conversation_library_router
from disco.agent_server.routes.spaces import make_spaces_router
from disco.agent_server.space_store import JsonSpaceStore
from disco.core import SqliteEventStore
from disco.retrieval import DiskVectorStore
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeRuntime:
    def __init__(self, root: Path) -> None:
        self._project_store = ProjectStore(str(root))
        self._vector_store = DiskVectorStore(JsonSpaceStore(root).vectors_dir)
        self.spaces = self

    def _current_project_store(self) -> ProjectStore:
        return self._project_store

    def space_vector_store(self) -> DiskVectorStore:
        return self._vector_store

    @property
    def projects(self) -> SimpleNamespace:
        return SimpleNamespace(current_project_store=self._current_project_store)


def _client(tmp_path: Path) -> tuple[TestClient, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime(tmp_path / "projects")
    app = FastAPI()
    app.add_middleware(AgentAuthMiddleware, store=store)
    app.include_router(make_spaces_router(store, rt))  # type: ignore[arg-type]
    app.include_router(make_conversation_library_router(store, rt))  # type: ignore[arg-type]
    return TestClient(app), store


def test_space_crud_rename_and_membership_round_trip(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    store.create_conversation(
        "conv_member",
        owner_id="local",
        title="Build a dashboard",
        surface="build",
    )
    store.create_conversation(
        "conv_other",
        owner_id="local",
        title="Unrelated research",
        surface="research",
    )

    created = client.post(
        "/api/spaces",
        json={"name": "Policy archive", "description": "Internal notes"},
    )
    assert created.status_code == 200
    space = created.json()["space"]
    space_id = space["space_id"]
    assert space["member_count"] == 0
    assert space["members"] == []

    renamed = client.patch(
        f"/api/spaces/{space_id}",
        json={"name": "Policy work", "description": "Active work"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["space"]["name"] == "Policy work"

    assigned = client.post(
        "/api/conversations/conv_member/space",
        json={"space_id": space_id},
    )
    assert assigned.status_code == 200
    assert assigned.json()["space_id"] == space_id

    listed = client.get("/api/spaces")
    assert listed.status_code == 200
    assert listed.json()["spaces"][0]["space_id"] == space_id
    assert listed.json()["spaces"][0]["member_count"] == 1

    detail = client.get(f"/api/spaces/{space_id}")
    assert detail.status_code == 200
    body = detail.json()["space"]
    assert body["description"] == "Active work"
    assert body["member_count"] == 1
    assert [member["id"] for member in body["members"]] == ["conv_member"]
    assert body["members"][0]["space_id"] == space_id
    assert body["members"][0]["surface"] == "build"

    filtered = client.get(f"/api/conversations?space_id={space_id}&limit=10")
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()] == ["conv_member"]

    deleted = client.delete(f"/api/spaces/{space_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/spaces/{space_id}").status_code == 404

    after_delete = client.get("/api/conversations?space_id=&limit=10")
    assert after_delete.status_code == 200
    rows = after_delete.json()
    assert {row["id"] for row in rows} == {"conv_member", "conv_other"}
    assert next(row for row in rows if row["id"] == "conv_member")["space_id"] is None
