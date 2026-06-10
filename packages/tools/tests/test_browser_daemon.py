import asyncio
import os
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from perpleximanus.tools.anatomy import ToolContext
from perpleximanus.tools.builtin.browser import BrowserTool
from perpleximanus.tools.sandbox.base import ExecResult


@pytest.mark.asyncio
async def test_browser_render_observation():
    tool = BrowserTool()
    data = {
        "ok": True,
        "url": "http://example.com",
        "title": "Example Domain",
        "console": [
            {"level": "error", "text": "Uncaught TypeError"},
            {"level": "warning", "text": "Deprecation warning"}
        ],
        "elements": [
            "1[:]<button>Click me</button>",
            "2[:]<a>Link</a>"
        ],
        "text": "Hello world",
        "screenshot_path": ".pmx/screenshots/0001-navigate.png"
    }
    rendered = tool._render_observation(data)
    assert "[UNTRUSTED WEB CONTENT" in rendered
    assert "URL: http://example.com" in rendered
    assert "TITLE: Example Domain" in rendered
    assert "CONSOLE (1 errors, 1 warnings):" in rendered
    assert "  - error: Uncaught TypeError" in rendered
    assert "ELEMENTS:" in rendered
    assert "1[:]<button>Click me</button>" in rendered
    assert "TEXT:\nHello world" in rendered
    assert "[END UNTRUSTED WEB CONTENT]" in rendered
    assert "screenshot: .pmx/screenshots/0001-navigate.png" in rendered

@pytest.mark.asyncio
async def test_browser_ensure_daemon_restart_on_failure():
    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sessions = AsyncMock()
    
    # First health check fails (exit 1), then succeeds (exit 0)
    ctx.sandbox.exec_shell.side_effect = [
        ExecResult(exit_code=1, stdout="", stderr=""), # health check 1
        ExecResult(exit_code=0, stdout="", stderr=""), # health check 2 (after start)
    ]
    
    await tool._ensure_daemon(ctx)
    
    # Verify it tried to write the daemon and start it
    assert ctx.sandbox.write_file.called
    assert ctx.sessions.exec.called
    assert ctx.sessions.exec.call_args[0] == (
        "__browser", "python3 /workspace/.pmx/_browser_daemon.py", None
    )

@pytest.mark.integration
@pytest.mark.asyncio
async def test_browser_daemon_integration_real_chromium(tmp_path):
    # This test needs a real browser and local daemon
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import httpx
    import perpleximanus.tools.builtin._browser_daemon as daemon_mod

    # 1. Setup a fixture page: a counter button, a cookie-setter, and a console error.
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    fixture_file = fixture_dir / "index.html"
    fixture_file.write_text("""
        <html>
        <body>
            <h1 id="counter">0</h1>
            <div id="cookies"></div>
            <button id="btn"
                onclick="const c = document.getElementById('counter');
                         c.textContent = parseInt(c.textContent) + 1;">Click</button>
            <script>
                document.cookie = "testcookie=456";
                console.error('boom');
                document.getElementById('cookies').textContent = document.cookie;
            </script>
        </body>
        </html>
    """)

    # Start a server for the fixture
    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(fixture_file.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()

    fixture_server = HTTPServer(('127.0.0.1', 0), FixtureHandler)
    fixture_port = fixture_server.server_port
    fixture_thread = threading.Thread(target=fixture_server.serve_forever, daemon=True)
    fixture_thread.start()

    # 2. Run the REAL daemon script as a subprocess (same entrypoint the sandbox uses),
    # with PMX_WORKSPACE pointed at tmp_path so screenshots land somewhere we can assert.
    daemon_script = pathlib.Path(daemon_mod.__file__).absolute()
    daemon_proc = subprocess.Popen(
        [sys.executable, str(daemon_script)],
        env={
            **os.environ,
            "PYTHONPATH": str(pathlib.Path(daemon_mod.__file__).parents[5]),
            "PMX_WORKSPACE": str(tmp_path)
        },
        cwd=str(tmp_path)
    )

    try:
        # Wait for daemon
        url = "http://127.0.0.1:8901"
        for _ in range(20):
            try:
                res = httpx.get(f"{url}/health", timeout=1.0)
                if res.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            pytest.fail("Daemon failed to start")

        # (a) navigate → "boom" in console, button in elements, screenshot file EXISTS
        client = httpx.AsyncClient(base_url=url)
        fixture_url = f"http://127.0.0.1:{fixture_port}/"
        res = await client.post("/", json={"action": "navigate", "url": fixture_url})
        data = res.json()
        assert data["ok"]
        assert any("boom" in c["text"] for c in data["console"])
        assert any("<button>Click</button>" in el for el in data["elements"])
        # screenshot_path is workspace-relative; PMX_WORKSPACE=tmp_path, so it must exist there
        assert data["screenshot_path"]
        assert (tmp_path / data["screenshot_path"]).is_file()

        # (b) click by index, then screenshot WITHOUT navigate → updated counter text
        # Find index of button
        import re
        btn_index = None
        for el in data["elements"]:
            m = re.match(r"(\d+)\[:\] <button>", el)
            if m:
                btn_index = int(m.group(1))
                break
        assert btn_index is not None

        res = await client.post("/", json={"action": "click", "index": btn_index})
        data = res.json()
        assert data["ok"]
        assert "1" in data["text"] # counter incremented

        # (c) page sets a cookie; re-navigate; cookie text still present
        # Our fixture sets a cookie. We can check if it's there via document.cookie
        res = await client.post("/", json={"action": "navigate", "url": fixture_url})
        data = res.json()
        assert "testcookie=456" in data["text"]

    finally:
        daemon_proc.terminate()
        fixture_server.shutdown()
