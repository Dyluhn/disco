"""Real upstream transports exercise reserved ASGI closes and repeated sessions."""

import asyncio

import pytest
import websockets
from disco.agent_server.host_proxy_parts import _websocket as proxy
from websockets.frames import Close


async def _start(upstream):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(proxy._proxy_websocket(
        None,
        {"path": "/websockify", "query_string": b"", "headers": [], "subprotocols": []},
        incoming.get,
        outgoing.put,
        upstream,
    ))
    return incoming, outgoing, task


async def _next(outgoing):
    message = await asyncio.wait_for(outgoing.get(), 2)
    if message["type"] == "websocket.close":
        Close(message["code"], message.get("reason", "")).serialize()
    return message


@pytest.mark.parametrize("code,expected", [(1000, 1000), (1001, 1001), (1005, 1000),
                                           (1006, 1011), (1015, 1011), (4001, 4001)])
async def test_asgi_disconnect_closes_real_upstream_with_wire_valid_code(code, expected):
    observed = asyncio.Queue()

    async def echo(connection):
        try:
            async for message in connection:
                await connection.send(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            await observed.put(connection.close_code)

    async with websockets.serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        incoming, outgoing, task = await _start(f"http://127.0.0.1:{port}")
        assert (await _next(outgoing))["type"] == "websocket.accept"
        await incoming.put({"type": "websocket.receive", "bytes": b"RFB acceptance"})
        assert await _next(outgoing) == {"type": "websocket.send", "bytes": b"RFB acceptance"}
        await incoming.put({"type": "websocket.disconnect", "code": code})
        await asyncio.wait_for(task, 2)
        assert await asyncio.wait_for(observed.get(), 2) == expected
        assert outgoing.empty(), "Disconnected ASGI clients must not receive a second close"


@pytest.mark.parametrize("code", [1000, 1001, 4004])
async def test_real_upstream_close_preserves_code_and_reason(code):
    async def close(connection):
        await connection.close(code, "upstream done")

    async with websockets.serve(close, "127.0.0.1", 0) as server:
        incoming, outgoing, task = await _start(
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        )
        assert (await _next(outgoing))["type"] == "websocket.accept"
        assert await _next(outgoing) == {
            "type": "websocket.close", "code": code, "reason": "upstream done",
        }
        await asyncio.wait_for(task, 2)
        assert incoming.empty()


async def test_abrupt_upstream_loss_sends_valid_error_and_allows_reconnection():
    connections = 0

    async def upstream(connection):
        nonlocal connections
        connections += 1
        await connection.recv()
        if connections == 1:
            connection.transport.abort()
        else:
            await connection.send(b"RFB second session")
            await connection.wait_closed()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        incoming, outgoing, task = await _start(base)
        assert (await _next(outgoing))["type"] == "websocket.accept"
        await incoming.put({"type": "websocket.receive", "text": "first"})
        assert (await _next(outgoing))["code"] == 1011
        await asyncio.wait_for(task, 2)
        incoming, outgoing, task = await _start(base)
        assert (await _next(outgoing))["type"] == "websocket.accept"
        await incoming.put({"type": "websocket.receive", "text": "second"})
        assert (await _next(outgoing))["bytes"] == b"RFB second session"
        await incoming.put({"type": "websocket.disconnect", "code": 1005})
        await asyncio.wait_for(task, 2)
        assert connections == 2


async def test_proxy_cancellation_consumes_both_pumps_and_closes_real_upstream():
    closed = asyncio.Event()

    async def upstream(connection):
        await connection.wait_closed()
        closed.set()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        _, outgoing, task = await _start(
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        )
        assert (await _next(outgoing))["type"] == "websocket.accept"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
        assert not [t for t in asyncio.all_tasks() if not t.done() and any(
            name in t.get_coro().__qualname__
            for name in ("client_to_upstream", "upstream_to_client")
        )]


async def test_connect_failure_uses_valid_close_code(monkeypatch):
    async def reject(*_args, **_kwargs):
        raise OSError("controlled connect failure")

    monkeypatch.setattr(proxy.websockets, "connect", reject)
    _, outgoing, task = await _start("http://unreachable.invalid")
    assert (await _next(outgoing))["code"] == 1011
    await asyncio.wait_for(task, 2)


async def test_receive_failure_is_consumed_and_closes_the_other_pump():
    closed = asyncio.Event()

    async def upstream(connection):
        await connection.wait_closed()
        closed.set()

    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "websocket.connect"}
        raise RuntimeError("controlled ASGI receive failure")

    outgoing = asyncio.Queue()
    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        await asyncio.wait_for(proxy._proxy_websocket(
            None, {"path": "/websockify", "headers": []}, receive, outgoing.put,
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
        ), 2)
        assert (await _next(outgoing))["type"] == "websocket.accept"
        assert (await _next(outgoing))["code"] == 1011
        await asyncio.wait_for(closed.wait(), 2)
        assert not [t for t in asyncio.all_tasks() if not t.done() and any(
            name in t.get_coro().__qualname__
            for name in ("client_to_upstream", "upstream_to_client")
        )]
