"""A stopped Search must release in-flight work before another frame arrives."""

from __future__ import annotations

import asyncio
import json

import pytest
from disco.agent_server.routes.ws import _send_search_until_disconnect
from disco.agent_server.search_history import SearchHistory, SearchScope, read_search_history
from disco.core import ConversationStatus, SqliteEventStore


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.frames = []
        self.sent = asyncio.Event()

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, text):
        self.frames.append(json.loads(text))
        self.sent.set()

    async def send_json(self, frame):
        self.frames.append(frame)
        self.sent.set()


async def test_disconnect_cancels_waiting_inference_and_releases_conversation():
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_search", surface="research")
    history = SearchHistory(store)
    socket = Socket()
    cancelled = asyncio.Event()

    async def blocked():
        try:
            yield {"type": "state", "status": "running"}
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scope = SearchScope(query="first", request_id="first", conversation_id="conv_search")
    task = asyncio.create_task(
        _send_search_until_disconnect(socket, history.stream(scope, blocked))
    )
    await asyncio.wait_for(socket.sent.wait(), 1)
    assert history.active == {"conv_search"}
    await socket.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set()
    assert not history.active
    assert (await store.get_state("conv_search")).execution_status == ConversationStatus.ERROR
    assert not (await read_search_history(store, "conv_search"))["turns"]

    async def complete():
        yield {"type": "final", "answer": {"blocks": [], "query": "retry"}}
        yield {"type": "state", "status": "finished"}

    retry = scope.model_copy(update={"request_id": "retry", "query": "retry"})
    await asyncio.wait_for(
        _send_search_until_disconnect(Socket(), history.stream(retry, complete)), 1
    )
    assert not history.active
    assert len((await read_search_history(store, "conv_search"))["turns"]) == 1
    assert (await store.get_state("conv_search")).execution_status == ConversationStatus.FINISHED
    store.close()


async def test_sender_failure_closes_generator_and_receiver():
    socket = Socket()
    closed = asyncio.Event()

    async def failed():
        try:
            yield {"type": "state", "status": "running"}
            raise RuntimeError("upstream failed")
        finally:
            closed.set()

    with pytest.raises(RuntimeError, match="upstream failed"):
        await asyncio.wait_for(_send_search_until_disconnect(socket, failed()), 1)
    assert closed.is_set()
    assert not socket.incoming._getters


async def test_parent_cancellation_drains_waiting_generator_and_socket_reader():
    socket = Socket()
    closed = asyncio.Event()

    async def blocked():
        try:
            yield {"type": "state", "status": "running"}
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(_send_search_until_disconnect(socket, blocked()))
    await asyncio.wait_for(socket.sent.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not socket.incoming._getters
