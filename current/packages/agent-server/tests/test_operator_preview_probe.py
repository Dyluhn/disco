"""Fix 2 (B-H.2) — operator surfaces shell-served sites via the widened preview gate.

A shell-served site (`python3 -m http.server`) writes no index.html OBSERVATION and
emits no app-deliverable, so the operator's old gate (files[index.html] OR an
app-deliverable) left it invisible — `operator view` reported no preview even though
a live page existed. The widened gate ALSO probes when GET /preview reports a live
port (runtime.preview.preview() owners → available/ports). These tests drive the OperatorClient
against a tiny threaded HTTP stub of the agent-server endpoints view() touches.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
from disco.agent_server.verify import operator as operator_module
from disco.agent_server.verify.operator import OperatorClient
from disco.agent_server.verify.runner import AbstractVerifyClient, HttpVerifyClient


def test_gate_context_preserves_latest_tool_question_semantics() -> None:
    events = [
        {
            "kind": "action",
            "tool_call": {"name": "ask_user", "arguments": "first question"},
        },
        {
            "kind": "action",
            "tool_call": {"name": "ask_user", "arguments": "latest question"},
        },
    ]
    context = OperatorClient._gate_context("AWAITING_USER_QUESTION", events)
    assert context["question"] == "latest question"


class _CapabilityPreviewClient(AbstractVerifyClient):
    def __init__(self, response: tuple[int, bytes] | None) -> None:
        self.response = response
        self.calls: list[str] = []

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        self.calls.append(cid)
        return self.response

    async def create_conversation(self, *args, **kwargs):
        raise NotImplementedError

    async def run_ws_exchange(self, *args, **kwargs):
        raise NotImplementedError

    async def poll_until_terminal(self, *args, **kwargs):
        raise NotImplementedError

    async def get_events(self, *args, **kwargs):
        raise NotImplementedError

    async def get_state(self, *args, **kwargs):
        raise NotImplementedError

    async def get_trace(self, *args, **kwargs):
        raise NotImplementedError

    async def get_manifest(self, *args, **kwargs):
        raise NotImplementedError


class _Stub(BaseHTTPRequestHandler):
    # Per-process knobs the tests set before each run.
    preview_available = False
    serve_page = True
    conversation_status = "RUNNING"
    unauthenticated_private_calls = 0

    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str = "application/json",
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        return "disco_session=operator-proof" in (self.headers.get("Cookie") or "")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/session":
            self._send(
                200,
                json.dumps(
                    {
                        "authenticated": self._authenticated(),
                        "csrf_token": "operator-csrf" if self._authenticated() else "",
                    }
                ).encode(),
            )
            return
        if path == "/api/auth/pairing-token":
            self._send(200, json.dumps({"pairing_token": "operator-pair"}).encode())
            return
        if not self._authenticated():
            type(self).unauthenticated_private_calls += 1
            self._send(401, b"{}")
        elif path == "/conversations":
            self._send(200, json.dumps({"conversation_ids": ["conv_operatorproof"]}).encode())
        elif path.endswith("/state"):
            self._send(
                200,
                json.dumps({"execution_status": self.conversation_status}).encode(),
            )
        elif path.endswith("/events"):
            # No file_write/index.html observation and no app-deliverable — exactly
            # the shell-served blind spot.
            self._send(200, json.dumps([]).encode())
        elif path.endswith("/preview"):
            if self.preview_available:
                payload = {"available": True, "ports": [{"port": 8000}]}
            else:
                payload = {"available": False, "ports": []}
            self._send(200, json.dumps(payload).encode())
        elif "/preview-app/" in path:
            if self.serve_page:
                self._send(200, b"<html><title>Shell Served</title></html>", "text/html")
            else:
                self._send(503, b"preview not available", "text/plain")
        else:
            self._send(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/mint":
            self._send(
                200,
                json.dumps({"csrf_token": "operator-csrf"}).encode(),
                headers={"Set-Cookie": "disco_session=operator-proof; Path=/; HttpOnly"},
            )
        else:
            self._send(404, b"{}")

    def log_message(self, *args: object) -> None:  # silence
        pass


def _serve() -> tuple[str, HTTPServer]:
    _Stub.unauthenticated_private_calls = 0
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def test_operator_probes_preview_when_live_port_detected() -> None:
    """No index.html observation, no app-deliverable — but a live preview port IS
    reported, so the operator fetches and surfaces the rendered page (B-H.2)."""
    _Stub.preview_available = True
    _Stub.serve_page = True
    base, srv = _serve()
    try:
        preview = _CapabilityPreviewClient((200, b"<html><title>Shell Served</title></html>"))
        op = OperatorClient(base, _preview_client=preview)
        view = asyncio.run(op.view("conv_shellserved"))
        assert "preview" in view, "operator should have probed the live preview"
        assert view["preview"]["status"] == 200
        assert "shell served" in view["preview"]["html_head"].lower()
        assert view["preview"]["url"] == "/__disco/isolated-preview/conv_shellserved/"
        assert preview.calls == ["conv_shellserved"]
        assert _Stub.unauthenticated_private_calls == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_operator_skips_probe_when_no_signal() -> None:
    """No index.html, no app-deliverable, AND no live port → no probe (unchanged)."""
    _Stub.preview_available = False
    _Stub.serve_page = True
    base, srv = _serve()
    try:
        preview = _CapabilityPreviewClient((200, b"unused"))
        op = OperatorClient(base, _preview_client=preview)
        view = asyncio.run(op.view("conv_nothing"))
        assert "preview" not in view
        assert preview.calls == []
        assert _Stub.unauthenticated_private_calls == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_operator_cli_list_and_wait_any_authenticate_every_get(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact list and ``wait --any`` CLI paths must pair before their private
    GET /conversations, state, and events requests."""
    _Stub.conversation_status = "FINISHED"
    base, srv = _serve()
    try:
        assert operator_module.main(["--agent-base", base, "list"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed == {
            "count": 1,
            "active": 0,
            "conversations": [{"cid": "conv_operatorproof", "status": "FINISHED"}],
        }

        assert operator_module.main(["--agent-base", base, "wait", "--any", "--timeout", "0"]) == 0
        waited = json.loads(capsys.readouterr().out)
        assert waited["cid"] == "conv_operatorproof"
        assert waited["status"] == "FINISHED"
        assert waited["terminal"] is True
        assert _Stub.unauthenticated_private_calls == 0
    finally:
        _Stub.conversation_status = "RUNNING"
        srv.shutdown()
        srv.server_close()


async def test_operator_authenticates_http_mutations_and_websocket_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={"authenticated": False})
        if request.url.path == "/api/auth/pairing-token":
            return httpx.Response(200, json={"pairing_token": "operator-pair"})
        if request.url.path == "/api/auth/mint":
            return httpx.Response(
                200,
                json={"csrf_token": "operator-csrf"},
                headers={"set-cookie": "disco_session=operator-proof; Path=/; HttpOnly"},
            )
        assert request.headers["cookie"] == "disco_session=operator-proof"
        if request.url.path == "/conversations" and request.method == "POST":
            assert request.headers["x-disco-csrf"] == "operator-csrf"
            return httpx.Response(200, json={"conversation_id": "conv_operatorproof"})
        if request.url.path == "/conversations/conv_operatorproof/state":
            return httpx.Response(200, json={"execution_status": "AWAITING_PLAN_APPROVAL"})
        return httpx.Response(404)

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        async def recv(self, *, decode: bool = False) -> str:
            del decode
            return json.dumps({"type": "state", "state": {"execution_status": "RUNNING"}})

    sockets: list[_FakeWebSocket] = []
    connects: list[tuple[str, dict[str, Any]]] = []

    @contextlib.asynccontextmanager
    async def fake_connect(url: str, **kwargs: Any):
        ws = _FakeWebSocket()
        sockets.append(ws)
        connects.append((url, kwargs))
        yield ws

    monkeypatch.setattr(operator_module, "_ws_connect", fake_connect)
    auth = HttpVerifyClient("http://127.0.0.1:8000", _transport=httpx.MockTransport(handler))
    operator = OperatorClient("http://127.0.0.1:8000", _auth_client=auth)

    started = await operator.start("build", "Build the selected app")
    assert started["cid"] == "conv_operatorproof"
    responded = await operator.respond("conv_operatorproof", "approve")
    assert responded["ok"] is True
    assert responded["status_before"] == "AWAITING_PLAN_APPROVAL"
    assert responded["status_after"] == "RUNNING"
    assert [socket.sent for socket in sockets] == [
        [{"type": "send_message", "content": "Build the selected app"}],
        [{"type": "approve_plan"}],
    ]
    for url, kwargs in connects:
        assert url == "ws://127.0.0.1:8000/ws/conversations/conv_operatorproof"
        assert kwargs["origin"] == "http://127.0.0.1:8000"
        assert kwargs["additional_headers"] == {"Cookie": "disco_session=operator-proof"}
    assert ("POST", "/conversations") in seen
