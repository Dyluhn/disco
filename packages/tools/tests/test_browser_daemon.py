import asyncio
import os
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.browser import BrowserTool
from disco.tools.sandbox.base import ExecResult


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

class _FakeConsoleMessage:
    """A Playwright-like ConsoleMessage: .type / .text / .location."""

    def __init__(self, type, text, location=None):
        self.type = type
        self.text = text
        self.location = location or {}


class _FakePageError:
    """A Playwright-like PageError: .message / .stack."""

    def __init__(self, message, stack=None):
        self.message = message
        self.stack = stack


class _FakeRequest:
    def __init__(self, method, url, failure=None):
        self.method = method
        self.url = url
        self.failure = failure


def _fresh_state():
    import disco.tools.builtin._browser_daemon as daemon_mod

    return daemon_mod.BrowserState()


def test_daemon_stop_reports_failures_and_attempts_every_owner(capsys):
    """Partial teardown is bounded and visible, never a silent fallback."""

    class _BrokenResource:
        def __init__(self, label):
            self.label = label
            self.called = False

        def close(self):
            self.called = True
            raise RuntimeError(self.label)

    class _BrokenPlaywright:
        called = False

        def stop(self):
            self.called = True
            raise ValueError("playwright")

    state = _fresh_state()
    page = state.page = _BrokenResource("sensitive-page-detail")
    context = state.context = _BrokenResource("sensitive-context-detail")
    browser = state.browser = _BrokenResource("sensitive-browser-detail")
    playwright = state.playwright = _BrokenPlaywright()

    state.stop()

    assert page.called and context.called and browser.called and playwright.called
    assert state.page is state.context is state.browser is state.playwright is None
    diagnostic = capsys.readouterr().err
    assert "page:RuntimeError" in diagnostic
    assert "context:RuntimeError" in diagnostic
    assert "browser:RuntimeError" in diagnostic
    assert "playwright:ValueError" in diagnostic
    assert "sensitive-" not in diagnostic


def test_daemon_add_console_keeps_location():
    state = _fresh_state()
    state._add_console(
        _FakeConsoleMessage(
            "error",
            "Uncaught TypeError: x is undefined",
            {"url": "http://localhost:5173/app.js", "lineNumber": 42, "columnNumber": 7},
        )
    )
    assert state.console_logs[0]["level"] == "error"
    assert state.console_logs[0]["text"] == "Uncaught TypeError: x is undefined"
    assert state.console_logs[0]["location"]["url"] == "http://localhost:5173/app.js"
    assert state.console_logs[0]["location"]["lineNumber"] == 42


def test_daemon_add_pageerror_keeps_stack():
    state = _fresh_state()
    stack = "Error: boom\n    at foo (app.js:10:5)\n    at bar (app.js:20:3)"
    state._add_pageerror(_FakePageError("boom", stack))
    assert state.console_logs[0]["level"] == "error"
    assert state.console_logs[0]["text"] == "boom"
    assert state.console_logs[0]["stack"] == stack


def test_daemon_add_request_failed_records_network():
    state = _fresh_state()
    state._add_request_failed(
        _FakeRequest("GET", "http://localhost:5173/api/data", "net::ERR_CONNECTION_REFUSED")
    )
    assert state.network_fails[0] == {
        "method": "GET",
        "url": "http://localhost:5173/api/data",
        "failure": "net::ERR_CONNECTION_REFUSED",
    }


def test_daemon_network_ring_buffer_bounded():
    import disco.tools.builtin._browser_daemon as daemon_mod

    state = _fresh_state()
    for i in range(daemon_mod.MAX_NETWORK + 25):
        state._add_request_failed(_FakeRequest("GET", f"http://x/{i}", "boom"))
    assert len(state.network_fails) == daemon_mod.MAX_NETWORK


def test_daemon_clear_console_clears_network():
    state = _fresh_state()
    state._add_request_failed(_FakeRequest("GET", "http://x/", "boom"))
    state._add_console(_FakeConsoleMessage("log", "hi"))
    state.clear_console()
    assert state.console_logs == []
    assert state.network_fails == []


