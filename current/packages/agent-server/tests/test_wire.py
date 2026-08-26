"""Streaming/wire + REST tests — event-state-contract.md §8.6 and §7.5.

Headless: Starlette's TestClient drives the ASGI app in-process (no network, no
external services). Each test gets a fresh in-memory store via create_app.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from disco.agent_server import create_app
from disco.agent_server.routes.ws import _handle_steer_frame
from disco.core import SqliteEventStore, WSClientFrame
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


def test_explicit_zero_cursor_replays_complete_history(client):
    cid = _create(client)
    for i in range(3):
        client.post(f"/conversations/{cid}/messages", json={"content": f"history-{i}"})

    with client.websocket_connect(f"/ws/conversations/{cid}?last_seq=0") as ws:
        assert ws.receive_json()["type"] == "state"
        replayed = [ws.receive_json() for _ in range(3)]

    assert [frame["event"]["seq"] for frame in replayed] == [1, 2, 3]


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
    # `agent_view_id` is a first-class FileStreamFrame field (core/wire.py) with no
    # `exclude_none`, so a bare model_dump — the SAME one the live ws send path uses
    # (routes/ws.py:_file_stream_payload → WSServerFrame → model_dump) — always
    # carries the key. This payload declares no agent_view_id, so the
    # normalized-and-serialized value is None (the absent-value contract). The
    # frontend gates watch-it-write deltas on this key (useBuildStream.ts:
    # `if (fs.agent_view_id !== activeAgentViewId) return state`), so it must stay
    # on the wire — this stays an EXACT dict rather than dropping/broadening the key.
    assert frame["file_stream"] == {
        "tool": "file_edit",
        "path": "src/App.tsx",
        "index": 0,
        "delta": "return <main>Live</main>;\n",
        "field": "new",
        "agent_view_id": None,
    }


def test_file_stream_payload_normalizes_and_serializes_agent_view_id():
    """Positive: a real agent_view_id is normalized onto the typed frame and
    serialized on the wire verbatim (the multi-view routing field the frontend
    gates on). Mirrors the exact-dict shape of the ephemeral test above but for the
    POPULATED case — same construction the live ws send path uses (routes/ws.py:
    _file_stream_payload → WSServerFrame → model_dump). Index/field normalization
    (`"0"→0`, `field="new"`) still hold alongside the id."""
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
            "agent_view_id": "aview_v2",
        }
    )

    frame = WSServerFrame(type="file_stream", file_stream=payload).model_dump(mode="json")
    assert frame["file_stream"] == {
        "tool": "file_edit",
        "path": "src/App.tsx",
        "index": 0,
        "delta": "return <main>Live</main>;\n",
        "field": "new",
        "agent_view_id": "aview_v2",
    }


def test_file_stream_payload_agent_view_id_absent_or_falsy_is_none():
    """Negative/contract: _file_stream_payload maps absent, empty, and other falsy
    agent_view_id inputs to None (never a bare '' on the wire) and coerces a
    truthy non-string id to str — the exact current normalization contract
    (routes/ws.py:55). None is still serialized as an explicit key (the frame has
    no exclude_none), so the frontend gate always sees the field."""
    from disco.agent_server.routes.ws import _file_stream_payload
    from disco.core import WSServerFrame

    base = {"type": "file_stream", "tool": "file_write", "path": "p", "index": 0, "delta": "d"}

    # absent key → None
    assert _file_stream_payload(base).agent_view_id is None
    # explicit falsy values → None (not "" and not 0)
    assert _file_stream_payload({**base, "agent_view_id": ""}).agent_view_id is None
    assert _file_stream_payload({**base, "agent_view_id": None}).agent_view_id is None
    assert _file_stream_payload({**base, "agent_view_id": 0}).agent_view_id is None
    # truthy non-string → coerced to str; a real id is preserved verbatim
    assert _file_stream_payload({**base, "agent_view_id": 123}).agent_view_id == "123"
    assert _file_stream_payload({**base, "agent_view_id": "aview_x"}).agent_view_id == "aview_x"

    # the absent-value None is a serialized wire key, not a dropped one
    absent = WSServerFrame(type="file_stream", file_stream=_file_stream_payload(base)).model_dump(
        mode="json"
    )
    assert "agent_view_id" in absent["file_stream"]
    assert absent["file_stream"]["agent_view_id"] is None


def test_file_stream_agent_view_id_round_trips_on_live_ws_path():
    """Positive (live path): a real agent_view_id on an ephemeral file_stream
    survives the actual production send path — routes/ws.py drains the ephemeral
    bus → _file_stream_payload → WSServerFrame → _send_json_redacted — to the exact
    bytes the client receives. The consumer quarantines deltas whose agent_view_id
    != activeAgentViewId (useBuildStream.ts:205), so this field must reach the
    wire; dropping it (e.g. exclude_none) would suppress every delta in a
    multi-view session. A non-secret id is unaffected by redaction."""
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store))
    cid = _create(client)

    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        store.publish_ephemeral(
            cid,
            {
                "type": "file_stream",
                "tool": "file_edit",
                "path": "src/App.tsx",
                "index": "0",
                "delta": "return <main>Live</main>;\n",
                "field": "new",
                "agent_view_id": "aview_live",
            },
        )
        frame = ws.receive_json()

    assert frame["type"] == "file_stream"
    assert frame["file_stream"]["agent_view_id"] == "aview_live"
    # normalization still holds end-to-end on the live path
    assert frame["file_stream"]["index"] == 0
    assert frame["file_stream"]["field"] == "new"
    assert frame["file_stream"]["path"] == "src/App.tsx"
    assert frame["file_stream"]["delta"] == "return <main>Live</main>;\n"


def test_file_stream_ephemeral_frame_is_redacted_on_live_ws_path():
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store))
    cid = _create(client)
    secret = "OPENAI_API_KEY=sk_live_1234567890abcdefghijklmnop"

    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        store.publish_ephemeral(
            cid,
            {
                "type": "file_stream",
                "tool": "file_write",
                "path": "secrets.txt",
                "index": 0,
                "delta": secret,
                "field": "content",
            },
        )
        frame = ws.receive_json()

    serialized = json.dumps(frame)
    assert frame["type"] == "file_stream"
    assert secret not in serialized
    assert "REDACTED" in serialized


def test_persisted_event_frame_is_redacted_on_live_ws_path():
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store))
    cid = _create(client)
    secret = "OPENAI_API_KEY=sk_live_1234567890abcdefghijklmnop"
    posted = client.post(f"/conversations/{cid}/messages", json={"content": secret})
    assert posted.status_code == 200, posted.text

    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        assert ws.receive_json()["type"] == "state"
        frame = ws.receive_json()

    serialized = json.dumps(frame)
    assert frame["type"] == "event"
    assert secret not in serialized
    assert "REDACTED" in serialized


def test_research_stream_frames_are_redacted_on_live_ws_path():
    store = SqliteEventStore(":memory:")
    secret = "OPENAI_API_KEY=sk_live_1234567890abcdefghijklmnop"

    async def fake_stream(*_args, **_kwargs) -> AsyncIterator[dict]:
        yield {"type": "token", "token": secret}
        yield {"type": "final", "answer": {"markdown": secret}}

    fake_runtime = mock.MagicMock()
    fake_runtime.deep_research.research_stream = mock.MagicMock(side_effect=fake_stream)
    fake_runtime.settings.model_binding.get_last_selected_model = mock.MagicMock(return_value=None)
    client = TestClient(create_app(store, runtime=fake_runtime))

    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "redaction check"})
        token_frame = ws.receive_json()
        final_frame = ws.receive_json()

    serialized = json.dumps([token_frame, final_frame])
    assert secret not in serialized
    assert "REDACTED" in serialized


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


@pytest.mark.asyncio
async def test_steer_route_preserves_id_and_old_clients_omit_it():
    store = SqliteEventStore(":memory:")
    enqueue = mock.Mock(return_value=True)
    runtime = SimpleNamespace(deep_research=SimpleNamespace(enqueue_steer=enqueue))

    await _handle_steer_frame(
        store,
        "conv_route",
        WSClientFrame(type="steer", steer_text="focus on Y instead"),
        runtime,
    )
    await _handle_steer_frame(
        store,
        "conv_route",
        WSClientFrame(type="steer", steer_text="focus on Z", steer_id="steer-9"),
        runtime,
    )

    assert enqueue.call_args_list == [
        mock.call("conv_route", "focus on Y instead", None),
        mock.call("conv_route", "focus on Z", "steer-9"),
    ]
