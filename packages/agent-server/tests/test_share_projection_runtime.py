"""The share-bundle cassette projection must come from the installed runtime.

Production export failed with HTTP500 because `share_service` imported the
development-only `harness.projection` package, absent from published images.
The projection now lives in `disco.core.share_projection`; the harness
delegates to it. These tests pin the single-source contract behaviorally:
bundle cassettes equal the runtime projection and load into the harness
Cassette the replay path reads.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import EventSource, LLMMessage, MessageEvent, SqliteEventStore, StatusEvent
from disco.core.events import (
    ActionEvent,
    ConversationStatus,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from fastapi.testclient import TestClient


def test_harness_projection_delegates_to_the_runtime_helper() -> None:
    from disco.core.share_projection import project_cassette_rows as runtime_fn
    from harness.projection import project_cassette_rows as harness_fn

    assert harness_fn is runtime_fn


def test_cassette_key_matches_between_runtime_and_harness() -> None:
    from disco.core.share_projection import cassette_key as runtime_key
    from harness.cassette import cassette_key as harness_key

    payload = {"query": "what is vcrpy", "limit": 3, "deny": []}
    assert runtime_key("search", payload) == harness_key("search", payload)


def test_exported_cassette_equals_the_runtime_projection() -> None:
    """Seed a paired search call, export, and check the bundle's `cassette`
    is exactly what the runtime helper projects from the bundle's events —
    and that the harness Cassette loads it for replay lookup."""
    from disco.core.share_projection import project_cassette_rows
    from harness.cassette import Cassette

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    client = TestClient(create_app(store, runtime=runtime))
    cid = "conv_runtime_projection"

    async def go() -> None:
        store.create_conversation(cid, owner_id="local", surface="deep_research")
        await store.append(
            cid,
            MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="q")),
        )
        action = await store.append(
            cid,
            ActionEvent(
                source=EventSource.AGENT,
                thought="search the web",
                tool_call=ToolCall(
                    tool_name="ddgs.search",
                    arguments={"query": "what is vcrpy", "limit": 3, "deny": []},
                ),
            ),
        )
        await store.append(
            cid,
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(
                    call_id="c1",
                    tool_name="ddgs.search",
                    success=True,
                    content="Search result",
                    structured={"results": [{"url": "https://example.com"}]},
                ),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))

    asyncio.run(go())
    bundle = client.get(f"/api/conversations/{cid}/share/bundle").json()
    assert bundle["bundle_version"] == 1
    assert bundle["cassette"] == project_cassette_rows(bundle["events"])
    cas = Cassette.from_rows(bundle["cassette"])
    row = bundle["cassette"][0]
    assert cas.lookup(row["seam"], row["input"]) == row["output"]


def test_export_works_when_development_harness_cannot_be_imported(monkeypatch):
    import builtins

    importer = builtins.__import__

    def without_harness(name, *args, **kwargs):
        if name == "harness" or name.startswith("harness."):
            raise ModuleNotFoundError("development harness is not installed")
        return importer(name, *args, **kwargs)

    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_installed", owner_id="local", surface="agent")
    runtime = ConversationRuntime(store)
    client = TestClient(create_app(store, runtime=runtime))
    monkeypatch.setattr(builtins, "__import__", without_harness)
    response = client.get("/api/conversations/conv_installed/share/bundle")
    assert response.status_code == 200, response.text
    assert response.json()["cassette"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "q", "limit": "many"},
        {"query": "q", "deny": ["host", {"invalid": "shape"}]},
    ],
)
def test_rejected_tool_arguments_do_not_break_bundle_projection(arguments):
    from disco.core.share_projection import project_cassette_rows

    events = [
        {
            "kind": "action",
            "id": "bad_call",
            "tool_call": {"tool_name": "search", "arguments": arguments},
        },
        {
            "kind": "observation",
            "action_id": "bad_call",
            "tool_result": {"success": False, "content": "invalid arguments"},
        },
    ]
    assert project_cassette_rows(events) == []
