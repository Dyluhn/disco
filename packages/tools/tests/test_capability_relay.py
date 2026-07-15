"""Exploit tests for the sandbox -> host-service capability relay."""

from __future__ import annotations

import errno
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from disco.tools.sandbox.base import SandboxSpec, SandboxUnavailableError
from disco.tools.sandbox.capability_relay import (
    CapabilityRelayServer,
    RelayConfigurationError,
    serve_in_thread,
)
from disco.tools.sandbox.config import SandboxConfig
from disco.tools.sandbox.process import ProcessSandboxService

_TOKEN = "a2v0.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


class _UpstreamHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, str], bytes]] = []
    redirect = False
    content_type = "application/json"
    content_encoding = ""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        type(self).requests.append((self.path, dict(self.headers), body))
        if type(self).redirect:
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        response = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", type(self).content_type)
        if type(self).content_encoding:
            self.send_header("Content-Encoding", type(self).content_encoding)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def relay() -> tuple[CapabilityRelayServer, _UpstreamHandler]:
    _UpstreamHandler.requests = []
    _UpstreamHandler.redirect = False
    _UpstreamHandler.content_type = "application/json"
    _UpstreamHandler.content_encoding = ""
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    up_host, up_port = upstream.server_address[:2]
    server = CapabilityRelayServer("127.0.0.1", 0, f"http://{up_host}:{up_port}")
    serve_in_thread(server)
    try:
        yield server, _UpstreamHandler
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def _request(server: CapabilityRelayServer, request: bytes) -> tuple[int, bytes]:
    host, port = server.server_address[:2]
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(request)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError as exc:
            # A fast fail-closed relay may send its complete 4xx response and
            # close before the client half-closes. ENOTCONN is then expected;
            # the buffered response remains readable and is still asserted.
            if exc.errno != errno.ENOTCONN:
                raise
        response = bytearray()
        while chunk := sock.recv(65536):
            response.extend(chunk)
    head, body = bytes(response).split(b"\r\n\r\n", 1)
    return int(head.split(b" ", 2)[1]), body


def _valid(
    server: CapabilityRelayServer,
    *,
    path: bytes = b"/_disco/svc/svc.ping",
    host: bytes | None = None,
    extra: bytes = b"",
    body: bytes = b"{}",
    length: bytes | None = None,
) -> bytes:
    authority = host or server.allowed_authority.encode("ascii")
    declared = length if length is not None else str(len(body)).encode("ascii")
    return (
        b"POST "
        + path
        + b" HTTP/1.1\r\nHost: "
        + authority
        + b"\r\nAuthorization: Bearer "
        + _TOKEN.encode("ascii")
        + b"\r\nContent-Type: application/json\r\nContent-Length: "
        + declared
        + b"\r\n"
        + extra
        + b"\r\n"
        + body
    )


def test_forwards_only_fixed_service_route_and_minimal_headers(relay) -> None:
    server, upstream = relay
    status, body = _request(
        server,
        _valid(server, extra=b"User-Agent: workerd\r\nX-Unrelated: drop-me\r\n"),
    )
    assert status == 200 and body == b'{"ok":true}'
    assert len(upstream.requests) == 1
    path, headers, sent = upstream.requests[0]
    assert path == "/_disco/svc/svc.ping" and sent == b"{}"
    assert set(headers) == {"Host", "Content-Type", "Content-Length", "Authorization"}


def test_accepts_full_128_byte_service_name(relay) -> None:
    server, upstream = relay
    service = b"a." + (b"b" * 126)
    status, body = _request(
        server,
        _valid(server, path=b"/_disco/svc/" + service),
    )
    assert status == 200 and body == b'{"ok":true}'
    assert upstream.requests[0][0] == f"/_disco/svc/{service.decode('ascii')}"


