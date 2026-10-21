"""Cross-namespace kernel transport: real HTTP/WS bytes, no external daemon."""

from __future__ import annotations

import asyncio
import socket
import struct
from types import SimpleNamespace

import httpx
import pytest
from disco.tools.sandbox._container_parts.kernel_tunnel import (
    DockerExecStream,
    KernelExecTunnel,
)
from disco.tools.sandbox.base import SandboxError
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


class _SocketExec:
    """Fake daemon exec channel; a real socket carries the guest endpoint bytes."""

    def __init__(self, address):
        self.socket = socket.create_connection(address, timeout=2)
        self.socket.settimeout(None)
        self.closed = False

    def read(self):
        return self.socket.recv(65536)

    def write(self, data):
        self.socket.sendall(data)

    def close(self):
        self.closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


async def test_tunnel_relays_authenticated_http_and_websocket_and_closes_sessions():
    streams = []

    async def auth(connection, request):
        if request.headers.get("Authorization") != "token test-private-key":
            return connection.respond(401, "Unauthorized")
        if request.path == "/api":
            return connection.respond(200, '{"version":"test"}')

    async def echo(websocket):
        async for data in websocket:
            await websocket.send(data)

    async with serve(echo, "127.0.0.1", 0, process_request=auth) as upstream:
        destination = upstream.sockets[0].getsockname()

        def opener():
            stream = _SocketExec(destination)
            streams.append(stream)
            return stream

        tunnel = KernelExecTunnel(opener)
        try:
            host, port = tunnel.forward(8899)
            assert host == "127.0.0.1"
            assert port != destination[1]
            async with httpx.AsyncClient(trust_env=False) as client:
                denied = await client.get(f"http://{host}:{port}/api")
                assert denied.status_code == 401
                ready = await client.get(
                    f"http://{host}:{port}/api",
                    headers={"Authorization": "token test-private-key"},
                )
                assert ready.status_code == 200
                assert ready.json() == {"version": "test"}
            async with connect(
                f"ws://{host}:{port}/api/kernels/test/channels",
                additional_headers={"Authorization": "token test-private-key"},
                proxy=None,
            ) as websocket:
                payload = b"kernel-frame\x00" * 12000
                await websocket.send(payload)
                assert await websocket.recv() == payload
            await asyncio.to_thread(tunnel.close)
            for _ in range(30):
                if streams and all(stream.closed for stream in streams):
                    break
                await asyncio.sleep(0.01)
            assert streams and all(stream.closed for stream in streams)
            with pytest.raises(OSError):
                await asyncio.to_thread(socket.create_connection, (host, port), 0.2)
        finally:
            await asyncio.to_thread(tunnel.close)


def test_tunnel_refuses_arbitrary_ports_and_does_not_open_exec_until_used():
    calls = []
    tunnel = KernelExecTunnel(lambda: calls.append(True))
    try:
        for port in [22, 80, 8000, 8898, 8900, -1]:
            with pytest.raises(SandboxError):
                tunnel.forward(port)
        assert calls == []
        address = tunnel.forward(8899)
        assert address[0] == "127.0.0.1"
        assert tunnel.forward(8899) == address
    finally:
        tunnel.close()
    with pytest.raises(SandboxError):
        tunnel.forward(8899)


def test_docker_exec_demultiplexes_stdout_and_keeps_stdin_raw():
    parent, guest = socket.socketpair()
    calls = []

    def exec_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(output=parent)

    stream = DockerExecStream(SimpleNamespace(exec_run=exec_run))
    try:
        payload = b"HTTP/1.1 200 OK\r\n\r\nhello\x00world"
        guest.sendall(struct.pack(">BxxxI", 1, len(payload)) + payload)
        assert stream.read() == payload
        stream.write(b"GET /api HTTP/1.1\r\n\r\n")
        assert guest.recv(100) == b"GET /api HTTP/1.1\r\n\r\n"
        assert calls[0][1] == {
            "stdin": True,
            "stdout": True,
            "stderr": False,
            "tty": False,
            "socket": True,
        }
        assert "('127.0.0.1', 8899)" in calls[0][0][-1]
    finally:
        stream.close()
        guest.close()


@pytest.mark.parametrize("channel,size", [(2, 10), (1, 9 * 1024 * 1024)])
def test_docker_exec_rejects_invalid_frames(channel, size):
    parent, guest = socket.socketpair()
    stream = DockerExecStream(
        SimpleNamespace(exec_run=lambda *a, **kw: SimpleNamespace(output=parent))
    )
    try:
        guest.sendall(struct.pack(">BxxxI", channel, size))
        with pytest.raises(OSError, match="invalid sandbox exec frame"):
            stream.read()
    finally:
        stream.close()
        guest.close()


