"""Fix 2 (B-H.2) — operator surfaces shell-served sites via the widened preview gate.

A shell-served site (`python3 -m http.server`) writes no index.html OBSERVATION and
emits no app-deliverable, so the operator's old gate (files[index.html] OR an
app-deliverable) left it invisible — `operator view` reported no preview even though
a live page existed. The widened gate ALSO probes when GET /preview reports a live
port (runtime.preview() owners → available/ports). These tests drive the OperatorClient
against a tiny threaded HTTP stub of the agent-server endpoints view() touches.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from disco.agent_server.verify.operator import OperatorClient


class _Stub(BaseHTTPRequestHandler):
    # Per-process knobs the tests set before each run.
    preview_available = False
    serve_page = True

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.endswith("/state"):
            self._send(200, json.dumps({"execution_status": "RUNNING"}).encode())
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

    def log_message(self, *args: object) -> None:  # silence
        pass


def _serve() -> tuple[str, HTTPServer]:
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
        op = OperatorClient(base)
        view = asyncio.run(op.view("conv_shellserved"))
        assert "preview" in view, "operator should have probed the live preview"
        assert view["preview"]["status"] == 200
        assert "shell served" in view["preview"]["html_head"].lower()
    finally:
        srv.shutdown()


def test_operator_skips_probe_when_no_signal() -> None:
    """No index.html, no app-deliverable, AND no live port → no probe (unchanged)."""
    _Stub.preview_available = False
    _Stub.serve_page = True
    base, srv = _serve()
    try:
        op = OperatorClient(base)
        view = asyncio.run(op.view("conv_nothing"))
        assert "preview" not in view
    finally:
        srv.shutdown()
