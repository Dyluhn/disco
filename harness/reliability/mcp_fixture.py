"""Deterministic Streamable-HTTP MCP server for live reliability campaigns.

The server exposes an exact-argument echo tool plus deterministic search/fetch
tools. Live tests can therefore prove settings -> approval -> runtime reload ->
workflow calls and MCP discovery -> grounded citation without depending on an
arbitrary third-party MCP service or a pre-seeded conversation.
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOOL_NAME = "reliability_echo"
SEARCH_TOOL_NAME = "search"
FETCH_TOOL_NAME = "fetch"
EXPECTED_TOKEN = "DISCO_MCP_LIVE_PROOF"
RESULT_MARKER = "MCP_RUNTIME_CALL_SUCCEEDED"
SEARCH_MARKER = "DISCO_MCP_SEARCH_PROOF"
SOURCE_URL = "https://www.rfc-editor.org/rfc/rfc9110.txt"
SOURCE_TITLE = "RFC 9110 — HTTP Semantics"
SOURCE_CONTENT = (
    f"{SEARCH_MARKER}. RFC 9110 defines safe methods as methods whose requested semantics are "
    "essentially read-only; clients do not request or expect state changes as a "
    "result of applying a safe method. GET, HEAD, OPTIONS, and TRACE are safe."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _FixtureState:
    def __init__(self, log_path: Path | None) -> None:
        self.log_path = log_path
        self._lock = threading.Lock()
        self.session_closed = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def record(self, payload: dict[str, Any]) -> None:
        row = {"at": _now(), **payload}
        with self._lock:
            self.calls.append(row)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")


class _Handler(BaseHTTPRequestHandler):
    server_version = "DiscoReliabilityMCP/1.0"

    @property
    def state(self) -> _FixtureState:
        return self.server.fixture_state  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(
        self,
        status: HTTPStatus,
        body: dict[str, Any],
        *,
        session: bool = False,
    ) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if session:
            self.send_header("Mcp-Session-Id", "disco-reliability-session")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "tool": TOOL_NAME, "calls": len(self.state.calls)},
            )
            return
        if self.path == "/calls":
            with self.state._lock:
                calls = list(self.state.calls)
            self._send_json(HTTPStatus.OK, {"calls": calls})
            return
        if self.path == "/mcp":
            # Streamable HTTP permits a long-lived GET SSE channel for
            # server-initiated messages. Returning 405 is protocol-valid for a
            # server that does not offer that channel, but the current official
            # client starts a bounded reconnect ladder on every lifecycle. The
            # reliability fixture implements the complete channel so its own
            # omissions cannot masquerade as product retry thrash.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while not self.state.session_closed.wait(0.25):
                    self.wfile.write(b": reliability keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED)

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # The official client terminates a Streamable-HTTP session on close.
        if self.path == "/mcp":
            self.state.session_closed.set()
            self._send_empty(HTTPStatus.NO_CONTENT)
            return
        self._send_empty(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/mcp":
            self._send_empty(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "invalid request"},
                },
            )
            return

        method = str(payload.get("method") or "")
        message_id = payload.get("id")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        self.state.record({"method": method, "params": params})

        if method == "notifications/initialized":
            self._send_empty(HTTPStatus.ACCEPTED)
            return
        if method == "initialize":
            self.state.session_closed.clear()
            requested = str(params.get("protocolVersion") or "2024-11-05")
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": requested,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "disco-reliability-mcp", "version": "1.0.0"},
                    },
                },
                session=True,
            )
            return
        if method == "tools/list":
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "tools": [
                            {
                                "name": TOOL_NAME,
                                "description": (
                                    "Reliability proof tool. Call exactly once with token "
                                    f"equal to {EXPECTED_TOKEN}; write its returned text into "
                                    "the workflow output file."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "token": {
                                            "type": "string",
                                            "description": f"Must equal {EXPECTED_TOKEN}.",
                                        }
                                    },
                                    "required": ["token"],
                                },
                            },
                            {
                                "name": SEARCH_TOOL_NAME,
                                "description": (
                                    "Search the reliability source corpus. Accepts a query and "
                                    "returns public web results suitable for grounded citations."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                },
                            },
                            {
                                "name": FETCH_TOOL_NAME,
                                "description": (
                                    "Fetch a source by id or URL from the reliability corpus."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "id": {"type": "string"},
                                        "url": {"type": "string"},
                                    },
                                },
                            },
                        ]
                    },
                },
                session=True,
            )
            return
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            valid = False
            if name == TOOL_NAME:
                token = str(arguments.get("token") or "")
                valid = token == EXPECTED_TOKEN
                text = (
                    f"{RESULT_MARKER}: {token}"
                    if valid
                    else f"invalid reliability echo token: {token!r}"
                )
            elif name == SEARCH_TOOL_NAME:
                query = str(arguments.get("query") or "").strip()
                valid = bool(query)
                text = json.dumps(
                    {
                        "results": [
                            {
                                "id": SOURCE_URL,
                                "title": SOURCE_TITLE,
                                "url": SOURCE_URL,
                                "snippet": f"{SEARCH_MARKER}. {SOURCE_CONTENT}",
                            }
                        ]
                        if valid
                        else []
                    }
                )
            elif name == FETCH_TOOL_NAME:
                source_id = str(arguments.get("id") or arguments.get("url") or "")
                valid = source_id == SOURCE_URL
                text = SOURCE_CONTENT if valid else f"unknown source id: {source_id!r}"
            else:
                text = f"unknown reliability tool: {name!r}"
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": not valid,
                    },
                },
                session=True,
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            },
            session=True,
        )


class ReliabilityMcpServer:
    """Threaded fixture server usable by unit tests and the command-line wrapper."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        log_path: Path | None = None,
    ) -> None:
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.fixture_state = _FixtureState(log_path)  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.fixture_state.session_closed.set()  # type: ignore[attr-defined]
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def serve_forever(self) -> None:
        self._server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic reliability MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log")
    parser.add_argument("--ready-file")
    args = parser.parse_args(argv)
    server = ReliabilityMcpServer(
        args.host,
        args.port,
        log_path=Path(args.log).resolve() if args.log else None,
    )

    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()
        threading.Thread(target=server._server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if args.ready_file:
        ready = Path(args.ready_file).resolve()
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text(
            json.dumps({"mcp_url": server.mcp_url, "pid": __import__("os").getpid()}),
            encoding="utf-8",
        )
    print(server.mcp_url, flush=True)
    try:
        server.serve_forever()
    finally:
        server._server.fixture_state.session_closed.set()  # type: ignore[attr-defined]
        server._server.server_close()
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
