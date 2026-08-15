import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import disco.agent_server.host_proxy as host_proxy_module
import httpx
import pytest
import uvicorn
import websockets
from disco.agent_server.host_proxy import (
    MAX_PREVIEW_REQUEST_BODY_BYTES,
    PREVIEW_HOST_RE,
    HostPreviewProxyMiddleware,
    _rewrite_canonical_upstream_location,
)
from disco.core.auth import (
    PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS,
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_COOKIE,
    AuthSession,
    PreviewCapabilitySigner,
)
from disco.core.store.sqlite import SqliteEventStore
from starlette.applications import Starlette
from starlette.endpoints import WebSocketEndpoint
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import Scope


class EchoHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/new-location")
            self.end_headers()
            return

        if self.path == "/cookies":
            body = json.dumps({"cookies": self.headers.get_all("Cookie") or []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "disco_preview_cap=upstream-clobber; Path=/")
            self.send_header("Set-Cookie", "app_session=preserved; Path=/")
            self.send_header("Set-Cookie", "disco_preview_cap_extra=preserved; Path=/")
            self.send_header("Set-Cookie", "Disco_preview_cap=case-distinct; Path=/")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/huge-html":
            body = b"<html><body>" + b"x" * (3 * 1024 * 1024) + b"</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/cacheable":
            body = b"cacheable upstream"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Surrogate-Control", "max-age=86400")
            self.send_header("CDN-Cache-Control", "public, max-age=86400")
            self.send_header("ETag", '"upstream-etag"')
            self.send_header("Last-Modified", "Tue, 14 Jul 2026 00:00:00 GMT")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"path": self.path, "method": "GET"}).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"path": self.path, "method": "POST", "body": body.decode()}).encode()
        )


class FailingPublishedClient:
    """httpx-compatible client whose outbound transport always fails."""

    def __init__(
        self,
        *,
        message: str = "synthetic published transport failure",
        error_type: type[httpx.RequestError] = httpx.ConnectError,
    ) -> None:
        self.calls = 0
        self.message = message
        self.error_type = error_type

    def build_request(self, method, url, *, headers, content):
        return httpx.Request(method, url, headers=headers, content=content)

    async def send(self, request, *, stream):
        assert stream is True
        self.calls += 1
        raise self.error_type(self.message, request=request)


