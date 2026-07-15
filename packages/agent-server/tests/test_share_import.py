"""rp-06 residue — share-bundle IMPORT: round-trip fidelity, the fail-closed
validation gates, re-scrub on ingest, the read-only server-edge guards, and the
origin marker. A bundle is untrusted third-party data, so the security properties
(read-only enforcement, re-scrub) are asserted as hard behavior, not trusted.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import MessageEvent, SqliteEventStore, StatusEvent
from disco.core.events import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    c = TestClient(create_app(store, runtime=runtime))
    c._store = store  # type: ignore[attr-defined]
    return c


def _seed(store: SqliteEventStore, cid: str) -> None:
    """A small build-like log: user msg → action → paired observation → finished."""

    async def go() -> None:
        store.create_conversation(cid, owner_id="local", title="My build", surface="build")
        await store.append(
            cid,
            MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hi")),
        )
        act = await store.append(
            cid,
            ActionEvent(
                source=EventSource.AGENT,
                thought="writing the file",
                tool_call=ToolCall(tool_name="file_write", arguments={"path": "a.txt"}),
            ),
        )
        await store.append(
            cid,
            ObservationEvent(
                action_id=act.id,
                tool_result=ToolResult(
                    call_id="c1", tool_name="file_write", success=True, content="wrote"
                ),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))

    asyncio.run(go())


def _export(client: TestClient, cid: str) -> dict:
    r = client.get(f"/api/conversations/{cid}/share/bundle")
    assert r.status_code == 200, r.text
    return r.json()


# ---- round-trip + provenance ------------------------------------------------


def test_export_then_import_round_trips_read_only(client: TestClient) -> None:
    _seed(client._store, "conv_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_src")

    r = client.post("/api/share/import", json=bundle)
    assert r.status_code == 200, r.text
    new_cid = r.json()["conversation_id"]
    assert new_cid != "conv_src"  # importer-minted, never the bundle's cid

    events = client.get(f"/conversations/{new_cid}/events").json()["events"]
    # same number of events; action/observation pairing preserved (ids kept)
    assert len(events) == len(bundle["events"])
    action = next(e for e in events if e["kind"] == "action")
    obs = next(e for e in events if e["kind"] == "observation")
    assert obs["action_id"] == action["id"]
    # provenance: imported + read-only (the origin marker the guards key on)
    assert client._store.conversation_origin(new_cid) == "imported"  # type: ignore[attr-defined]


def test_imported_conversation_is_read_only_at_every_kick_path(client: TestClient) -> None:
    _seed(client._store, "conv_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_src")
    cid = client.post("/api/share/import", json=bundle).json()["conversation_id"]

    # every loop-kicking / mutating HTTP path must 409 with imported_read_only
    assert client.post(f"/conversations/{cid}/messages", json={"content": "go"}).status_code == 409
    assert client.post(f"/conversations/{cid}/followup", json={"content": "go"}).status_code == 409
    assert client.post(f"/conversations/{cid}/resume").status_code == 409
    sched = client.post(
        f"/api/conversations/{cid}/schedules", json={"rrule": "0 9 * * *", "description": "x"}
    )
    assert sched.status_code == 409
    msg = client.post(f"/conversations/{cid}/messages", json={"content": "go"}).json()
    assert msg["detail"]["reason"] == "imported_read_only"


# ---- fail-closed validation -------------------------------------------------


def test_unsupported_version_422(client: TestClient) -> None:
    _seed(client._store, "conv_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_src")
    bundle["bundle_version"] = 2
    r = client.post("/api/share/import", json=bundle)
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "unsupported_bundle_version"


def test_malformed_event_rejects_whole_bundle(client: TestClient) -> None:
    _seed(client._store, "conv_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_src")
    bundle["events"].append({"kind": "not_a_real_kind", "smuggled": "field"})
    before = len(client.get("/conversations?owner_id=local").json()["conversation_ids"])
    r = client.post("/api/share/import", json=bundle)
    assert r.status_code == 422
    # no partial import — no new conversation row was created
    after = len(client.get("/conversations?owner_id=local").json()["conversation_ids"])
    assert after == before


def test_no_events_422(client: TestClient) -> None:
    body = {"bundle_version": 1, "events": [], "surface": "build"}
    r = client.post("/api/share/import", json=body)
    assert r.status_code == 422


# ---- re-scrub on ingest (exporter scrubbing is a claim, not a property) ------


def test_import_rescrubs_secrets(client: TestClient) -> None:
    """A doctored bundle carrying an un-scrubbed secret must be redacted on import —
    so this instance can never re-export someone else's leaked credential."""
    _seed(client._store, "conv_src")  # type: ignore[attr-defined]
    bundle = copy.deepcopy(_export(client, "conv_src"))
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"  # GitHub PAT shape
    # splice it into a message event's content (simulating an un-scrubbed bundle)
    for e in bundle["events"]:
        if e["kind"] == "message":
            e["message"]["content"] = f"my token is {secret}"
            break
    cid = client.post("/api/share/import", json=bundle).json()["conversation_id"]
    events = client.get(f"/conversations/{cid}/events").json()["events"]
    blob = str(events)
    assert secret not in blob  # the raw secret never lands
    assert "REDACTED" in blob  # it was replaced, not dropped
