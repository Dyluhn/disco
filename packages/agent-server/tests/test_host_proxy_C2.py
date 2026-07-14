"""C2 — first-hit wake race: connect-retry on a just-woken upstream.

The preview upstream (a dev server inside the sandbox) is bound lazily; the
first proxy hit after wake can land BEFORE the port is listening, which
surfaces as a connect failure rather than an HTTP response. The proxy
(`host_proxy.HostPreviewProxyMiddleware`) must retry the connect a bounded
number of times with a short backoff, NOT retry on real HTTP responses, and
add zero extra latency to a healthy upstream.

These tests live in a separate file so the C2 evidence is self-contained.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from disco.agent_server.host_proxy import (
    _CONNECT_BACKOFF_BASE,
    _CONNECT_BACKOFF_CAP,
    _CONNECT_BACKOFF_FACTOR,
    _CONNECT_RETRY_ATTEMPTS,
    HostPreviewProxyMiddleware,
)
from starlette.applications import Starlette
from starlette.routing import Route


# ---------------------------------------------------------------------------
# Flaky server: TCP server that closes the first N connections, then serves
# a real HTTP 200. The closure happens AFTER the OS-level accept(), so the
# client sees ECONNRESET (a RequestError subclass) — exactly the failure
# mode that httpx surfaces when a port is bound but the listener is still
# coming up.
# ---------------------------------------------------------------------------
class FlakyHTTPServer:
    """Closes the first `fail_count` TCP connections, then serves HTTP 200."""

    def __init__(self, fail_count: int = 2) -> None:
        self.fail_count = fail_count
        self.attempt_count = 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port: int = self.sock.getsockname()[1]
        self._stopped = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stopped:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            self.attempt_count += 1
            if self.attempt_count <= self.fail_count:
                # Close immediately to provoke a transport error on the client.
                try:
                    conn.close()
                except OSError:
                    pass
            else:
                # From now on, serve real HTTP requests.
                threading.Thread(
                    target=self._handle_http, args=(conn,), daemon=True
                ).start()

    def _handle_http(self, conn: socket.socket) -> None:
        try:
            buf = b""
            conn.settimeout(2.0)
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            request_line = buf.split(b"\r\n", 1)[0]
            parts = request_line.split(b" ")
            method = parts[0].decode() if parts else "GET"
            path = parts[1].decode() if len(parts) > 1 else "/"
            headers_part, _, body_part = buf.partition(b"\r\n\r\n")
            content_length = 0
            for line in headers_part.split(b"\r\n")[1:]:
                if b":" in line:
                    k, _, v = line.partition(b":")
                    if k.strip().lower() == b"content-length":
                        try:
                            content_length = int(v.strip())
                        except ValueError:
                            content_length = 0
            while len(body_part) < content_length:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                body_part += chunk
            body = json.dumps(
                {"method": method, "path": path, "body": body_part.decode()}
            ).encode()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n" + body
            )
            conn.sendall(response)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Upstream that returns a configurable status (used to verify 5xx is NOT
# retried). Tracks request count so the test can assert no retry happened.
# ---------------------------------------------------------------------------
class _CountingHandler(BaseHTTPRequestHandler):
    request_count = 0
    status_to_return = 500

    def do_GET(self):  # noqa: N802 — stdlib name
        _CountingHandler.request_count += 1
        body = json.dumps({"path": self.path}).encode()
        self.send_response(_CountingHandler.status_to_return)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):  # silence test noise
        return


@pytest.fixture
def counting_server():
    _CountingHandler.request_count = 0
    server = HTTPServer(("127.0.0.1", 0), _CountingHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", server
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def real_server():
    """Healthy upstream that echoes the request."""
    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"path": self.path, "method": "GET"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kwargs):
            return

    server = HTTPServer(("127.0.0.1", 0), Echo)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join()
    server.server_close()


def _make_proxy_app(upstream_url: str) -> Starlette:
    app = Starlette(routes=[Route("/never", lambda req: None)])
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda cid8, port: upstream_url if cid8 == "aaaaaaaa" else None,
    )
    return app


# ---------------------------------------------------------------------------
# Test 1: a stub upstream that refuses the connection for the first 2
# attempts then binds → the proxy returns 200 (it retried), not 502.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_connect_retry_then_success():
    flaky = FlakyHTTPServer(fail_count=2)
    try:
        app = _make_proxy_app(f"http://127.0.0.1:{flaky.port}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://aaaaaaaa-8000.localhost"
        ) as client:
            t0 = time.perf_counter()
            resp = await client.get("/wake-up")
            elapsed = time.perf_counter() - t0

        assert resp.status_code == 200, (
            f"expected 200 after retries, got {resp.status_code}: {resp.content!r}"
        )
        payload = resp.json()
        assert payload["path"] == "/wake-up"
        # The proxy saw 3 connect attempts: 2 refused + 1 served.
        assert flaky.attempt_count == 3, (
            f"expected 3 connect attempts (2 fail + 1 ok), got {flaky.attempt_count}"
        )
        # Total backoff: 50ms + 100ms = 150ms minimum. Allow generous slack.
        assert elapsed < 2.0, f"retry path took too long: {elapsed:.3f}s"
    finally:
        flaky.stop()


# ---------------------------------------------------------------------------
# Test 2: a permanently-down upstream → bounded retries then a single clean
# error (no hang, no infinite loop). Total wall time must be bounded.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_connect_retry_bounded_failure():
    # Pick a port we know is closed: bind-then-close to grab a free port.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()

    app = _make_proxy_app(f"http://127.0.0.1:{closed_port}")
    transport = httpx.ASGITransport(app=app)

    t0 = time.perf_counter()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://aaaaaaaa-8000.localhost"
    ) as client:
        resp = await client.get("/")
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 502
    assert b"preview upstream unreachable" in resp.content

    # Expected backoff: 50 + 100 + 200 + 400 = 750ms (4 intervals between
    # 5 attempts). Add slack for connect time on a closed port (RST is
    # local, so usually < 5ms). Hard cap at 3s to assert bounded.
    assert elapsed < 3.0, f"bounded failure took too long: {elapsed:.3f}s"
    # Sanity: must have spent at least the backoff budget.
    expected_backoff = sum(
        min(
            _CONNECT_BACKOFF_BASE * (_CONNECT_BACKOFF_FACTOR ** i),
            _CONNECT_BACKOFF_CAP,
        )
        for i in range(_CONNECT_RETRY_ATTEMPTS - 1)
    )
    assert elapsed >= expected_backoff * 0.8, (
        f"too fast ({elapsed:.3f}s) — backoff loop likely didn't run "
        f"({expected_backoff:.3f}s expected)"
    )


# ---------------------------------------------------------------------------
# Test 3: a real 5xx response is NOT retried. This protects the contract
# that the proxy passes upstream error statuses through unchanged and does
# not amplify load against a struggling (but responding) upstream.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_5xx_passthrough_no_retry(counting_server):
    upstream_url, _server = counting_server
    _CountingHandler.status_to_return = 503  # upstream is up but angry
    _CountingHandler.request_count = 0

    app = _make_proxy_app(upstream_url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://aaaaaaaa-8000.localhost"
    ) as client:
        t0 = time.perf_counter()
        resp = await client.get("/")
        elapsed = time.perf_counter() - t0

    assert resp.status_code == 503, (
        f"expected upstream 503 to pass through, got {resp.status_code}"
    )
    # The retry loop only fires on RequestError. A 5xx is a real response.
    # The upstream must have been hit exactly once.
    assert _CountingHandler.request_count == 1, (
        f"upstream hit {_CountingHandler.request_count} times — proxy "
        f"must not retry on a real response"
    )
    # A single hit on localhost should be well under 100ms; the retry budget
    # alone is 750ms of backoff, so this proves the loop didn't run.
    # The intent is "no retry delay was inserted" (a retry adds seconds — the
    # auth-retry backoff alone is 2s). 0.1s wall-clock false-failed under
    # full-suite load (observed 0.18-0.36s of pure scheduler noise); 1.0s still
    # proves no-retry while surviving a loaded box. request_count==1 above is
    # the authoritative no-retry check.
    assert elapsed < 1.0, f"5xx path took {elapsed:.3f}s — retry loop ran?"


# ---------------------------------------------------------------------------
# Test 4: a healthy upstream must add ZERO extra latency. We assert the
# total wall time of a single GET is far below the retry backoff budget
# (the proxy cannot have slept if it returned in < 100ms).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_healthy_upstream_no_extra_latency(real_server):
    app = _make_proxy_app(real_server)
    transport = httpx.ASGITransport(app=app)

    # Warm up: first request through ASGI can be a hair slower.
    async with httpx.AsyncClient(
        transport=transport, base_url="http://aaaaaaaa-8000.localhost"
    ) as client:
        await client.get("/warmup")

        # Time a single GET. If the retry loop ran even once, the
        # asyncio.sleep() floor of 50ms would push us over the budget.
        t0 = time.perf_counter()
        resp = await client.get("/fast")
        elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    # Strict: must be far below the smallest backoff interval.
    assert elapsed < 0.05, (
        f"healthy upstream added latency: {elapsed*1000:.1f}ms "
        f"(expected < 50ms; retry backoff starts at 50ms)"
    )