@pytest.mark.parametrize(
    ("request_factory", "expected"),
    [
        (lambda s: _valid(s, path=b"http://169.254.169.254/latest/meta-data/"), 404),
        (lambda s: _valid(s, path=b"/_disco/svc/../../var/run/docker.sock"), 404),
        (lambda s: _valid(s, path=b"/_disco/debug/inspect"), 404),
        (lambda s: _valid(s, path=b"/_disco/svc/svc.ping/extra"), 404),
        (lambda s: _valid(s, host=b"169.254.169.254"), 400),
        (lambda s: _valid(s, extra=b"Transfer-Encoding: chunked\r\n"), 400),
        (lambda s: _valid(s, extra=b"Content-Encoding: gzip\r\n"), 400),
        (lambda s: _valid(s, extra=b"X-Test: ok\r\n folded: no\r\n"), 400),
        (lambda s: _valid(s, extra=b"Content-Length: 2\r\n"), 400),
        (lambda s: _valid(s, body=b"{}extra", length=b"2"), 400),
        (
            lambda s: _valid(
                s,
                path=b"/_disco/svc/" + b"a" * 100 + b"." + b"b" * 100,
            ),
            404,
        ),
        (lambda s: _valid(s, body=b"a" * (64 * 1024 + 1)), 413),
        (lambda s: _valid(s, body=b"not-json"), 400),
        (lambda s: _valid(s, body=b"[]"), 400),
        (lambda s: _valid(s, body=b'{"a":1,"a":2}'), 400),
        (lambda s: _valid(s, extra=b"X-Fill: " + b"a" * (16 * 1024) + b"\r\n"), 431),
    ],
)
def test_refuses_metadata_docker_sibling_host_and_smuggling(
    relay, request_factory, expected
) -> None:
    server, upstream = relay
    status, _body = _request(server, request_factory(server))
    assert status == expected
    assert upstream.requests == []


def test_refuses_connect_and_non_post(relay) -> None:
    server, upstream = relay
    authority = server.allowed_authority.encode("ascii")
    for method, target in (
        (b"CONNECT", b"169.254.169.254:80"),
        (b"GET", b"/_disco/svc/svc.ping"),
    ):
        status, _ = _request(
            server,
            method + b" " + target + b" HTTP/1.1\r\nHost: " + authority + b"\r\n\r\n",
        )
        assert status == 405
    assert upstream.requests == []


def test_refuses_bad_bearer_before_upstream(relay) -> None:
    server, upstream = relay
    status, _ = _request(
        server,
        _valid(server).replace(_TOKEN.encode("ascii"), b"a2v0.short.bad"),
    )
    assert status == 401
    assert upstream.requests == []


def test_does_not_follow_upstream_redirect(relay) -> None:
    server, upstream = relay
    upstream.redirect = True
    status, _ = _request(server, _valid(server))
    assert status == 502
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    ("content_type", "content_encoding"),
    [("text/plain", ""), ("application/json", "gzip")],
)
def test_refuses_non_json_or_compressed_upstream_response(
    relay, content_type, content_encoding
) -> None:
    server, upstream = relay
    upstream.content_type = content_type
    upstream.content_encoding = content_encoding
    status, _ = _request(server, _valid(server))
    assert status == 502


def test_plaintext_non_loopback_config_is_refused() -> None:
    with pytest.raises(RelayConfigurationError, match="refuses plaintext"):
        CapabilityRelayServer("127.0.0.1", 0, "http://192.168.1.10:8000")


def test_plaintext_localhost_must_resolve_only_to_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8000))
        ],
    )
    with pytest.raises(RelayConfigurationError, match="resolved outside loopback"):
        CapabilityRelayServer("127.0.0.1", 0, "http://localhost:8000")


def test_overload_response_has_one_framed_json_body(relay) -> None:
    server, _upstream = relay
    for _ in range(16):
        assert server._slots.acquire(blocking=False)
    try:
        host, port = server.server_address[:2]
        with socket.create_connection((host, port), timeout=2) as sock:
            response = bytearray()
            while chunk := sock.recv(65536):
                response.extend(chunk)
    finally:
        for _ in range(16):
            server._slots.release()
    head, body = bytes(response).split(b"\r\n\r\n", 1)
    status = int(head.split(b" ", 2)[1])
    assert status == 503
    assert body == b'{"error":"relay overloaded"}'


@pytest.mark.asyncio
async def test_process_backend_relay_is_loopback_lifecycle_owned(relay, tmp_path) -> None:
    upstream_relay, _ = relay
    # Use the fixture's real upstream, not the relay itself, as the pinned endpoint.
    upstream = upstream_relay.upstream
    cfg = SandboxConfig(
        backend="process",
        host_service_upstream=f"http://{upstream.host}:{upstream.port}",
    )
    service = ProcessSandboxService(root=str(tmp_path), config=cfg)
    instance = await service.create(
        SandboxSpec(host_services=True), owner_id="o", conversation_id="c"
    )
    assert instance.host_service_relay_url is not None
    host, port = instance.host_service_relay_url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=1):
        pass
    await instance.destroy()
    with pytest.raises(OSError):
        socket.create_connection((host, int(port)), timeout=0.1)


@pytest.mark.asyncio
async def test_required_relay_without_operator_upstream_fails_closed(tmp_path) -> None:
    service = ProcessSandboxService(root=str(tmp_path))
    with pytest.raises(SandboxUnavailableError):
        await service.create(SandboxSpec(host_services=True), owner_id="o", conversation_id="c")
    assert not any(tmp_path.iterdir())
