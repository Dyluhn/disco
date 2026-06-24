"""BW-09: a seeded conversation title is sanitized at the SOURCE (the create
route) before it is persisted, so a verbose raw query (e.g. the Deep Research
surface seeding ``query.slice(0, 100)``) is never written to the DB and then
masked by a CSS truncate at render time."""

from __future__ import annotations

from disco.agent_server.routes import make_conversations_router
from disco.core import SqliteEventStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(store: SqliteEventStore) -> TestClient:
    app = FastAPI()
    # runtime=None: the create path still persists the (sanitized) title; the
    # runtime-only wiring (surface/model/etc.) is skipped, which is fine here.
    app.include_router(make_conversations_router(store, None))
    return TestClient(app)


def test_verbose_seed_title_persisted_clamped_not_raw() -> None:
    store = SqliteEventStore(":memory:")
    try:
        client = _client(store)
        raw = (
            "What are the most important macroeconomic and geopolitical factors "
            "driving global energy prices over the next decade and beyond?"
        )
        resp = client.post(
            "/conversations",
            json={"owner_id": "local", "surface": "deep_research", "title": raw},
        )
        assert resp.status_code == 200
        cid = resp.json()["conversation_id"]

        row = store._conn.execute(
            "SELECT title FROM conversations WHERE conversation_id = ?", (cid,)
        ).fetchone()
        stored = row["title"]
        assert stored != raw  # NOT the raw verbose query
        assert stored is not None and len(stored) <= 60  # clamped to the budget
        assert stored.startswith("What are the most")  # derived from the query head
        assert raw.startswith(stored)  # a clean prefix — no injected text
        # No trailing punctuation / no trailing whitespace at the boundary.
        assert not stored.endswith(("?", ".", ",", " "))
    finally:
        store.close()


def test_empty_seed_title_left_unset_for_auto_titler() -> None:
    """No seed (build/agent surfaces send title=None) → the title stays unset so
    the async auto-titler still owns it."""
    store = SqliteEventStore(":memory:")
    try:
        client = _client(store)
        resp = client.post(
            "/conversations", json={"owner_id": "local", "surface": "build"}
        )
        assert resp.status_code == 200
        cid = resp.json()["conversation_id"]
        row = store._conn.execute(
            "SELECT title FROM conversations WHERE conversation_id = ?", (cid,)
        ).fetchone()
        assert row["title"] is None
    finally:
        store.close()