async def test_close_interrupts_active_websocket_and_releases_exec_stream():
    streams = []

    async def echo(ws):
        async for data in ws:
            await ws.send(data)

    async with serve(echo, "127.0.0.1", 0) as upstream:

        def opener():
            stream = _SocketExec(upstream.sockets[0].getsockname())
            streams.append(stream)
            return stream

        tunnel = KernelExecTunnel(opener)
        host, port = tunnel.forward(8899)
        try:
            async with connect(f"ws://{host}:{port}/channels", proxy=None) as websocket:
                await websocket.send("still running")
                assert await websocket.recv() == "still running"
                await asyncio.to_thread(tunnel.close)
                from websockets.exceptions import ConnectionClosed

                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(websocket.recv(), timeout=2)
            assert streams and all(stream.closed for stream in streams)
        finally:
            await asyncio.to_thread(tunnel.close)


def test_container_opener_preserves_remote_tunnel_and_rejects_noninternal_ports(monkeypatch):
    from disco.tools.sandbox._container_parts import kernel_tunnel
    from test_preview import _Container, _inst

    opened = []

    class Tunnel:
        def __init__(self, opener):
            opened.append(opener)

        def forward(self, port):
            assert port == 8899
            return "127.0.0.1", 45678

    monkeypatch.setattr(kernel_tunnel, "KernelExecTunnel", Tunnel)
    instance = _inst(_Container({8899: "33333", 8000: "33334"}))
    assert instance._open_kernel_port(8000) is None
    assert opened == []
    assert instance._open_kernel_port(8899) == ("127.0.0.1", 45678)
    assert instance._open_kernel_port(8899) == ("127.0.0.1", 45678)
    assert len(opened) == 1
    # Remote gVisor/Podman SSH forwarding retains its existing transport.
    remote = _inst(_Container({8899: "33333"}))
    remote._loopback_tunnel = SimpleNamespace(forward=lambda port: ("127.0.0.1", 44444))
    assert remote._open_kernel_port(8899) == ("127.0.0.1", 44444)
    assert len(opened) == 1


def test_podman_transport_uses_native_exec_and_reaps_process(monkeypatch):
    import subprocess
    import sys

    from disco.tools.sandbox._container_parts import kernel_tunnel

    actual_popen = subprocess.Popen
    commands = []
    processes = []

    def fake_podman(argv, **kwargs):
        commands.append(argv)
        # Real pipe/process lifecycle, replacing only the external daemon/guest.
        process = actual_popen(
            [
                sys.executable,
                "-u",
                "-c",
                ("import os\nwhile data := os.read(0,65536):\n os.write(1,data)\n"),
            ],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(kernel_tunnel.subprocess, "Popen", fake_podman)
    stream = kernel_tunnel.PodmanExecStream("unix:///private/podman.sock", "sandbox-exact")
    try:
        stream.write(b"binary\x00payload")
        assert stream.read() == b"binary\x00payload"
        assert commands[0][:7] == [
            "podman",
            "--url",
            "unix:///private/podman.sock",
            "exec",
            "-i",
            "sandbox-exact",
            "python3",
        ]
        assert "('127.0.0.1', 8899)" in commands[0][-1]
    finally:
        stream.close()
        stream.close()
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_connection_limit_rejects_extra_clients_and_shutdown_releases_all(monkeypatch):
    import threading

    from disco.tools.sandbox._container_parts import kernel_tunnel

    monkeypatch.setattr(kernel_tunnel, "_MAX_CONNECTIONS", 2)
    opened = []
    ready = threading.Event()

    class BlockingExec:
        def __init__(self):
            self.closed = threading.Event()

        def read(self):
            assert self.closed.wait(timeout=5)
            return b""

        def write(self, data):
            pass

        def close(self):
            self.closed.set()

    def opener():
        stream = BlockingExec()
        opened.append(stream)
        if len(opened) == 2:
            ready.set()
        return stream

    tunnel = KernelExecTunnel(opener)
    clients = []
    try:
        address = tunnel.forward(8899)
        clients = [socket.create_connection(address, timeout=2) for _ in range(2)]
        assert ready.wait(timeout=2)
        extra = socket.create_connection(address, timeout=2)
        clients.append(extra)
        assert extra.recv(1) == b""
        assert len(opened) == 2
        tunnel.close()
        assert all(stream.closed.wait(timeout=1) for stream in opened)
        assert not tunnel._thread.is_alive()
    finally:
        tunnel.close()
        for client in clients:
            client.close()
