import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from perpleximanus.agent_server.host_proxy import HostPreviewProxyMiddleware

class EchoHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/new-location")
            self.end_headers()
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
        self.wfile.write(json.dumps({"path": self.path, "method": "POST", "body": body.decode()}).encode())

@pytest.fixture(scope="module")
def upstream_http():
    server = HTTPServer(("127.0.0.1", 0), EchoHTTPRequestHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join()

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
            return "http://127.0.0.1:59999" # Dead upstream
        return None

    app.add_middleware(HostPreviewProxyMiddleware, upstream_resolver=resolver)
    return app

@pytest.mark.asyncio
async def test_get_passthrough(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aaaaaaaa-8000.localhost") as client:
        # standard GET, path /src/main.jsx, query string
        resp = await client.get("/src/main.jsx?foo=bar")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        data = resp.json()
        assert data["method"] == "GET"
        assert data["path"] == "/src/main.jsx?foo=bar"

@pytest.mark.asyncio
async def test_post_passthrough(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aaaaaaaa-8000.localhost") as client:
        resp = await client.post("/api/submit", content=b"mybody")
        assert resp.status_code == 201
        data = resp.json()
        assert data["method"] == "POST"
        assert data["path"] == "/api/submit"
        assert data["body"] == "mybody"

@pytest.mark.asyncio
async def test_redirect_untouched(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aaaaaaaa-8000.localhost", follow_redirects=False) as client:
        resp = await client.get("/redirect")
        assert resp.status_code == 301
        assert resp.headers["location"] == "/new-location"

@pytest.mark.asyncio
async def test_unknown_port(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aaaaaaaa-9999.localhost") as client:
        resp = await client.get("/")
        assert resp.status_code == 404
        assert b"unknown port" in resp.content

@pytest.mark.asyncio
async def test_internal_port_forbidden(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aaaaaaaa-8899.localhost") as client:
        resp = await client.get("/")
        assert resp.status_code == 404
        assert b"unknown port" in resp.content

@pytest.mark.asyncio
async def test_unknown_cid8_resolver_none(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cccccccc-8000.localhost") as client:
        resp = await client.get("/")
        assert resp.status_code == 503
        assert b"preview not available" in resp.content

@pytest.mark.asyncio
async def test_upstream_down(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bbbbbbbb-8000.localhost") as client:
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

from starlette.routing import Route, WebSocketRoute
from starlette.endpoints import WebSocketEndpoint
from starlette.routing import WebSocketRoute

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
    
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="critical")
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
    
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="debug")
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
    async with websockets.connect(f"ws://aaaaaaaa-8000.localhost:{port}/ws", subprotocols=["vite-hmr"]) as ws:
        assert ws.subprotocol == "vite-hmr"
        
        await ws.send("hello text")
        resp = await ws.recv()
        assert resp == "hello text"
        
        await ws.send(b"hello bytes")
        resp = await ws.recv()
        assert resp == b"hello bytes"

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
    base = "http://aaaaaaaa-8000.localhost"
    async with httpx.AsyncClient(transport=transport, base_url=base) as client:
        resp = await client.get("/")
        # If the middleware didn't await the coroutine, the resolver would read as
        # falsy -> 503. The awaited (dead) fake-upstream gives 502 instead.
        assert resp.status_code == 502
        assert b"preview upstream unreachable" in resp.content