@pytest.fixture(scope="module")
def upstream_http():
    server = HTTPServer(("127.0.0.1", 0), EchoHTTPRequestHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def mock_app(upstream_http):
    def test_route(request):
        return PlainTextResponse("real app route works")

    app = Starlette(routes=[Route("/existing", test_route)])

    def resolver(cid8, port):
        if cid8 == "aaaaaaaa" and port == 8000:
            return upstream_http
        if cid8 == "aaaaaaaa" and port == 8899:
            return upstream_http
        if cid8 == "bbbbbbbb":
            return "http://127.0.0.1:59999"  # Dead upstream
        return None

    app.add_middleware(HostPreviewProxyMiddleware, upstream_resolver=resolver)
    return app


@pytest.mark.asyncio
async def test_live_capability_uses_body_post_and_durable_one_time_exchange(upstream_http):
    store = SqliteEventStore(":memory:")
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda cid8, port, _owner=None: (
            upstream_http if cid8 == "aaaaaaaa" and port == 8000 else None
        ),
        require_capability=True,
        redemption_store=store,
    )
    signer = PreviewCapabilitySigner(redemption_store=store)
    session = AuthSession(
        owner_id="owner-a",
        csrf_token="csrf",
        session_id="session",
        expires_at=2**31,
    )
    intent = signer.mint_intent(
        session=session,
        conversation_id="conv_aaaaaaaafull",
        port=8000,
        target_path="/",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://p2-aaaaaaaa-8000.localhost",
    ) as client:
        wrong_method = await client.get(PREVIEW_BOOTSTRAP_PATH)
        assert wrong_method.status_code == 405

        redemption = await client.post(
            PREVIEW_BOOTSTRAP_PATH,
            headers={"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"},
            data={"intent": intent},
        )
        assert redemption.status_code == 200
        assert "?" not in str(redemption.request.url)
        assert intent not in str(redemption.request.url)
        assert "window.location.replace" in redemption.text
        assert intent not in redemption.text
        assert redemption.headers["clear-site-data"] == '"storage"'
        cookie = redemption.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "; Secure" in cookie
        assert "samesite=none" in cookie.lower()
        assert "Partitioned" in cookie
        assert "samesite=strict" not in cookie.lower()

        # The response capability gates the real upstream and cannot be exchanged twice.
        capability_cookie = cookie.split(";", 1)[0]
        for duplicate_headers in (
            [("Cookie", f"{PREVIEW_COOKIE}=child-domain-invalid; {capability_cookie}")],
            [("Cookie", f"{capability_cookie}; {PREVIEW_COOKIE}=child-domain-invalid")],
            [
                ("Cookie", f"{PREVIEW_COOKIE}=child-domain-invalid"),
                ("Cookie", capability_cookie),
            ],
        ):
            proxied = await client.get("/", headers=duplicate_headers)
            assert proxied.status_code == 200
            assert proxied.headers["cache-control"] == "private, no-store"
            assert proxied.headers["pragma"] == "no-cache"

        cacheable = await client.get("/cacheable", headers={"Cookie": capability_cookie})
        assert cacheable.status_code == 200
        assert cacheable.headers["cache-control"] == "private, no-store"
        assert "etag" not in cacheable.headers
        assert "last-modified" not in cacheable.headers
        assert "surrogate-control" not in cacheable.headers
        assert "cdn-cache-control" not in cacheable.headers

        mutation = await client.post(
            "/api/submit",
            headers={"Cookie": capability_cookie, "Origin": "http://p2-aaaaaaaa-8000.localhost"},
            content=b"capability mutation",
        )
        assert mutation.status_code == 201
        assert mutation.json()["body"] == "capability mutation"
        for hostile_origin in (None, "https://evil.example"):
            headers = {"Cookie": capability_cookie}
            if hostile_origin is not None:
                headers["Origin"] = hostile_origin
            rejected_mutation = await client.post(
                "/api/submit", headers=headers, content=b"must not reach upstream"
            )
            assert rejected_mutation.status_code == 403
            assert rejected_mutation.text == "preview origin required"
        wrong_scheme_mutation = await client.post(
            "/api/submit",
            headers={
                "Cookie": capability_cookie,
                "Origin": "http://p2-aaaaaaaa-8000.localhost",
                "X-Forwarded-Proto": "https",
            },
            content=b"must not reach upstream",
        )
        assert wrong_scheme_mutation.status_code == 403

        for service_worker_headers in (
            {"Sec-Fetch-Dest": "serviceworker"},
            {"Service-Worker": "script"},
        ):
            service_worker = await client.get(
                "/sw.js",
                headers={"Cookie": capability_cookie, **service_worker_headers},
            )
            assert service_worker.status_code == 403
            assert service_worker.headers["cache-control"] == "private, no-store"
            assert service_worker.text == "preview service workers disabled"
        replay = await client.post(
            PREVIEW_BOOTSTRAP_PATH,
            data={"intent": intent},
        )
        assert replay.status_code == 403
        assert "set-cookie" not in replay.headers

        # Top-level control remains first-party Strict and non-partitioned.
        top_level_intent = signer.mint_intent(
            session=session,
            conversation_id="conv_aaaaaaaafull",
            port=8000,
            target_path="/",
            allow_websocket=True,
        )
        top_level_redemption = await client.post(
            PREVIEW_BOOTSTRAP_PATH,
            headers={"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "cross-site"},
            data={"intent": top_level_intent},
        )
        top_level_cookie = top_level_redemption.headers["set-cookie"]
        assert "samesite=strict" in top_level_cookie.lower()
        assert "Partitioned" not in top_level_cookie

        # JSON/multipart cannot smuggle alternate redemption shapes. Rejecting
        # the media type happens before consumption, so the valid form still wins.
        content_type_intent = signer.mint_intent(
            session=session,
            conversation_id="conv_aaaaaaaafull",
            port=8000,
            target_path="/",
            allow_websocket=True,
        )
        wrong_content_type = await client.post(
            PREVIEW_BOOTSTRAP_PATH, json={"intent": content_type_intent}
        )
        assert wrong_content_type.status_code == 403
        valid_after_rejection = await client.post(
            PREVIEW_BOOTSTRAP_PATH, data={"intent": content_type_intent}
        )
        assert valid_after_rejection.status_code == 200

    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"http://p3s-aaaaaaaa-{'b' * 40}-8000.localhost",
    ) as static_origin:
        wrong_transport = await static_origin.get("/", headers={"Cookie": capability_cookie})
        assert wrong_transport.status_code == 403
        assert wrong_transport.text == "isolated preview route required"
    store.close()


