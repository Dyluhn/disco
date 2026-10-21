"""Real ASGI send failures must drain both conversation subscriptions."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from disco.agent_server.routes.ws import _drain_conversation_pumps, _run_conversation_ws
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from starlette.websockets import WebSocket

CID = "ws-cleanup"


class Peer:
    def __init__(self, fail_kind=None, error=OSError):
        self.incoming = asyncio.Queue()
        self.frames = []
        self.fail_kind = fail_kind
        self.error = error
        self.failed = asyncio.Event()
        self.received = asyncio.Queue()
        self.socket = WebSocket({"type": "websocket", "path": "/"}, self.receive, self.send)

    async def receive(self):
        return await self.incoming.get()

    async def send(self, message):
        if message["type"] != "websocket.send":
            return
        frame = json.loads(message["text"])
        if frame["type"] == self.fail_kind:
            self.failed.set()
            self.disconnect()
            raise self.error("transport closed" if self.error is OSError else "producer defect")
        self.frames.append(frame)
        self.received.put_nowait(frame)

    async def accept(self):
        self.incoming.put_nowait({"type": "websocket.connect"})
        await self.socket.accept()

    def disconnect(self):
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1006})


class Store(SqliteEventStore):
    def __init__(self):
        super().__init__(":memory:")
        self.ephemeral_ready = asyncio.Event()

    async def _subscribe_ephemeral(self, cid):
        stream = super()._subscribe_ephemeral(cid)
        self.ephemeral_ready.set()
        try:
            async for frame in stream:
                yield frame
        finally:
            await stream.aclose()


def runtime():
    rt = MagicMock()
    rt.lifecycle.sandbox_state.return_value = None
    rt.lifecycle.sandbox_instance_ids.return_value = []
    rt.settings.is_autonomous.return_value = False
    rt.settings.is_quiet.return_value = False
    rt.settings.is_assist.return_value = False
    rt.sandbox.backend_name.return_value = None
    return rt


def empty_subscriptions(store):
    assert not store._subscribers[CID]
    assert not store._eph_subscribers[CID]


@pytest.mark.parametrize("kind", ["state", "event", "file_stream"])
async def test_asgi_send_disconnect_releases_both_streams_and_notifies_viewer(kind):
    store = Store()
    store.create_conversation(CID)
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt, peer = runtime(), Peer(fail_kind=kind)
    await peer.accept()
    task = asyncio.create_task(_run_conversation_ws(store, rt, peer.socket, CID, 0))
    try:
        if kind == "file_stream":
            await asyncio.wait_for(store.ephemeral_ready.wait(), 1)
            store.publish_ephemeral(CID, {"tool": "file_write", "path": "x", "delta": "hello"})
        await asyncio.wait_for(task, 1)
        assert peer.failed.is_set()
        rt.connections.on_disconnect.assert_called_once_with(CID)
        empty_subscriptions(store)
        assert (await store.get_state(CID)).execution_status == ConversationStatus.RUNNING
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.parametrize("kind", ["state", "event", "file_stream"])
async def test_unexpected_producer_failure_is_preserved_after_full_cleanup(kind):
    store = Store()
    store.create_conversation(CID)
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt, peer = runtime(), Peer(fail_kind=kind, error=RuntimeError)
    await peer.accept()
    task = asyncio.create_task(_run_conversation_ws(store, rt, peer.socket, CID, 0))
    try:
        if kind == "file_stream":
            await asyncio.wait_for(store.ephemeral_ready.wait(), 1)
            store.publish_ephemeral(CID, {"tool": "file_write", "path": "x", "delta": "hello"})
        with pytest.raises(RuntimeError, match="producer defect"):
            await asyncio.wait_for(task, 1)
        rt.connections.on_disconnect.assert_called_once_with(CID)
        empty_subscriptions(store)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


async def test_parent_cancellation_drains_pumps_without_changing_running_task_state():
    store = Store()
    store.create_conversation(CID)
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt, peer = runtime(), Peer()
    await peer.accept()
    task = asyncio.create_task(_run_conversation_ws(store, rt, peer.socket, CID, 0))
    try:
        await asyncio.wait_for(store.ephemeral_ready.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rt.connections.on_disconnect.assert_called_once_with(CID)
        empty_subscriptions(store)
        assert (await store.get_state(CID)).execution_status == ConversationStatus.RUNNING
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


async def test_repeated_disconnect_and_reconnect_replays_only_later_durable_events():
    store = Store()
    store.create_conversation(CID)
    rt = runtime()
    cursor = 0
    try:
        for _ in range(3):
            event = await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
            peer = Peer()
            await peer.accept()
            task = asyncio.create_task(_run_conversation_ws(store, rt, peer.socket, CID, cursor))
            try:
                state = await asyncio.wait_for(peer.received.get(), 1)
                replay = await asyncio.wait_for(peer.received.get(), 1)
                assert state["type"] == "state"
                assert replay["event"]["id"] == event.id
                assert replay["event"]["seq"] > cursor
                peer.disconnect()
                await asyncio.wait_for(task, 1)
                empty_subscriptions(store)
                cursor = event.seq
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert rt.connections.on_disconnect.call_count == 3
    finally:
        store.close()


async def test_secondary_error_is_consumed_and_logged_without_overriding_primary(caplog):
    async def broken():
        raise ValueError("secondary producer error")

    broken_task = asyncio.create_task(broken())
    await asyncio.gather(broken_task, return_exceptions=True)
    pending_task = asyncio.create_task(asyncio.Event().wait())
    await _drain_conversation_pumps((broken_task, pending_task), primary_failed=True)
    assert pending_task.done() and broken_task.done()
    assert "secondary producer error" in caplog.text
