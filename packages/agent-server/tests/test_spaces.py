from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import create_app
from disco.agent_server.space_store import JsonSpaceStore
from disco.core import SqliteEventStore
from disco.retrieval import (
    DefaultCorpusService,
    DefaultRetrievalEngine,
    DiskVectorStore,
    ExtractedDoc,
    HashingEmbedder,
    LexicalReranker,
    RetrievalRequest,
    SearchHit,
)
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient


class _FakeSearch:
    async def search(self, query: str, **kwargs: Any) -> list[SearchHit]:  # noqa: ARG002
        return []


class _FakeExtraction:
    async def extract(self, url: str) -> ExtractedDoc:  # noqa: ARG002
        return ExtractedDoc(url=url, title=url, content="", passages=[])

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:  # noqa: ARG002
        return []


class _FakeRuntime:
    def __init__(self, root: Path) -> None:
        self._project_store = ProjectStore(str(root))
        self._embedder = HashingEmbedder()
        self._vector_store = DiskVectorStore(JsonSpaceStore(root).vectors_dir)

    def project_store(self) -> ProjectStore:
        return self._project_store

    def space_vector_store(self) -> DiskVectorStore:
        return self._vector_store

    def space_corpus_service(self) -> DefaultCorpusService:
        return DefaultCorpusService(self._vector_store, self._embedder)


def _client(tmp_path: Path) -> tuple[TestClient, _FakeRuntime]:
    rt = _FakeRuntime(tmp_path / "projects")
    return TestClient(create_app(SqliteEventStore(":memory:"), runtime=rt)), rt


def _upload(client: TestClient, space_id: str, name: str, data: bytes) -> Any:
    return client.post(
        f"/api/spaces/{space_id}/documents",
        files=[("files", (name, io.BytesIO(data), "application/octet-stream"))],
    )


def test_space_crud_round_trip(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    created = client.post(
        "/api/spaces",
        json={"name": "Policy archive", "description": "Internal notes"},
    )
    assert created.status_code == 200
    space = created.json()["space"]
    space_id = space["space_id"]

    listed = client.get("/api/spaces")
    assert listed.status_code == 200
    assert listed.json()["spaces"][0]["space_id"] == space_id
    assert listed.json()["spaces"][0]["doc_count"] == 0

    detail = client.get(f"/api/spaces/{space_id}")
    assert detail.status_code == 200
    assert detail.json()["space"]["description"] == "Internal notes"

    deleted = client.delete(f"/api/spaces/{space_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/spaces/{space_id}").status_code == 404


@pytest.mark.asyncio
async def test_space_upload_ingest_retrieve_and_persist(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    space_id = client.post("/api/spaces", json={"name": "Alpha"}).json()["space"]["space_id"]
    other_id = client.post("/api/spaces", json={"name": "Beta"}).json()["space"]["space_id"]

    uploaded = _upload(
        client,
        space_id,
        "alpha.md",
        b"# Notes\n\nSENTINEL_ALPHA durable evidence lives here.",
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["saved"][0]["passage_count"] >= 1
    assert uploaded.json()["space"]["doc_count"] == 1

    _upload(client, other_id, "beta.txt", b"SENTINEL_BETA unrelated evidence.")

    engine = DefaultRetrievalEngine(
        _FakeSearch(),
        _FakeExtraction(),
        LexicalReranker(),
        embedder=rt._embedder,
        vector_store=rt.space_vector_store(),
    )
    scoped = await engine.retrieve(
        RetrievalRequest(
            query="SENTINEL_ALPHA",
            use_web=False,
            corpus_ids=frozenset({space_id}),
        )
    )
    assert scoped.passages
    assert all(p.corpus_id == space_id for p in scoped.passages)
    assert all("SENTINEL_BETA" not in p.text for p in scoped.passages)

    restarted_store = DiskVectorStore(JsonSpaceStore(tmp_path / "projects").vectors_dir)
    restarted_engine = DefaultRetrievalEngine(
        _FakeSearch(),
        _FakeExtraction(),
        LexicalReranker(),
        embedder=HashingEmbedder(),
        vector_store=restarted_store,
    )
    persisted = await restarted_engine.retrieve(
        RetrievalRequest(
            query="SENTINEL_ALPHA",
            use_web=False,
            corpus_ids=frozenset({space_id}),
        )
    )
    assert persisted.passages
    assert persisted.passages[0].corpus_id == space_id

    restarted_registry = JsonSpaceStore(tmp_path / "projects")
    detail = restarted_registry.get(space_id)
    assert detail is not None
    assert detail.doc_count == 1
    assert detail.byte_count > 0