@pytest.mark.asyncio
async def test_managed_host_port_requires_and_honors_exact_signed_capability(upstream_http):
    """The broad range is reachable only through the existing capability boundary."""

    port = 10_123
    store = SqliteEventStore(":memory:")
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda cid8, selected, _owner=None: (
            upstream_http if cid8 == "aaaaaaaa" and selected == port else None
        ),
        require_capability=True,
        redemption_store=store,
    )
    signer = PreviewCapabilitySigner(redemption_store=store)
    session = AuthSession("owner-a", "csrf", "session", 2**31)
    intent = signer.mint_intent(
        session=session,
        conversation_id="conv_aaaaaaaafull",
        port=port,
        target_path="/",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"http://p2-aaaaaaaa-{port}.localhost",
    ) as client:
        unsigned = await client.get("/")
        assert unsigned.status_code == 403

        redeemed = await client.post(PREVIEW_BOOTSTRAP_PATH, data={"intent": intent})
        assert redeemed.status_code == 200
        capability_cookie = redeemed.headers["set-cookie"].split(";", 1)[0]
        proxied = await client.get("/", headers={"Cookie": capability_cookie})
        assert proxied.status_code == 200

    # The same dynamic-looking host is still rejected when the capability
    # middleware is absent; the range never becomes anonymous generic routing.
    ungoverned = Starlette()
    ungoverned.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda *_args: upstream_http,
        require_capability=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ungoverned),
        base_url=f"http://p2-aaaaaaaa-{port}.localhost",
    ) as client:
        rejected = await client.get("/")
    assert rejected.status_code == 404
    assert rejected.text == "unknown port"
    store.close()


@pytest.mark.asyncio
async def test_canonical_live_proxy_preserves_real_http_and_rejects_stale_authority(
    upstream_http,
) -> None:
    store = SqliteEventStore(":memory:")
    signer = PreviewCapabilitySigner(redemption_store=store)
    session = AuthSession("owner-a", "csrf", "session", 2**31)
    authority = "live:" + "a" * 64
    intent = signer.mint_intent(
        session=session,
        conversation_id="conv_aaaaaaaafull",
        port=8000,
        target_path="/",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
        authority_id=authority,
    )
    redeemed = signer.redeem_intent(intent, cid8="aaaaaaaa", port=8000, path_scope="host")
    assert redeemed is not None
    token, _target = redeemed
    current = {"authority": authority}
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda *_args: upstream_http,
        require_capability=True,
        redemption_store=store,
        canonical_authority_resolver=lambda *_args: current["authority"],
    )
    transport = httpx.ASGITransport(app=app)
    host = "p2-aaaaaaaa-8000.localhost"
    cookie = f"{PREVIEW_COOKIE}={token}; app_session=kept"
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
        redirect = await client.get("/redirect", headers={"Cookie": cookie}, follow_redirects=False)
        assert redirect.status_code == 301
        assert redirect.headers["location"] == "/new-location"

        cookies = await client.get("/cookies", headers={"Cookie": cookie})
        assert cookies.status_code == 200
        assert "app_session=kept" in "; ".join(cookies.json()["cookies"])
        assert PREVIEW_COOKIE not in "; ".join(cookies.json()["cookies"])
        assert "app_session=preserved; Path=/" in cookies.headers.get_list("set-cookie")

        posted = await client.post(
            "/action",
            headers={"Cookie": cookie, "Origin": f"http://{host}"},
            content=b"real interaction",
        )
        assert posted.status_code == 201
        assert posted.json()["body"] == "real interaction"

        # Canonical live HTTP keeps full application routing, but it never hands
        # workspace-secret or structurally ambiguous paths to a server that may
        # have been launched at the workspace root.
        ordinary_hidden_route = await client.get("/.well-known/app", headers={"Cookie": cookie})
        assert ordinary_hidden_route.status_code == 200
        for unsafe_path in (
            "/.env",
            "/nested/../.env",
            "/%2e%2e/.env",
            "/%252e%252e/.env",
            "/assets%2f..%2f.env",
        ):
            refused = await client.get(unsafe_path, headers={"Cookie": cookie})
            assert refused.status_code == 404
            assert refused.text == "preview path not found"

        current["authority"] = "live:" + "b" * 64
        stale = await client.get("/", headers={"Cookie": cookie})
        assert stale.status_code == 409
        assert stale.text == "preview generation changed"
    store.close()


@pytest.mark.asyncio
async def test_live_capability_disconnect_does_not_spin() -> None:
    store = SqliteEventStore(":memory:")

    async def downstream(scope, receive, send):
        raise AssertionError((scope, receive, send))

    app = HostPreviewProxyMiddleware(
        downstream,
        upstream_resolver=lambda *_args: None,
        require_capability=True,
        redemption_store=store,
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PREVIEW_BOOTSTRAP_PATH,
        "raw_path": PREVIEW_BOOTSTRAP_PATH.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"p2-aaaaaaaa-8000.localhost"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=0.2)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    assert not any(name.lower() == b"set-cookie" for name, _value in sent[0].get("headers", []))
    store.close()


