from __future__ import annotations

import asyncio
import urllib.parse

import pytest
from disco.agent_server.routes import preview as preview_routes
from disco.agent_server.routes.preview import make_preview_router
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from fastapi import FastAPI


class _FakeRuntime:
    def __init__(self, wake_result: str | None) -> None:
        self.wake_result = wake_result
        self.wake_calls: list[tuple[str, int]] = []
        self.project_store_called = False

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        self.wake_calls.append((cid8, port))
        return self.wake_result

    def project_store(self):
        self.project_store_called = True
        return None


class _FakeUpstreamWebSocket:
    def __init__(self, subprotocol: str | None) -> None:
        self.subprotocol = subprotocol
        self.sent: list[str | bytes] = []
        self._messages: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._closed = False

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)
        await self._messages.put(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        await self._messages.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


def _app(runtime: _FakeRuntime, store: SqliteEventStore | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(make_preview_router(store or SqliteEventStore(":memory:"), runtime))  # type: ignore[arg-type]
    return app


async def _start_ws(
    app: FastAPI, target: str, *, subprotocols: list[str] | None = None
) -> tuple[asyncio.Queue[dict], asyncio.Queue[dict], asyncio.Task]:
    parsed = urllib.parse.urlsplit(target)
    headers = [(b"host", b"testserver")]
    if subprotocols:
        headers.append((b"sec-websocket-protocol", ",".join(subprotocols).encode("latin1")))

    receive_queue: asyncio.Queue[dict] = asyncio.Queue()
    send_queue: asyncio.Queue[dict] = asyncio.Queue()
    await receive_queue.put({"type": "websocket.connect"})

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "scheme": "ws",
        "path": parsed.path,
        "raw_path": parsed.path.encode("latin1"),
        "query_string": parsed.query.encode("latin1"),
        "headers": headers,
        "subprotocols": subprotocols or [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    async def receive() -> dict:
        return await receive_queue.get()

    async def send(message: dict) -> None:
        await send_queue.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    return receive_queue, send_queue, task


async def _next_sent(send_queue: asyncio.Queue[dict]) -> dict:
    return await asyncio.wait_for(send_queue.get(), timeout=1.0)


async def _finish_ws(receive_queue: asyncio.Queue[dict], task: asyncio.Task) -> None:
    await receive_queue.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, timeout=1.0)


@pytest.fixture
def fake_ws_connect(monkeypatch):
    calls: list[dict[str, object]] = []
    clients: list[_FakeUpstreamWebSocket] = []

    async def _connect(url: str, subprotocols: list[str] | None = None):
        client = _FakeUpstreamWebSocket(
            "vite-hmr" if subprotocols and "vite-hmr" in subprotocols else None
        )
        calls.append({"url": url, "subprotocols": list(subprotocols or [])})
        clients.append(client)
        return client

    monkeypatch.setattr(preview_routes.websockets, "connect", _connect)
    return calls, clients


@pytest.mark.asyncio
async def test_preview_app_websocket_hmr_frames_pass_through(fake_ws_connect) -> None:
    calls, upstream_clients = fake_ws_connect
    runtime = _FakeRuntime("http://upstream.local:5173")
    app = _app(runtime)

    receive_queue, send_queue, task = await _start_ws(
        app,
        "/conversations/conv_abc12345deadbeef/preview-app/hmr?token=vite",
        subprotocols=["vite-hmr"],
    )

    assert await _next_sent(send_queue) == {
        "type": "websocket.accept",
        "subprotocol": "vite-hmr",
        "headers": [],
    }

    await receive_queue.put({"type": "websocket.receive", "text": "vite:ping"})
    assert await _next_sent(send_queue) == {"type": "websocket.send", "text": "vite:ping"}

    await receive_queue.put({"type": "websocket.receive", "bytes": b"vite-bytes"})
    assert await _next_sent(send_queue) == {"type": "websocket.send", "bytes": b"vite-bytes"}

    await _finish_ws(receive_queue, task)

    assert calls == [
        {"url": "ws://upstream.local:5173/hmr?token=vite", "subprotocols": ["vite-hmr"]}
    ]
    assert upstream_clients[0].sent == ["vite:ping", b"vite-bytes"]
    assert runtime.wake_calls == [("abc12345", 8000)]
    assert runtime.project_store_called is False


@pytest.mark.asyncio
async def test_port_websocket_hmr_uses_requested_user_port(fake_ws_connect) -> None:
    calls, upstream_clients = fake_ws_connect
    runtime = _FakeRuntime("http://upstream.local:5173")
    app = _app(runtime)

    receive_queue, send_queue, task = await _start_ws(
        app,
        "/conversations/conv_feedface9999/port/5173/hmr",
        subprotocols=["vite-hmr"],
    )

    assert (await _next_sent(send_queue))["type"] == "websocket.accept"
    await receive_queue.put({"type": "websocket.receive", "text": "hmr update"})
    assert await _next_sent(send_queue) == {"type": "websocket.send", "text": "hmr update"}
    await _finish_ws(receive_queue, task)

    assert calls == [{"url": "ws://upstream.local:5173/hmr", "subprotocols": ["vite-hmr"]}]
    assert upstream_clients[0].sent == ["hmr update"]
    assert runtime.wake_calls == [("feedface", 5173)]
    assert runtime.project_store_called is False


@pytest.mark.asyncio
async def test_preview_app_websocket_wake_miss_does_not_use_static_snapshot() -> None:
    runtime = _FakeRuntime(None)
    app = _app(runtime)

    _receive_queue, send_queue, task = await _start_ws(
        app,
        "/conversations/conv_deadbeef0000/preview-app/hmr?token=vite",
        subprotocols=["vite-hmr"],
    )
    assert await _next_sent(send_queue) == {
        "type": "websocket.close",
        "code": 1008,
        "reason": "preview not available",
    }
    await asyncio.wait_for(task, timeout=1.0)

    assert runtime.wake_calls == [("deadbeef", 8000)]
    assert runtime.project_store_called is False


@pytest.mark.asyncio
async def test_finished_preview_websocket_never_wakes_an_unsealed_runtime() -> None:
    runtime = _FakeRuntime("http://replacement.local:8000")
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_finished0000"
    store.create_conversation(conversation_id)
    await store.append(
        conversation_id,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    app = _app(runtime, store)

    _receive_queue, send_queue, task = await _start_ws(
        app,
        f"/conversations/{conversation_id}/preview-app/hmr",
        subprotocols=["vite-hmr"],
    )

    assert await _next_sent(send_queue) == {
        "type": "websocket.close",
        "code": 1008,
        "reason": "preview not available",
    }
    await asyncio.wait_for(task, timeout=1.0)
    assert runtime.wake_calls == []
    assert runtime.project_store_called is True