def test_render_observation_surfaces_stack_source_network_and_logs():
    tool = BrowserTool()
    data = {
        "ok": True,
        "url": "http://localhost:5173/",
        "title": "App",
        "console": [
            {"level": "log", "text": "boot sequence started"},
            {"level": "info", "text": "fetching config"},
            {
                "level": "error",
                "text": "Uncaught TypeError: cannot read 'x'",
                "location": {
                    "url": "http://localhost:5173/app.js",
                    "lineNumber": 42,
                    "columnNumber": 7,
                },
                "stack": (
                    "TypeError: cannot read 'x'\n"
                    "    at render (app.js:42:7)\n"
                    "    at mount (app.js:10:3)"
                ),
            },
        ],
        "network": [
            {
                "method": "GET",
                "url": "http://localhost:5173/api/data",
                "failure": "net::ERR_FAILED",
            },
            {"method": "POST", "url": "http://localhost:5173/api/save", "status": 500},
        ],
        "elements": [],
        "text": "Hello",
        "screenshot_path": ".pmx/screenshots/0001-navigate.png",
    }
    rendered = tool._render_observation(data)
    # header summary preserved
    assert "CONSOLE (1 errors, 0 warnings):" in rendered
    # stack (truncated form) is present
    assert "at render (app.js:42:7)" in rendered
    # source:line from location
    assert "http://localhost:5173/app.js:42:7" in rendered
    # console.log surfaced because an error is present
    assert "boot sequence started" in rendered
    # network failures, both failure-text and status forms
    assert "NETWORK FAIL: GET http://localhost:5173/api/data -> net::ERR_FAILED" in rendered
    assert "NETWORK FAIL: POST http://localhost:5173/api/save -> 500" in rendered


def test_render_observation_hides_logs_when_no_problems():
    tool = BrowserTool()
    data = {
        "url": "http://localhost:5173/",
        "title": "App",
        "console": [
            {"level": "log", "text": "just a healthy log line"},
            {"level": "info", "text": "all good"},
        ],
        "network": [],
        "elements": [],
        "text": "Hello",
        "screenshot_path": None,
    }
    rendered = tool._render_observation(data)
    # No error/warning → no console block, and the noisy logs are dropped.
    assert "CONSOLE" not in rendered
    assert "just a healthy log line" not in rendered


def test_render_observation_stack_truncated_to_cap():
    import disco.tools.builtin.browser as browser_mod

    tool = BrowserTool()
    stack = "Error: deep\n" + "\n".join(f"    at frame{i} (app.js:{i}:1)" for i in range(40))
    data = {
        "url": "http://localhost:5173/",
        "title": "App",
        "console": [{"level": "error", "text": "deep", "stack": stack}],
        "network": [],
        "elements": [],
        "text": "Hello",
        "screenshot_path": None,
    }
    rendered = tool._render_observation(data)
    # Only the first _MAX_STACK_LINES frames are kept, then a truncation marker.
    assert "at frame0 (app.js:0:1)" in rendered
    omitted_frame = (
        f"at frame{browser_mod._MAX_STACK_LINES} "
        f"(app.js:{browser_mod._MAX_STACK_LINES}:1)"
    )
    assert omitted_frame not in rendered
    assert "stack truncated" in rendered


class _FakePage:
    """A Playwright-like page for driving the daemon capture path.

    `body_texts` and `elements_seq` are per-call sequences: each `.evaluate()`
    (text read) and each `_get_elements()` returns the next entry, holding the
    last one once exhausted. This lets a test simulate a late SPA hydration that
    is empty for the first N reads then yields content.
    """

    def __init__(self, body_texts, screenshot_path_target):
        self._body_texts = list(body_texts)
        self._eval_call = 0
        self.url = "http://localhost:5173/"
        self._screenshot_target = screenshot_path_target
        self.waits = []  # records wait_for_timeout calls (retry sleeps)

    def goto(self, url, wait_until=None):
        pass

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def title(self):
        return "App"

    def evaluate(self, script):
        # The capture path calls evaluate() once per attempt for the body text.
        idx = min(self._eval_call, len(self._body_texts) - 1)
        self._eval_call += 1
        return self._body_texts[idx]

    def screenshot(self, path=None, full_page=False):
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n")


