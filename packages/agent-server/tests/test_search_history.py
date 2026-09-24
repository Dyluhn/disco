"""Standard Search is durable, owner-scoped and safe to reopen without execution."""

from __future__ import annotations

import asyncio
import copy

import pytest
from disco.agent_server import create_app
from disco.agent_server.search_history import SearchHistory, SearchScope, read_search_history
from disco.core import ConversationStatus, SqliteEventStore
from disco.core.auth import SESSION_COOKIE, SessionSigner
from fastapi.testclient import TestClient
from test_research import _FakeSearch, _runtime


def _request(request_id="first", **overrides):
    return {
        "conversation_id": "conv_search",
        "request_id": request_id,
        "query": "What is the capital of France?",
        "sources": [],
        **overrides,
    }


def _answer():
    return {
        "query": "q",
        "blocks": [
            {"kind": "prose", "id": "prose1", "text": "Paris. [[source1]]"},
            {"kind": "sheet", "id": "sheet1", "filename": "artifacts/table.xlsx"},
        ],
        "claims": [],
        "passages": [{"id": "source1", "text": "Paris", "source_url": "https://example.test"}],
        "all_hits": [],
        "unsupported_count": 0,
        "follow_ups": ["How large?"],
    }


def _frames(client, payload, headers=None):
    with client.websocket_connect("/ws/research", headers=headers or {}) as ws:
        ws.send_json(payload)
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "error" or frame == {"type": "state", "status": "finished"}:
                return frames


def test_real_pipeline_persists_and_reopens_after_restart_without_research(tmp_path):
    db = tmp_path / "search.sqlite"
    store = SqliteEventStore(db)
    store.create_conversation("conv_search", surface="research")
    search = _FakeSearch()
    client = TestClient(create_app(store, runtime=_runtime(store, search)))
    first = _frames(client, _request())
    final = next(frame["answer"] for frame in first if frame["type"] == "final")
    assert search.calls == 1
    persisted = client.get("/conversations/conv_search/research")
    assert persisted.status_code == 200
    assert persisted.headers["cache-control"] == "private, no-store"
    assert persisted.json()["turns"][0]["answer"] == final
    assert persisted.json()["turns"][0]["scope"]["request_id"] == "first"
    assert final["passages"][0]["id"] == "wiki_p0"
    rows = client.get("/api/conversations").json()
    row = next(row for row in rows if row["id"] == "conv_search")
    assert row["surface"] == "research"
    assert row["status"] == "FINISHED"
    assert row["title"]
    store.close()
    restarted = SqliteEventStore(db)
    cold_search = _FakeSearch()
    cold_client = TestClient(create_app(restarted, runtime=_runtime(restarted, cold_search)))
    for _ in range(2):
        assert cold_client.get("/conversations/conv_search/research").json() == persisted.json()
    replay = _frames(cold_client, _request())
    assert replay == [{"type": "final", "answer": final}, {"type": "state", "status": "finished"}]
    assert cold_search.calls == 0
    assert len(cold_client.get("/api/conversations").json()) == 1
    restarted.close()


async def test_followups_keep_all_original_bytes_and_identity():
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research")
    history = SearchHistory(store)
    original = _answer()
    calls = 0

    async def run():
        nonlocal calls
        calls += 1
        yield {"type": "final", "answer": copy.deepcopy(original)}
        yield {"type": "state", "status": "finished"}

    scopes = [_request(), _request("followup", query="How large?", parent_request_id="first")]
    for scope in scopes:
        assert len([frame async for frame in history.stream(SearchScope(**scope), run)]) == 2
    rows = await read_search_history(store, "conv_search")
    assert [t["scope"]["request_id"] for t in rows["turns"]] == ["first", "followup"]
    assert rows["turns"][1]["scope"]["parent_request_id"] == "first"
    assert all(turn["answer"] == original for turn in rows["turns"])
    # Same identity with modified input must never execute again.
    with pytest.raises(ValueError, match="different scope"):
        _ = [frame async for frame in history.stream(SearchScope(**_request(query="changed")), run)]
    assert calls == 2
    store.close()


