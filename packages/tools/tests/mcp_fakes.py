"""MCP fakes for testing — RP-05 rung A.

FakeStdioServer: an asyncio server that speaks the MCP JSON-RPC protocol over
stdio (real subprocess boundary). Exposes tools: echo, add, read_file, list_files.

Run as: `python -m packages.tools.tests.mcp_fakes`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile


class FakeStdioServer:
    """A minimal MCP JSON-RPC server speaking over stdin/stdout streams.

    Responds to: initialize, notifications/initialized, tools/list, tools/call.
    """

    def __init__(self) -> None:
        self._tempdir: str | None = None

    async def run(self) -> None:
        """Async loop: read JSON-RPC lines from stdin, write responses to stdout.
        Also writes diagnostic messages to stderr (for D4 stderr capture testing)."""
        self._tempdir = tempfile.mkdtemp(prefix="mcp_fake_")
        test_file = os.path.join(self._tempdir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Hello from MCP fake server!\n")

        # Write a diagnostic message to stderr (D4: real stderr capture)
        sys.stderr.write("MCP fake server diagnostic: tempdir=" + self._tempdir + "\n")
        sys.stderr.flush()

        # Use asyncio StreamReader on stdin (non-blocking)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        transport, _ = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout
        )
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)

        while True:
            try:
                line = await reader.readline()
            except Exception:
                break
            if not line:
                break
            line_str = line.decode().strip()
            if not line_str:
                continue
            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                err = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Parse error"},
                        "id": None,
                    }
                )
                writer.write((err + "\n").encode())
                await writer.drain()
                continue

            method = msg.get("method", "")
            msg_id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-mcp-server", "version": "1.0.0"},
                    },
                }
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo back the message",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                    "required": ["message"],
                                },
                            },
                            {
                                "name": "add",
                                "description": "Add two numbers together",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer"},
                                        "b": {"type": "integer"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                            {
                                "name": "read_file",
                                "description": "Read a file from the server temp dir",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            },
                            {
                                "name": "list_files",
                                "description": "List files in the temp dir",
                                "inputSchema": {"type": "object", "properties": {}},
                            },
                        ]
                    },
                }
            elif method == "tools/call":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": self._call_tool(params),
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                    "id": msg_id,
                }

            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()

        writer.close()

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            if name == "echo":
                msg = args.get("message", "")
                text = f"Echo: {msg}"
            elif name == "add":
                a = int(args.get("a", 0))
                b = int(args.get("b", 0))
                text = str(a + b)
            elif name == "read_file":
                path = args.get("path", "")
                full = os.path.join(self._tempdir, path)
                if not os.path.abspath(full).startswith(self._tempdir or ""):
                    text = "Error: path traversal denied"
                elif not os.path.exists(full):
                    text = f"Error: file not found: {path}"
                else:
                    with open(full) as f:
                        text = f.read()
            elif name == "list_files":
                text = json.dumps(sorted(os.listdir(self._tempdir or ".")))
            else:
                text = f"Error: unknown tool: {name}"
        except Exception as exc:
            text = f"Error: {exc}"
        return {"content": [{"type": "text", "text": text}], "isError": False}


if __name__ == "__main__":
    asyncio.run(FakeStdioServer().run())