def _drive_capture(fake_page, elements_seq, action="navigate", params=None):
    """Run a HtmlHandler._handle_action against a fake page + elements sequence."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    # Point module state at the fake page.
    daemon_mod.state.page = fake_page

    handler = daemon_mod.BrowserHandler.__new__(daemon_mod.BrowserHandler)

    elems = list(elements_seq)
    calls = {"n": 0}

    def _fake_get_elements(page):
        idx = min(calls["n"], len(elems) - 1)
        calls["n"] += 1
        return elems[idx]

    handler._get_elements = _fake_get_elements  # type: ignore[method-assign]

    p = {"action": action, "url": "http://localhost:5173/"}
    if params:
        p.update(params)
    return handler._handle_action(action, p)


def test_capture_retries_until_late_hydration_renders(tmp_path, monkeypatch):
    """B-F: a SPA that is blank for the first 2 reads then mounts must NOT be
    returned as a permanent empty observation — the capture path retries."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "SCREENSHOT_DIR", str(tmp_path / ".pmx/screenshots"))

    fake_page = _FakePage(
        body_texts=["", "", "Hello from React"],  # empty twice, then content
        screenshot_path_target=tmp_path,
    )
    # Elements empty until text appears (3rd read).
    elements_seq = [[], [], ["1[:]<button>Go</button>"]]

    res = _drive_capture(fake_page, elements_seq)

    assert res["ok"] is True
    assert res["text"] == "Hello from React"
    assert res["elements"] == ["1[:]<button>Go</button>"]
    # It retried: two empty reads → two retry sleeps before the content read.
    retry_sleeps = [w for w in fake_page.waits if w == daemon_mod.CAPTURE_RETRY_INTERVAL_MS]
    assert retry_sleeps == [
        daemon_mod.CAPTURE_RETRY_INTERVAL_MS,
        daemon_mod.CAPTURE_RETRY_INTERVAL_MS,
    ]


def test_capture_breaks_on_elements_only(tmp_path, monkeypatch):
    """Non-empty elements (even with empty body text) should end the retry loop."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "SCREENSHOT_DIR", str(tmp_path / ".pmx/screenshots"))

    fake_page = _FakePage(body_texts=[""], screenshot_path_target=tmp_path)
    res = _drive_capture(fake_page, [["1[:]<button>Go</button>"]])

    assert res["ok"] is True
    assert res["text"] == ""
    assert res["elements"] == ["1[:]<button>Go</button>"]
    # Content on the first read → no retry sleeps (only the navigate settle wait).
    assert daemon_mod.CAPTURE_RETRY_INTERVAL_MS not in fake_page.waits


def test_capture_blank_page_returns_empty_after_bounded_cap(tmp_path, monkeypatch):
    """A genuinely-blank page must return empty after the bounded cap — it must
    NOT hang, and must not retry more than CAPTURE_RETRY_MAX times."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "SCREENSHOT_DIR", str(tmp_path / ".pmx/screenshots"))

    fake_page = _FakePage(body_texts=[""], screenshot_path_target=tmp_path)
    res = _drive_capture(fake_page, [[]])  # forever empty

    assert res["ok"] is True
    assert res["text"] == ""
    assert res["elements"] == []
    # Bounded: exactly CAPTURE_RETRY_MAX attempts → CAPTURE_RETRY_MAX-1 retry
    # sleeps (the final attempt does not sleep before falling through).
    retry_sleeps = [w for w in fake_page.waits if w == daemon_mod.CAPTURE_RETRY_INTERVAL_MS]
    assert len(retry_sleeps) == daemon_mod.CAPTURE_RETRY_MAX - 1


