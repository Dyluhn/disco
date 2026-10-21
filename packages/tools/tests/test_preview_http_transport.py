"""Real loopback protocol exchanges over the pinned sandbox exec boundary."""

from __future__ import annotations

import asyncio
import gzip
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.session_parts.request_inside import request_inside


class _ExecInstance:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.truncate = False
        self.timeout = False

    async def exec_shell(self, command: str, *, timeout_s: int) -> ExecResult:
        self.commands.append(command)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
        text = stdout.decode()
        if self.truncate and command.startswith("python3 -I -c"):
            text = text[: max(1, len(text) // 2)]
        return ExecResult(
            exit_code=process.returncode or 0,
            stdout=text,
            stderr=stderr.decode(),
            timed_out=self.timeout,
        )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, self.headers, body))
        if self.path == "/incomplete":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"partial")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.path == "/drop":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(201)
        self.send_header("Set-Cookie", "app_session=accepted; HttpOnly; Path=/")
        self.send_header("Set-Cookie", "app_theme=dark; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "100")
        self.end_headers()

    def do_GET(self) -> None:
        self.server.requests.append((self.command, self.path, self.headers, b""))
        if self.path == "/oversize":
            self.send_response(200)
            self.send_header("Content-Length", str(25 * 1024 * 1024 + 1))
            self.end_headers()
            return
        if self.path in {"/204", "/304"}:
            self.send_response(int(self.path[1:]))
            self.end_headers()
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/never")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = gzip.compress(b"\x00\xff<html>compressed</html>", mtime=0)
        self.send_response(200 if "app_session=accepted" in self.headers.get("Cookie", "") else 401)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def loopback_application():
    server = None
    for port in sorted(USER_PORTS - {NOVNC_PORT}):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            break
        except OSError:
            continue
    assert server is not None
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    instance = _ExecInstance()
    session = SimpleNamespace(
        _instance=instance,
        generation=1,
        shares_host_network=False,
        _ensure=AsyncMock(side_effect=AssertionError("must not recreate")),
    )
    try:
        yield server, session
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_real_exec_login_cookie_binary_query_and_repeated_headers(loopback_application):
    server, session = loopback_application
    port = server.server_port
    body = bytes(range(256)) * 4100  # just above1MiB is rejected before exec
    assert await request_inside(session, port, "POST", "/login", [], body) is None
    assert not session._instance.commands and not server.requests
    body = bytes(range(256)) * 4096
    target = "/login?x=%2f%2F+%20&x=&binary=%ff"
    response = await request_inside(
        session,
        port,
        "POST",
        target,
        [("X-Repeat", "one"), ("X-Repeat", "two")],
        body,
    )
    assert response is not None
    status, headers, echoed = response
    assert status == 201 and echoed == body
    assert [value for name, value in headers if name.lower() == "set-cookie"] == [
        "app_session=accepted; HttpOnly; Path=/",
        "app_theme=dark; Path=/",
    ]
    assert server.requests[0][1] == target
    assert server.requests[0][2].get_all("X-Repeat") == ["one", "two"]
    assert server.requests[0][2].get_all("Content-Length") == [str(len(body))]
    assert len([c for c in session._instance.commands if c.startswith("python3 -I -c")]) == 1
    assert session._instance.commands[-1].startswith("rm -rf -- /tmp/disco-preview-http-")
    for _reload in range(2):
        read = await request_inside(
            session, port, "GET", "/", [("Cookie", "app_session=accepted")], b""
        )
        assert read is not None and read[0] == 200
        assert gzip.decompress(read[2]) == b"\x00\xff<html>compressed</html>"
    session._ensure.assert_not_awaited()


@pytest.mark.parametrize(
    "method,target,status",
    [("HEAD", "/", 200), ("GET", "/204", 204), ("GET", "/304", 304), ("GET", "/redirect", 302)],
)
async def test_real_exec_no_body_and_no_redirect(loopback_application, method, target, status):
    server, session = loopback_application
    result = await request_inside(session, server.server_port, method, target, [], b"")
    assert result is not None and result[0] == status and result[2] == b""


@pytest.mark.parametrize(
    "failure", ["lost_response", "truncated_frame", "incomplete_body", "timeout"]
)
async def test_mutation_is_never_replayed_after_unknown_outcome(loopback_application, failure):
    server, session = loopback_application
    session._instance.truncate = failure == "truncated_frame"
    session._instance.timeout = failure == "timeout"
    target = {"lost_response": "/drop", "incomplete_body": "/incomplete"}.get(failure, "/login")
    result = await request_inside(session, server.server_port, "POST", target, [], b"write-once")
    assert result is None
    assert len(server.requests) == 1
    assert len(session._instance.commands) == 1
    session._ensure.assert_not_awaited()


@pytest.mark.parametrize("port", [NOVNC_PORT, 1, 65535, 8901])
async def test_internal_and_unselected_network_ports_cannot_dispatch(port):
    instance = SimpleNamespace(exec_shell=AsyncMock())
    session = SimpleNamespace(_instance=instance, generation=1, shares_host_network=False)
    assert await request_inside(session, port, "POST", "/", [], b"secret") is None
    instance.exec_shell.assert_not_awaited()


async def test_generation_change_during_staging_never_dispatches(loopback_application):
    server, session = loopback_application
    original = session._instance.exec_shell

    async def change_generation(command, *, timeout_s):
        result = await original(command, timeout_s=timeout_s)
        if command.startswith("printf"):
            session.generation += 1
        return result

    session._instance.exec_shell = change_generation
    assert (
        await request_inside(session, server.server_port, "POST", "/login", [], b"x" * 70000)
        is None
    )
    assert not server.requests
    assert session._instance.commands[-1].startswith("rm -rf")


async def test_concurrent_staged_requests_are_isolated_and_cleaned(loopback_application):
    server, session = loopback_application
    bodies = [b"a" * 70000, b"b" * 75000]
    responses = await asyncio.gather(
        *(
            request_inside(session, server.server_port, "POST", "/login", [], body)
            for body in bodies
        )
    )
    assert [result[2] for result in responses if result is not None] == bodies
    mkdirs = [command for command in session._instance.commands if command.startswith("mkdir")]
    assert len(mkdirs) == 2 and mkdirs[0] != mkdirs[1]
    for command in mkdirs:
        directory = command.split()[-1]
        assert not Path(directory).exists()


async def test_cancellation_during_staging_cleans_without_dispatch(loopback_application):
    server, session = loopback_application
    original = session._instance.exec_shell

    async def cancel_after_chunk(command, *, timeout_s):
        result = await original(command, timeout_s=timeout_s)
        if command.startswith("printf"):
            raise asyncio.CancelledError
        return result

    session._instance.exec_shell = cancel_after_chunk
    with pytest.raises(asyncio.CancelledError):
        await request_inside(session, server.server_port, "POST", "/login", [], b"x" * 70000)
    assert not server.requests
    directory = session._instance.commands[0].split()[-1]
    assert not Path(directory).exists()


async def test_oversized_response_cannot_be_truncated_into_success(loopback_application):
    server, session = loopback_application
    result = await request_inside(session, server.server_port, "GET", "/oversize", [], b"")
    assert result is None
    assert len(server.requests) == 1 and len(session._instance.commands) == 1