async def test_interruption_requires_explicit_retry_and_does_not_duplicate_requests():
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research")
    history = SearchHistory(store)
    calls = 0

    async def run():
        nonlocal calls
        calls += 1
        yield {"type": "state", "status": "running"}
        if calls == 1:
            raise RuntimeError("provider unavailable")
        yield {"type": "final", "answer": _answer()}

    with pytest.raises(RuntimeError):
        _ = [frame async for frame in history.stream(SearchScope(**_request()), run)]
    assert (await store.get_state("conv_search")).execution_status == ConversationStatus.ERROR
    with pytest.raises(ValueError, match="interrupted"):
        _ = [frame async for frame in history.stream(SearchScope(**_request()), run)]
    assert calls == 1
    _ = [frame async for frame in history.stream(SearchScope(**_request("retry")), run)]
    assert calls == 2
    assert len((await read_search_history(store, "conv_search"))["turns"]) == 1
    store.close()


async def test_active_request_rejects_parallel_work_and_cancellation_releases_it():
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research")
    history = SearchHistory(store)

    async def run():
        yield {"type": "state", "status": "running"}
        await asyncio.Event().wait()

    first = history.stream(SearchScope(**_request()), run)
    await anext(first)
    with pytest.raises(ValueError, match="already running"):
        await anext(history.stream(SearchScope(**_request("second")), run))
    await first.aclose()
    assert not history.active
    assert (await store.get_state("conv_search")).execution_status == ConversationStatus.ERROR
    store.close()


@pytest.mark.parametrize(
    "surface,origin", [("agent", None), ("build", None), ("research", "imported")]
)
async def test_cannot_mutate_other_surfaces_or_imported_authority(surface, origin):
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface=surface, origin=origin)

    async def run():
        pytest.fail("forbidden request reached pipeline")
        yield {}

    with pytest.raises(ValueError):
        await anext(SearchHistory(store).stream(SearchScope(**_request()), run))
    assert not await store.get_events("conv_search")
    store.close()


def test_other_owner_cannot_read_or_run_saved_search(monkeypatch):
    monkeypatch.setenv("DISCO_AUTH_SECRET", "search-owner-test-secret")
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research", owner_id="owner-a")
    search = _FakeSearch()
    client = TestClient(
        create_app(store, runtime=_runtime(store, search)), base_url="http://localhost:8000"
    )
    token, _ = SessionSigner().mint(owner_id="owner-b", is_admin=False)
    client.cookies.set(SESSION_COOKIE, token)
    client.headers.update(
        {
            "Cookie": f"{SESSION_COOKIE}={token}",
            "Origin": "http://localhost:8088",
            "Host": "localhost:8000",
        }
    )
    assert client.get("/conversations/conv_search/research").status_code == 403
    assert _frames(client, _request(), dict(client.headers))[0]["type"] == "error"
    assert search.calls == 0
    store.close()


async def test_answer_and_terminal_status_commit_atomically(monkeypatch):
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research")
    original = store._store_one

    def fail_terminal(cid, event):
        if getattr(event, "status", None) == ConversationStatus.FINISHED:
            raise OSError("disk full")
        return original(cid, event)

    monkeypatch.setattr(store, "_store_one", fail_terminal)

    async def run():
        yield {"type": "final", "answer": _answer()}

    with pytest.raises(OSError, match="disk full"):
        _ = [frame async for frame in SearchHistory(store).stream(SearchScope(**_request()), run)]
    assert (await read_search_history(store, "conv_search"))["turns"] == []
    assert (await store.get_state("conv_search")).execution_status == ConversationStatus.ERROR
    store.close()


async def test_deleted_search_is_not_resurrected_by_late_final_or_cleanup():
    from disco.core import DEFAULT_OWNER_ID

    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research")

    async def run():
        await store.delete_conversation("conv_search", owner_id=DEFAULT_OWNER_ID)
        yield {"type": "final", "answer": _answer()}

    with pytest.raises(KeyError, match="deleted"):
        _ = [frame async for frame in SearchHistory(store).stream(SearchScope(**_request()), run)]
    assert not await store.conversation_exists("conv_search")
    assert not await store.get_events("conv_search")
    store.close()