def test_daemon_start_applies_anti_fingerprint(monkeypatch):
    """W-46: state.start() must launch with the anti-automation flag + a realistic
    desktop-Chrome context (UA without 'HeadlessChrome', locale/timezone/Accept-Language)
    and inject the stealth init-script that erases the webdriver/plugins/languages tells.
    Asserts the actual launch/new_context kwargs via a fake playwright (no real browser)."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    rec: dict = {}

    class _FakeStartPage:
        def on(self, *a, **k):
            pass

    class _FakeContext:
        def add_init_script(self, script):
            rec["init_script"] = script

        def new_page(self):
            return _FakeStartPage()

    class _FakeBrowser:
        def new_context(self, **kwargs):
            rec["context_kwargs"] = kwargs
            return _FakeContext()

    class _FakeChromium:
        def launch(self, **kwargs):
            rec["launch_kwargs"] = kwargs
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        def stop(self):
            pass

    class _FakePWManager:
        def start(self):
            return _FakePlaywright()

    monkeypatch.setattr(daemon_mod, "sync_playwright", lambda: _FakePWManager())

    state = daemon_mod.BrowserState()
    state.start()

    # launch: headless + the anti-automation blink flag (the navigator.webdriver source).
    assert rec["launch_kwargs"]["headless"] is True
    assert "--disable-blink-features=AutomationControlled" in rec["launch_kwargs"]["args"]

    # context: realistic desktop UA (NO HeadlessChrome) + locale/timezone + Accept-Language.
    ck = rec["context_kwargs"]
    assert ck["user_agent"] == daemon_mod._REALISTIC_UA
    assert "HeadlessChrome" not in ck["user_agent"]
    assert ck["locale"] == "en-US"
    assert ck["timezone_id"]
    assert "Accept-Language" in ck["extra_http_headers"]

    # init-script erases the three cheap JS tells before any page script runs.
    assert "webdriver" in rec["init_script"]
    assert "plugins" in rec["init_script"]
    assert "languages" in rec["init_script"]


def test_daemon_start_headed_keeps_anti_fingerprint(monkeypatch):
    """W-46: the live (headed) restart on :1 must carry the SAME anti-fingerprint
    context + flag, plus the --display arg — the fingerprint is consistent across the
    headless→headed transition."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    rec: dict = {}

    class _FakeStartPage:
        def on(self, *a, **k):
            pass

    class _FakeContext:
        def add_init_script(self, script):
            rec["init_script"] = script

        def new_page(self):
            return _FakeStartPage()

    class _FakeBrowser:
        def new_context(self, **kwargs):
            rec["context_kwargs"] = kwargs
            return _FakeContext()

    class _FakeChromium:
        def launch(self, **kwargs):
            rec["launch_kwargs"] = kwargs
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        def stop(self):
            pass

    class _FakePWManager:
        def start(self):
            return _FakePlaywright()

    monkeypatch.setattr(daemon_mod, "sync_playwright", lambda: _FakePWManager())

    state = daemon_mod.BrowserState()
    state.start(display=":1")

    assert rec["launch_kwargs"]["headless"] is False
    assert "--disable-blink-features=AutomationControlled" in rec["launch_kwargs"]["args"]
    assert "--display=:1" in rec["launch_kwargs"]["args"]
    assert rec["context_kwargs"]["user_agent"] == daemon_mod._REALISTIC_UA
    assert "webdriver" in rec["init_script"]


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


@pytest.mark.asyncio
async def test_browser_unavailable_is_terminal_not_retryable(monkeypatch):
    """ROOT-3 (slides spiral): when the daemon never comes up, run() must return a
    clear, terminal 'skip browser verification' ToolOutcome (success=False +
    structured browser_unavailable flag) — NOT a raw exception or a generic error
    the agent retries in a loop."""
    from disco.tools.builtin.browser import (
        BROWSER_UNAVAILABLE_MSG,
        BrowserArgs,
        BrowserUnavailableError,
    )

    # Don't actually sleep through the 10s daemon poll.
    monkeypatch.setattr("disco.tools.builtin.browser.asyncio.sleep", AsyncMock())

    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sessions = AsyncMock()
    ctx.timeout_s = 30
    # Every health check fails → the daemon never starts.
    ctx.sandbox.exec_shell.return_value = ExecResult(exit_code=1, stdout="", stderr="")

    # _ensure_daemon raises the TYPED terminal error...
    with pytest.raises(BrowserUnavailableError):
        await tool._ensure_daemon(ctx)

    # ...and run() converts it into a terminal, non-retryable outcome (no exception).
    out = await tool.run(BrowserArgs(action="navigate", url="http://127.0.0.1:8000/"), ctx)
    assert out.success is False
    assert out.structured == {"browser_unavailable": True}
    assert out.error == BROWSER_UNAVAILABLE_MSG
    assert "do not retry" in out.error

@pytest.mark.integration
@pytest.mark.asyncio
async def test_browser_daemon_integration_real_chromium(tmp_path):
    # This test needs a real browser and local daemon
    import signal
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import disco.tools.builtin._browser_daemon as daemon_mod
    import httpx

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
        cwd=str(tmp_path),
        start_new_session=True,
    )
    client = None

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
        # BP-00: without the flag, the response must NOT carry inline image bytes.
        assert "screenshot_b64" not in data

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

        # (d) BP-00: include_screenshot_b64 → response carries base64 that decodes
        # byte-for-byte to the PNG the daemon wrote on disk.
        import base64
        res = await client.post(
            "/",
            json={"action": "navigate", "url": fixture_url, "include_screenshot_b64": True},
        )
        data = res.json()
        assert data["ok"]
        assert data.get("screenshot_b64")
        on_disk = (tmp_path / data["screenshot_path"]).read_bytes()
        assert base64.b64decode(data["screenshot_b64"]) == on_disk

    finally:
        if client is not None:
            await client.aclose()
        fixture_server.shutdown()
        fixture_server.server_close()
        fixture_thread.join(timeout=5)
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(daemon_proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            daemon_proc.wait(timeout=10)
