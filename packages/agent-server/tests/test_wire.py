"""Streaming/wire + REST tests — event-state-contract.md §8.6 and §7.5.

Headless: Starlette's TestClient drives the ASGI app in-process (no network, no
external services). Each test gets a fresh in-memory store via create_app.
"""

from __future__ import annotations

import pytest
import httpx
from disco.agent_server import create_app
from disco.core import SqliteEventStore
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    return TestClient(create_app(store))


def _create(client: TestClient, owner_id: str = "local") -> str:
    resp = client.post("/conversations", json={"owner_id": owner_id})
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


# ---- §7.5 REST surface ------------------------------------------------------


def test_create_returns_id_and_url(client):
    body = client.post("/conversations", json={}).json()
    assert body["conversation_id"].startswith("conv_")
    assert body["conversation_url"].endswith(body["conversation_id"])


def test_post_message_then_history_and_state(client):
    cid = _create(client)
    r = client.post(f"/conversations/{cid}/messages", json={"content": "hello"})
    assert r.json()["seq"] == 1

    events = client.get(f"/conversations/{cid}/events").json()
    assert len(events["events"]) == 1
    assert events["events"][0]["kind"] == "message"
    assert events["events"][0]["message"]["content"] == "hello"

    state = client.get(f"/conversations/{cid}/state").json()
    assert state["conversation_id"] == cid
    # A USER message resets iteration to 0 (event contract §3).
    assert state["iteration"] == 0


def test_events_pagination(client):
    cid = _create(client)
    for i in range(5):
        client.post(f"/conversations/{cid}/messages", json={"content": f"m{i}"})
    page1 = client.get(f"/conversations/{cid}/events?limit=2").json()
    assert [e["seq"] for e in page1["events"]] == [1, 2]
    assert page1["next_cursor"] == 2
    page2 = client.get(f"/conversations/{cid}/events?after_seq=2&limit=2").json()
    assert [e["seq"] for e in page2["events"]] == [3, 4]


@pytest.mark.asyncio
async def test_list_conversations_is_owner_scoped():
    store = SqliteEventStore(":memory:")
    transport = httpx.ASGITransport(app=create_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/conversations", json={"owner_id": "alice"})
        assert resp.status_code == 200
        a = resp.json()["conversation_id"]
        resp = await client.post("/conversations", json={"owner_id": "bob"})
        assert resp.status_code == 200
        alice = (await client.get("/conversations?owner_id=alice")).json()["conversation_ids"]
    assert alice == [a]  # §6.1: no cross-owner data


# ---- §8.6 streaming / wire --------------------------------------------------


def test_reconnect_replays_state_then_events_after_last_seq_then_live(client):
    cid = _create(client)
    # Seed three messages (seqs 1,2,3) before connecting.
    for i in range(3):
        client.post(f"/conversations/{cid}/messages", json={"content": f"m{i}"})

    # Reconnect with last_seq=1 → state frame, then exactly events seq>1, then live.
    with client.websocket_connect(f"/ws/conversations/{cid}?last_seq=1") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        assert first["state"]["last_seq"] == 3

        replayed = [ws.receive_json() for _ in range(2)]
        assert [f["type"] for f in replayed] == ["event", "event"]
        assert [f["event"]["seq"] for f in replayed] == [2, 3]  # only seq > 1

        # A live message appended after connect arrives as the next event frame.
        client.post(f"/conversations/{cid}/messages", json={"content": "live"})
        live = ws.receive_json()
        assert live["type"] == "event"
        assert live["event"]["seq"] == 4
        assert live["event"]["message"]["content"] == "live"


def test_pending_message_with_no_socket_is_applied_on_connect(client):
    cid = _create(client)
    # A message delivered with NO socket (the §7.4 pending path = the durable log).
    client.post(f"/conversations/{cid}/messages", json={"content": "queued while away"})

    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ev = ws.receive_json()
        assert ev["type"] == "event"
        assert ev["event"]["message"]["content"] == "queued while away"


def test_send_message_over_ws_is_echoed_as_event(client):
    cid = _create(client)
    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "send_message", "content": "hi from ws"})
        ev = ws.receive_json()
        assert ev["type"] == "event"
        assert ev["event"]["source"] == "user"
        assert ev["event"]["message"]["content"] == "hi from ws"


def test_file_stream_ephemeral_payload_is_normalized_for_ws_frame():
    from disco.agent_server.routes.ws import _file_stream_payload
    from disco.core import WSServerFrame

    payload = _file_stream_payload(
        {
            "type": "file_stream",
            "tool": "file_edit",
            "path": "src/App.tsx",
            "index": "0",
            "delta": "return <main>Live</main>;\n",
            "field": "new",
        }
    )

    frame = WSServerFrame(type="file_stream", file_stream=payload).model_dump(mode="json")
    assert frame["type"] == "file_stream"
    assert frame["file_stream"] == {
        "tool": "file_edit",
        "path": "src/App.tsx",
        "index": 0,
        "delta": "return <main>Live</main>;\n",
        "field": "new",
    }


def test_malformed_frame_gets_error_and_socket_survives(client):
    cid = _create(client)
    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        # Schema-invalid frame (unknown type) → transport error frame, no crash.
        ws.send_json({"type": "not_a_real_frame"})
        err = ws.receive_json()
        assert err["type"] == "error"
        # The socket still works afterward: ping → pong.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_ping_pong(client):
    cid = _create(client)
    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_non_json_text_frame_gets_error_and_socket_survives(client):
    cid = _create(client)
    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_text("this is not json {{{")
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "not JSON" in err["error"]["detail"]
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # still alive


def test_steer_over_ws_is_recorded_as_user_message(client):
    cid = _create(client)
    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "steer", "steer_text": "focus on Y instead"})
        ev = ws.receive_json()
        assert ev["type"] == "event"
        assert ev["event"]["source"] == "user"
        assert ev["event"]["message"]["content"] == "focus on Y instead"
        assert ev["event"]["meta"]["steer"] is True