@pytest.mark.asyncio
async def test_proxy_disconnect_never_forwards_a_truncated_mutation() -> None:
    async def downstream(scope, receive, send):
        raise AssertionError((scope, receive, send))

    app = HostPreviewProxyMiddleware(
        downstream,
        upstream_resolver=lambda *_args: "http://127.0.0.1:1",
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/submit",
        "raw_path": b"/api/submit",
        "query_string": b"",
        "headers": [(b"host", b"p2-aaaaaaaa-8000.localhost")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }
    messages = iter(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent: list[dict] = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    assert sent == []


@pytest.mark.asyncio
async def test_connect_retry_never_replays_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    class FailingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, request, *, stream):
            assert stream is True
            self.calls += 1
            raise httpx.ConnectError("synthetic connect failure", request=request)

    async def run(method: str) -> tuple[int, list[dict]]:
        client = FailingClient()
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        request = httpx.Request(method, "http://127.0.0.1:1/action")
        result, error, attempts = await host_proxy_module._send_with_connect_retry(  # noqa: SLF001
            client, request
        )
        assert result is None
        assert isinstance(error, httpx.ConnectError)
        assert attempts == client.calls
        assert sent == []
        return client.calls, sent

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        calls, sent = await run(method)
        assert calls == 1
        assert sent == []

    calls, sent = await run("GET")
    assert calls == 5
    assert sent == []


@pytest.mark.asyncio
async def test_get_passthrough(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        # standard GET, path /src/main.jsx, query string
        resp = await client.get("/src/main.jsx?foo=bar")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        data = resp.json()
        assert data["method"] == "GET"
        assert data["path"] == "/src/main.jsx?foo=bar"


def test_front_door_routes_versioned_preview_hosts_to_agent_server() -> None:
    nginx = (Path(__file__).parents[3] / "frontend" / "nginx.conf").read_text()
    assert (
        'server_name "~^(?:p2-[0-9a-f]{8}-\\d{2,5}|'
        f"p3s-[0-9a-f]{{8}}-[0-9a-f]{{{PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS}}}"
        '-\\d{2,5})\\.";' in nginx
    )

    default_server = nginx.split("listen 80 default_server;", 1)[1]
    for prefix in (
        "/__disco/path-preview-auth",
        "/__disco/isolated-preview",
    ):
        marker = f"location ^~ {prefix}/ {{"
        assert default_server.count(marker) == 1
        block = default_server.split(marker, 1)[1].split("\n    }", 1)[0]
        assert "proxy_pass http://$agent_upstream:8000;" in block
        assert "proxy_http_version 1.1;" in block
        assert "proxy_set_header Host $http_host;" in block
        assert "proxy_set_header X-Forwarded-Proto $xfp;" in block
        assert "proxy_set_header Upgrade $http_upgrade;" in block
        assert "proxy_set_header Connection $connection_upgrade;" in block
        assert "proxy_buffering off;" in block
        assert "proxy_request_buffering off;" in block
        assert "rewrite " not in block

    assert "location ^~ /api/" not in default_server
    assert "location ^~ /conversations/" not in default_server


def test_front_door_derives_dynamic_resolver_from_the_container_runtime(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[3]
    nginx = (root / "frontend" / "nginx.conf").read_text()
    dockerfile = (root / "frontend" / "Dockerfile").read_text()
    resolver_envsh = (
        root / "frontend" / "docker-entrypoint.d" / "10-disco-resolver.envsh"
    ).read_text()

    assert "resolver ${DISCO_NGINX_RESOLVER} valid=10s ipv6=off;" in nginx
    assert "resolver 127.0.0.11" not in nginx
    assert "set $app_upstream app-server;" in nginx
    assert "set $agent_upstream agent-server;" in nginx

    assert (
        "COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template"
        in dockerfile
    )
    assert "10-disco-resolver.envsh" in dockerfile
    assert "NGINX_ENVSUBST_FILTER=^DISCO_NGINX_RESOLVER$" in dockerfile
    assert "/etc/resolv.conf" in resolver_envsh
    assert 'export DISCO_NGINX_RESOLVER="$disco_nginx_resolver"' in resolver_envsh

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_awk = fake_bin / "awk"
    fake_awk.write_text("#!/bin/sh\nprintf '%s\\n' \"$DISCO_TEST_RESOLVER\"\n")
    fake_awk.chmod(0o755)
    script = root / "frontend" / "docker-entrypoint.d" / "10-disco-resolver.envsh"
    command = f'set -e; . "{script}"; printf "%s" "$DISCO_NGINX_RESOLVER"'

    for supplied, rendered in (
        ("10.89.0.1", "10.89.0.1"),
        ("fd00::53", "[fd00::53]"),
    ):
        result = subprocess.run(
            ["sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "DISCO_TEST_RESOLVER": supplied,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            },
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == rendered

    missing = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DISCO_TEST_RESOLVER": "",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert missing.returncode != 0
    assert "no runtime nameserver" in missing.stderr


def test_unversioned_preview_host_is_no_longer_served() -> None:
    assert PREVIEW_HOST_RE.fullmatch("aaaaaaaa-8000.localhost") is None


@pytest.mark.asyncio
async def test_post_passthrough(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        resp = await client.post("/api/submit", content=b"mybody")
        assert resp.status_code == 201
        data = resp.json()
        assert data["method"] == "POST"
        assert data["path"] == "/api/submit"
        assert data["body"] == "mybody"


@pytest.mark.asyncio
async def test_in_sandbox_get_fallback_never_converts_a_mutation_to_get() -> None:
    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        async def fetch_inside(self, port: int, path: str):
            self.calls.append((port, path))
            return 200, b"inside GET", "text/plain"

    session = Session()
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda *_args: None,
        session_resolver=lambda *_args: session,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        mutation = await client.post("/api/submit", content=b"must not become GET")
        assert mutation.status_code == 503
        assert session.calls == []

        get = await client.get("/asset.js")
        assert get.status_code == 200
        assert get.text == "inside GET"
        assert session.calls == [(8000, "asset.js")]


@pytest.mark.asyncio
async def test_published_get_exhaustion_uses_authenticated_session_fallback_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        async def fetch_inside(self, port: int, path: str):
            self.calls.append((port, path))
            return 200, b"authenticated fallback", "text/plain"

    failing_client = FailingPublishedClient(
        message=(
            "https://private.invalid/asset.js?token=super-secret Authorization: Bearer super-secret"
        )
    )
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: failing_client)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    caplog.set_level("WARNING", logger="disco.agent_server.host_proxy")

    session = Session()
    resolver_calls: list[tuple[str, str | None]] = []

    def session_resolver(cid8: str, owner_id: str | None):
        resolver_calls.append((cid8, owner_id))
        return session

    store = SqliteEventStore(":memory:")
    inner = Starlette()
    app = HostPreviewProxyMiddleware(
        inner,
        upstream_resolver=lambda *_args: "http://published.invalid:8000",
        session_resolver=session_resolver,
        require_capability=True,
        redemption_store=store,
    )
    signer = PreviewCapabilitySigner(redemption_store=store)
    auth_session = AuthSession(
        owner_id="owner-fallback",
        csrf_token="csrf",
        session_id="session",
        expires_at=2**31,
    )
    intent = signer.mint_intent(
        session=auth_session,
        conversation_id="conv_aaaaaaaafull",
        port=8000,
        target_path="/asset.js?mode=proof",
        allow_websocket=False,
        http_methods=("GET",),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://p2-aaaaaaaa-8000.localhost",
    ) as client:
        redemption = await client.post(PREVIEW_BOOTSTRAP_PATH, data={"intent": intent})
        assert redemption.status_code == 200
        capability_cookie = redemption.headers["set-cookie"].split(";", 1)[0]
        response = await client.get(
            "/asset.js?mode=proof",
            headers={"Cookie": capability_cookie},
        )

    assert response.status_code == 200
    assert response.text == "authenticated fallback"
    assert failing_client.calls == 5
    assert resolver_calls == [("aaaaaaaa", "owner-fallback")]
    assert session.calls == [(8000, "asset.js?mode=proof")]
    published_logs = [
        record.getMessage()
        for record in caplog.records
        if "published upstream transport exhausted" in record.getMessage()
    ]
    assert published_logs == [
        "preview published upstream transport exhausted attempts=5 "
        "category=connect class=ConnectError in_session_fallback=success"
    ]
    assert "super-secret" not in "\n".join(record.getMessage() for record in caplog.records)
    store.close()


@pytest.mark.asyncio
async def test_published_and_session_failure_emit_one_sanitized_502(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_connect_error = type("C" * 100, (httpx.ConnectError,), {})
    failing_client = FailingPublishedClient(
        message=(
            "https://private.invalid/?token=super-secret Cookie: disco_preview_cap=super-secret"
        ),
        error_type=long_connect_error,
    )
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: failing_client)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    caplog.set_level("WARNING", logger="disco.agent_server.host_proxy")

    class BrokenSession:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_inside(self, _port: int, _path: str):
            self.calls += 1
            raise RuntimeError(
                "Authorization: Bearer fallback-secret https://inside.invalid/private"
            )

    session = BrokenSession()
    proxy = HostPreviewProxyMiddleware(
        Starlette(),
        upstream_resolver=lambda *_args: "http://published.invalid:8000",
        session_resolver=lambda *_args: session,
    )
    response_statuses: list[int] = []

    async def observed_app(scope, receive, send):
        async def observed_send(message):
            if message.get("type") == "http.response.start":
                response_statuses.append(message["status"])
            await send(message)

        await proxy(scope, receive, observed_send)

    transport = httpx.ASGITransport(app=observed_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://p2-aaaaaaaa-8000.localhost",
    ) as client:
        response = await client.get("/private?token=request-secret")

    assert response.status_code == 502
    assert response.text == "preview upstream unreachable"
    assert response_statuses == [502]
    assert failing_client.calls == 5
    assert session.calls == 1

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "preview in-session fallback failed category=internal class=RuntimeError" in logs
    assert (
        "preview published upstream transport exhausted attempts=5 category=connect "
        f"class={'C' * 64} in_session_fallback=unavailable" in logs
    )
    assert "C" * 65 not in logs
    for secret_fragment in (
        "super-secret",
        "fallback-secret",
        "request-secret",
        "Authorization",
        "Cookie",
        "https://",
    ):
        assert secret_fragment not in logs


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def test_published_non_get_failure_never_uses_session_fallback(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    failing_client = FailingPublishedClient()
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: failing_client)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    session_resolver_calls: list[str] = []

    def session_resolver(cid8: str):
        session_resolver_calls.append(cid8)
        raise AssertionError("non-GET fallback must stay fail-closed")

    app = HostPreviewProxyMiddleware(
        Starlette(),
        upstream_resolver=lambda *_args: "http://published.invalid:8000",
        session_resolver=session_resolver,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://p2-aaaaaaaa-8000.localhost",
    ) as client:
        response = await client.request(method, "/action", content=b"must not be replayed")

    assert response.status_code == 502
    assert session_resolver_calls == []
    expected_attempts = 5 if method in {"HEAD", "OPTIONS"} else 1
    assert failing_client.calls == expected_attempts


@pytest.mark.asyncio
async def test_published_novnc_failure_does_not_bypass_in_session_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from disco.tools.sandbox._container import NOVNC_PORT

    failing_client = FailingPublishedClient()
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: failing_client)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    resolver_calls: list[str] = []

    def session_resolver(cid8: str):
        resolver_calls.append(cid8)
        raise AssertionError("noVNC must never reach the in-session fallback")

    app = HostPreviewProxyMiddleware(
        Starlette(),
        upstream_resolver=lambda *_args: f"http://published.invalid:{NOVNC_PORT}",
        session_resolver=session_resolver,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"http://p2-aaaaaaaa-{NOVNC_PORT}.localhost",
    ) as client:
        response = await client.get("/vnc.html")

    assert response.status_code == 502
    assert failing_client.calls == 5
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_preview_proxy_never_exposes_or_accepts_internal_capability_cookie(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        response = await client.get(
            "/cookies",
            headers=[
                (
                    "Cookie",
                    "disco_preview_cap=secret; app_cookie=one; "
                    "disco_preview_cap_extra=similarly-named",
                ),
                ("cookie", "other_cookie=two; Disco_preview_cap=case-distinct"),
            ],
        )
    assert response.status_code == 200
    upstream_cookie_text = "; ".join(response.json()["cookies"])
    assert "disco_preview_cap=secret" not in upstream_cookie_text
    assert "app_cookie=one" in upstream_cookie_text
    assert "other_cookie=two" in upstream_cookie_text
    assert "disco_preview_cap_extra=similarly-named" in upstream_cookie_text
    assert "Disco_preview_cap=case-distinct" in upstream_cookie_text

    set_cookies = response.headers.get_list("set-cookie")
    assert not any(cookie.lstrip().startswith("disco_preview_cap=") for cookie in set_cookies)
    assert "app_session=preserved; Path=/" in set_cookies
    assert "disco_preview_cap_extra=preserved; Path=/" in set_cookies
    assert "Disco_preview_cap=case-distinct; Path=/" in set_cookies


@pytest.mark.asyncio
async def test_preview_proxy_rejects_declared_oversized_request_before_upstream(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        response = await client.post(
            "/api/submit", content=b"x" * (MAX_PREVIEW_REQUEST_BODY_BYTES + 1)
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_preview_proxy_rejects_chunked_oversized_request_without_content_length(mock_app):
    async def chunks():
        for _ in range(5):
            yield b"x" * (MAX_PREVIEW_REQUEST_BODY_BYTES // 4)

    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        response = await client.post("/api/submit", content=chunks())
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_redirect_untouched(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://p2-aaaaaaaa-8000.localhost",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/redirect")
        assert resp.status_code == 301
        assert resp.headers["location"] == "/new-location"


def test_canonical_redirect_rewrites_only_the_exact_managed_upstream_origin() -> None:
    scope: Scope = {
        "type": "http",
        "scheme": "http",
        "headers": [(b"host", b"127.0.0.2:19120")],
    }

    def rewrite(value: str) -> str:
        return _rewrite_canonical_upstream_location(
            value,
            upstream="http://127.0.0.1:5173",
            scope=scope,
        )

    assert rewrite("http://127.0.0.1:5173/account?next=1#profile") == (
        "http://127.0.0.2:19120/account?next=1#profile"
    )
    assert rewrite("/account") == "/account"
    assert rewrite("https://example.com/account") == "https://example.com/account"
    assert rewrite("http://127.0.0.1:5174/account") == "http://127.0.0.1:5174/account"


@pytest.mark.asyncio
async def test_oversized_html_streams_without_buffering_for_injection(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        response = await client.get("/huge-html")
    assert response.status_code == 200
    assert len(response.content) > 3 * 1024 * 1024
    assert b"disco-element-mention-picker" not in response.content


@pytest.mark.asyncio
async def test_unknown_port(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-9999.localhost"
    ) as client:
        resp = await client.get("/")
        assert resp.status_code == 404
        assert b"unknown port" in resp.content


@pytest.mark.asyncio
async def test_internal_port_forbidden(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-aaaaaaaa-8899.localhost"
    ) as client:
        resp = await client.get("/")
        assert resp.status_code == 404
        assert b"unknown port" in resp.content


@pytest.mark.asyncio
async def test_unknown_cid8_resolver_none(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-cccccccc-8000.localhost"
    ) as client:
        resp = await client.get("/")
        assert resp.status_code == 503
        assert b"preview not available" in resp.content


@pytest.mark.asyncio
async def test_upstream_down(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p2-bbbbbbbb-8000.localhost"
    ) as client:
        resp = await client.get("/")
        assert resp.status_code == 502
        assert b"preview upstream unreachable" in resp.content


@pytest.mark.asyncio
async def test_host_header_not_matching(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://normal-host.com") as client:
        resp = await client.get("/existing")
        assert resp.status_code == 200
        assert resp.text == "real app route works"


# --- WebSocket Test ---


class EchoWSEndpoint(WebSocketEndpoint):
    async def on_connect(self, websocket):
        proto = websocket.headers.get("sec-websocket-protocol", "")
        await websocket.accept(subprotocol="vite-hmr" if "vite-hmr" in proto else None)

    async def on_receive(self, websocket, data):
        if isinstance(data, str):
            await websocket.send_text(data)
        else:
            await websocket.send_bytes(data)


@pytest.fixture
async def real_ws_server():
    import socket

    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()

    app = Starlette(routes=[WebSocketRoute("/ws", EchoWSEndpoint)])

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        ws="websockets-sansio",
    )
    server = uvicorn.Server(config)

    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    yield f"ws://127.0.0.1:{port}"

    server.should_exit = True
    await task


@pytest.fixture
async def proxy_app_server(real_ws_server):
    import socket

    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()

    app = Starlette()

    def resolver(cid8, rport):
        if cid8 == "aaaaaaaa" and rport == 8000:
            return real_ws_server
        return None

    app.add_middleware(HostPreviewProxyMiddleware, upstream_resolver=resolver)

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="debug",
        ws="websockets-sansio",
    )
    server = uvicorn.Server(config)

    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    yield f"ws://127.0.0.1:{port}"

    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_websocket_proxy(proxy_app_server, real_ws_server):
    port = int(proxy_app_server.split(":")[-1])
    # Test real_ws_server directly
    async with websockets.connect(f"{real_ws_server}/ws", subprotocols=["vite-hmr"]) as ws:
        await ws.send("hello text")
        resp = await ws.recv()
        assert resp == "hello text"

    # Test proxy
    async with websockets.connect(
        f"ws://p2-aaaaaaaa-8000.localhost:{port}/ws", subprotocols=["vite-hmr"]
    ) as ws:
        assert ws.subprotocol == "vite-hmr"

        await ws.send("hello text")
        resp = await ws.recv()
        assert resp == "hello text"

        await ws.send(b"hello bytes")
        resp = await ws.recv()
        assert resp == b"hello bytes"


@pytest.fixture
async def capability_proxy_app_server(real_ws_server, tmp_path):
    import socket

    sock = socket.socket()
    sock.bind(("", 0))
    server_port = sock.getsockname()[1]
    sock.close()
    store = SqliteEventStore(tmp_path / "capability-proxy.sqlite3")
    signer = PreviewCapabilitySigner(redemption_store=store)
    session = AuthSession("owner-a", "csrf", "session", 2**31)
    intent = signer.mint_intent(
        session=session,
        conversation_id="conv_aaaaaaaafull",
        port=8000,
        target_path="/ws",
        allow_websocket=True,
    )
    redeemed = signer.redeem_intent(intent, cid8="aaaaaaaa", port=8000, path_scope="host")
    assert redeemed is not None
    capability_token, _target = redeemed

    app = Starlette()

    def resolver(cid8, port, owner_id=None):
        if cid8 == "aaaaaaaa" and port == 8000 and owner_id == "owner-a":
            return real_ws_server
        return None

    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=resolver,
        require_capability=True,
        redemption_store=store,
    )
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=server_port,
        log_level="critical",
        ws="websockets-sansio",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    yield server_port, capability_token

    server.should_exit = True
    await task
    store.close()


@pytest.mark.asyncio
async def test_signed_live_capability_authorizes_only_exact_origin_cid_and_port(
    capability_proxy_app_server,
    real_ws_server,
):
    server_port, token = capability_proxy_app_server
    host = f"p2-aaaaaaaa-8000.localhost:{server_port}"
    url = f"ws://{host}/ws"
    headers = {"Cookie": f"{PREVIEW_COOKIE}={token}"}

    # Control: the upstream accepts a genuinely absent subprotocol header.
    async with websockets.connect(f"{real_ws_server}/ws") as direct:
        await direct.send("no-subprotocol-control")
        assert await direct.recv() == "no-subprotocol-control"

    async with websockets.connect(
        url,
        origin=f"http://{host}",
        additional_headers=headers,
    ) as ws:
        await ws.send("capability-hmr")
        assert await ws.recv() == "capability-hmr"

    for duplicate_headers in (
        [("Cookie", f"{PREVIEW_COOKIE}=invalid; {PREVIEW_COOKIE}={token}")],
        [("Cookie", f"{PREVIEW_COOKIE}={token}; {PREVIEW_COOKIE}=invalid")],
        [("Cookie", f"{PREVIEW_COOKIE}=invalid"), ("Cookie", f"{PREVIEW_COOKIE}={token}")],
    ):
        async with websockets.connect(
            url,
            origin=f"http://{host}",
            additional_headers=duplicate_headers,
        ) as ws:
            await ws.send("duplicate-capability-hmr")
            assert await ws.recv() == "duplicate-capability-hmr"

    # Origin is checked before the signed cookie; rejecting it does not mutate
    # or consume the reusable scoped preview cookie.
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(
            url,
            origin="https://evil.example",
            additional_headers=headers,
        ):
            pass

    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(
            url,
            origin=f"https://{host}",
            additional_headers=headers,
        ):
            pass

    for wrong_url, wrong_origin in (
        (
            f"ws://p2-bbbbbbbb-8000.localhost:{server_port}/ws",
            f"http://p2-bbbbbbbb-8000.localhost:{server_port}",
        ),
        (
            f"ws://p2-aaaaaaaa-8899.localhost:{server_port}/ws",
            f"http://p2-aaaaaaaa-8899.localhost:{server_port}",
        ),
    ):
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(
                wrong_url,
                origin=wrong_origin,
                additional_headers=headers,
            ):
                pass


@pytest.mark.asyncio
async def test_async_resolver_awaited():
    async def async_resolver(cid8, port):
        await asyncio.sleep(0.01)
        if cid8 == "aaaaaaaa" and port == 8000:
            return "http://fake-upstream"
        return None

    app = Starlette()
    app.add_middleware(HostPreviewProxyMiddleware, upstream_resolver=async_resolver)

    transport = httpx.ASGITransport(app=app)
    base = "http://p2-aaaaaaaa-8000.localhost"
    async with httpx.AsyncClient(transport=transport, base_url=base) as client:
        resp = await client.get("/")
        # If the middleware didn't await the coroutine, the resolver would read as
        # falsy -> 503. The awaited (dead) fake-upstream gives 502 instead.
        assert resp.status_code == 502
        assert b"preview upstream unreachable" in resp.content
