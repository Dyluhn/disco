import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
from disco.tools.builtin._browser_daemon import (
    _count_visible_semantic_elements,
    _visible_dom_text,
)
from disco.tools.builtin.browser import BrowserTool


@pytest.mark.asyncio
async def test_browser_render_observation():
    tool = BrowserTool()
    data = {
        "ok": True,
        "url": "http://example.com",
        "title": "Example Domain",
        "console": [
            {"level": "error", "text": "Uncaught TypeError"},
            {"level": "warning", "text": "Deprecation warning"},
        ],
        "elements": ["1[:]<button>Click me</button>", "2[:]<a>Link</a>"],
        "text": "Hello world",
        "screenshot_path": ".pmx/screenshots/0001-navigate.png",
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
    def __init__(self, method, url, failure=None, *, resource_type=None, frame=None):
        self.method = method
        self.url = url
        self.failure = failure
        self.resource_type = resource_type
        self.frame = frame


class _FakeResponse:
    def __init__(self, request, *, status=200, headers=None):
        self.request = request
        self.status = status
        self.url = request.url
        self.headers = headers or {}


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
    assert state.lane("agent").document_content_type == ""


def test_daemon_captures_only_main_document_response_content_type():
    state = _fresh_state()
    main_frame = object()
    subframe = object()
    state.page = MagicMock(main_frame=main_frame)

    state._add_response(
        _FakeResponse(
            _FakeRequest(
                "GET",
                "http://localhost:8000/",
                resource_type="document",
                frame=main_frame,
            ),
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    )
    assert state.lane("agent").document_content_type == "text/plain; charset=utf-8"

    state._add_response(
        _FakeResponse(
            _FakeRequest(
                "GET",
                "http://localhost:8000/frame",
                resource_type="document",
                frame=subframe,
            ),
            headers={"content-type": "text/html"},
        )
    )
    state._add_response(
        _FakeResponse(
            _FakeRequest(
                "GET",
                "http://localhost:8000/app.js",
                resource_type="script",
                frame=main_frame,
            ),
            headers={"content-type": "text/javascript"},
        )
    )
    assert state.lane("agent").document_content_type == "text/plain; charset=utf-8"


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
        f"at frame{browser_mod._MAX_STACK_LINES} (app.js:{browser_mod._MAX_STACK_LINES}:1)"
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

    def __init__(self, body_texts, screenshot_path_target, document_content_type="text/html"):
        self._body_texts = list(body_texts)
        self._eval_call = 0
        self.url = "http://localhost:5173/"
        self._screenshot_target = screenshot_path_target
        self.document_content_type = document_content_type
        self.waits = []  # records wait_for_timeout calls (retry sleeps)

    def goto(self, url, wait_until=None):
        # Simulate the main-document Response event that Playwright emits during
        # navigation; production captures this before the observation is built.
        import disco.tools.builtin._browser_daemon as daemon_mod

        daemon_mod.state.lane("agent").document_content_type = self.document_content_type

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
    daemon_mod.state.render_ready = True
    daemon_mod.state.render_error = ""

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


def test_fill_accepts_css_selector_without_a_transient_element_index():
    """A DOM-changing interaction must not force a stale numeric fill target."""
    from types import SimpleNamespace

    import disco.tools.builtin._browser_daemon as daemon_mod
    from disco.tools.builtin._browser_daemon_parts.dispatch import _handle_fill

    handler = MagicMock()
    handler._freshness.return_value = {"schema_version": 1}
    handler._selector_for.side_effect = daemon_mod.BrowserHandler._selector_for
    locator = MagicMock()
    handler._action_locator.return_value = (locator, None)
    ctx = SimpleNamespace(
        handler=handler,
        page=MagicMock(),
        params={"action": "fill", "selector": "#name", "text": "Jane Doe"},
        lane_name="agent",
        generation="g" * 32,
        nonce="n" * 32,
        requested_epoch=3,
        sync_performed=False,
    )

    assert _handle_fill(ctx, MagicMock()) is None
    handler._action_locator.assert_called_once_with(ctx.page, "#name", {"schema_version": 1})
    locator.fill.assert_called_once_with("Jane Doe")


def _fill_context(handler, params):
    from types import SimpleNamespace

    return SimpleNamespace(
        handler=handler,
        page=MagicMock(),
        params=params,
        lane_name="agent",
        generation="g" * 32,
        nonce="n" * 32,
        requested_epoch=3,
        sync_performed=False,
    )


def _batch_fill_handler():
    import disco.tools.builtin._browser_daemon as daemon_mod

    handler = MagicMock()
    handler._freshness.return_value = {"schema_version": 1}
    handler._selector_for.side_effect = daemon_mod.BrowserHandler._selector_for
    handler._error.side_effect = daemon_mod.BrowserHandler._error
    return handler


def test_prod2_one_fill_call_fills_every_field_of_a_form():
    """PROD-2. Self-testing a signup form one input per model turn (address, city,
    ZIP, card, expiry, CVC) is six round trips to the model for a five-minute
    site. One call carries the whole form; nothing is submitted."""
    from disco.tools.builtin._browser_daemon_parts.dispatch import _handle_fill

    handler = _batch_fill_handler()
    locators = [MagicMock() for _ in range(3)]
    handler._action_locator.side_effect = [(locator, None) for locator in locators]
    ctx = _fill_context(
        handler,
        {
            "action": "fill",
            "fields": [
                {"selector": "#city", "text": "Oakland"},
                {"index": 7, "text": "94607"},
                {"selector": "#cvc", "text": "123"},
            ],
        },
    )

    assert _handle_fill(ctx, MagicMock()) is None
    assert [call.args[1] for call in handler._action_locator.call_args_list] == [
        "#city",
        "[data-pmx-index='7']",
        "#cvc",
    ]
    assert [locator.fill.call_args.args[0] for locator in locators] == [
        "Oakland",
        "94607",
        "123",
    ]
    # A batched fill never submits; submit stays a separate, analyzer-scored call.
    for locator in locators:
        assert not locator.click.called
        assert not locator.press.called


def test_prod2_batched_fill_failure_names_the_field_that_failed():
    """A bare 'browser fill could not be completed' for a six-field call makes the
    model re-derive the whole form. The error names the field and what already
    landed."""
    from disco.tools.builtin._browser_daemon_parts.dispatch import _handle_fill

    handler = _batch_fill_handler()
    good = MagicMock()
    handler._action_locator.side_effect = [
        (good, None),
        (
            None,
            {
                "ok": False,
                "error": "browser action target was not found",
                "error_class": "browser_action_failed",
                "error_reason": "selector_not_found",
            },
        ),
    ]
    ctx = _fill_context(
        handler,
        {
            "action": "fill",
            "fields": [
                {"selector": "#city", "text": "Oakland"},
                {"selector": "#zip", "text": "94607"},
            ],
        },
    )

    result = _handle_fill(ctx, MagicMock())

    assert result["error_reason"] == "selector_not_found"
    assert "field 2 of 2 ('#zip')" in result["error"]
    assert "already filled: field 1 of 2 ('#city')" in result["error"]
    good.fill.assert_called_once_with("Oakland")


def test_prod2_batched_fill_rejects_a_field_that_is_not_fillable():
    from disco.tools.builtin._browser_daemon_parts.dispatch import _handle_fill

    handler = _batch_fill_handler()
    locator = MagicMock()
    locator.evaluate.return_value = False
    handler._action_locator.return_value = (locator, None)
    ctx = _fill_context(
        handler,
        {"action": "fill", "fields": [{"selector": "#hero", "text": "Oakland"}]},
    )

    result = _handle_fill(ctx, MagicMock())

    assert result["error_reason"] == "interaction_blocked"
    assert "not a fillable input" in result["error"]
    assert "field 1 of 1 ('#hero')" in result["error"]
    assert not locator.fill.called


def _click_context(handler, page):
    from types import SimpleNamespace

    return SimpleNamespace(
        handler=handler,
        page=page,
        params={"action": "click", "selector": "#go"},
        lane_name="agent",
        generation="g" * 32,
        nonce="n" * 32,
        requested_epoch=3,
        sync_performed=False,
        click_timeout_ms=8000,
    )


def test_click_exception_remains_blocked():
    from disco.tools.builtin._browser_daemon_parts.dispatch import _handle_click

    handler = MagicMock()
    handler._freshness.return_value = {"schema_version": 1}
    handler._error.return_value = {
        "ok": False,
        "error_class": "browser_action_failed",
        "error_reason": "interaction_blocked",
    }
    locator = MagicMock()
    locator.click.side_effect = RuntimeError("intercepted")
    handler._action_locator.return_value = (locator, None)
    page = MagicMock()

    result = _handle_click(_click_context(handler, page), MagicMock())

    assert result["error_reason"] == "interaction_blocked"


def test_post_click_settle_failure_does_not_reclassify_dispatched_click():
    from disco.tools.builtin._browser_daemon_parts.dispatch import _handle_click

    handler = MagicMock()
    handler._freshness.return_value = {"schema_version": 1}
    locator = MagicMock()
    handler._action_locator.return_value = (locator, None)
    page = MagicMock()
    page.url = "http://example.test/"
    page.wait_for_timeout.side_effect = RuntimeError("settle transport raced")

    assert _handle_click(_click_context(handler, page), MagicMock()) is None
    locator.click.assert_called_once_with(timeout=8000)
    handler._error.assert_not_called()


@pytest.mark.integration
def test_fill_css_selector_reaches_real_selected_renderer(tmp_path, monkeypatch):
    import json

    import disco.tools.builtin._browser_daemon as daemon_mod
    from disco.tools.builtin.browser import _installed_browser_executables

    executables = _installed_browser_executables()
    assert executables, "a real installed browser renderer is required"
    monkeypatch.setenv("DISCO_BROWSER_EXECUTABLES", json.dumps(executables))
    browser_state = daemon_mod.BrowserState()
    monkeypatch.setattr(daemon_mod, "state", browser_state)
    monkeypatch.setattr(daemon_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "SCREENSHOT_DIR", str(tmp_path / ".pmx/screenshots"))
    monkeypatch.setattr(daemon_mod, "_page_kind", lambda _url: "external")

    try:
        browser_state.start()
        page = browser_state.page
        assert page is not None
        page.set_content("<label>Name<input id='name'></label>")
        handler = daemon_mod.BrowserHandler.__new__(daemon_mod.BrowserHandler)
        result = handler._handle_action(
            "fill",
            {"action": "fill", "selector": "#name", "text": "Jane Doe"},
        )

        assert result["ok"] is True, result
        assert page.locator("#name").input_value() == "Jane Doe"
    finally:
        browser_state.stop()


@pytest.mark.integration
def test_click_self_removing_target_returns_transition_capture(tmp_path, monkeypatch):
    import json

    import disco.tools.builtin._browser_daemon as daemon_mod
    from disco.tools.builtin.browser import _installed_browser_executables

    executables = _installed_browser_executables()
    assert executables, "a real installed browser renderer is required"
    monkeypatch.setenv("DISCO_BROWSER_EXECUTABLES", json.dumps(executables))
    browser_state = daemon_mod.BrowserState()
    monkeypatch.setattr(daemon_mod, "state", browser_state)
    monkeypatch.setattr(daemon_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "SCREENSHOT_DIR", str(tmp_path / ".pmx/screenshots"))
    monkeypatch.setattr(daemon_mod, "_page_kind", lambda _url: "external")

    try:
        browser_state.start()
        page = browser_state.page
        assert page is not None
        page.set_content(
            "<button id='go' onclick=\"this.remove(); "
            "document.body.textContent='DONE'\">GO</button>"
        )
        handler = daemon_mod.BrowserHandler.__new__(daemon_mod.BrowserHandler)
        result = handler._handle_action("click", {"action": "click", "selector": "#go"})

        assert result["ok"] is True, result
        assert result["text"] == "DONE"
    finally:
        browser_state.stop()


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


def test_capture_reports_browser_owned_document_content_type(tmp_path, monkeypatch):
    import disco.tools.builtin._browser_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "SCREENSHOT_DIR", str(tmp_path / ".pmx/screenshots"))

    fake_page = _FakePage(
        body_texts=["Node Paused 400913"],
        screenshot_path_target=tmp_path,
        document_content_type="text/plain",
    )
    res = _drive_capture(fake_page, [[]])

    assert res["text"] == "Node Paused 400913"
    assert res["document_content_type"] == "text/plain"


@pytest.mark.integration
def test_main_document_content_type_real_chromium_rejects_page_spoof():
    """The meaningful-content MIME basis comes from the Response, not page JS."""
    import disco.tools.builtin._browser_daemon as daemon_mod
    from disco.core.loop.finish import _browser_content_meaningful
    from disco.tools.builtin.browser import _installed_chromium_executable
    from playwright.sync_api import sync_playwright

    executable = _installed_chromium_executable()
    if executable is None:
        pytest.skip("installed Chromium is required")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler contract
            if self.path == "/plain":
                body = b"Node Paused 400913"
                content_type = "text/plain; charset=utf-8"
            else:
                body = (
                    b"<script>Object.defineProperty(document,'contentType',"
                    b"{value:'text/plain'})</script><p>Loading</p>"
                )
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            context = browser.new_context()
            page = context.new_page()
            browser_state = daemon_mod.BrowserState()
            browser_state._bind_page("agent", page)  # noqa: SLF001 - response boundary under test
            origin = f"http://127.0.0.1:{server.server_port}"

            browser_state.lane("agent").clear_diagnostics()
            page.goto(origin + "/plain", wait_until="load")
            plain = {
                "title": page.title(),
                "text": page.evaluate(
                    "() => (document.body.innerText || document.body.textContent || '').trim()"
                ),
                "elements": [],
                "document_content_type": browser_state.lane("agent").document_content_type,
            }
            assert plain["document_content_type"] == "text/plain; charset=utf-8"
            assert _browser_content_meaningful(plain)

            browser_state.lane("agent").clear_diagnostics()
            page.goto(origin + "/spoof", wait_until="load")
            spoofed = {
                "title": page.title(),
                "text": page.evaluate(
                    "() => (document.body.innerText || document.body.textContent || '').trim()"
                ),
                "elements": [],
                "document_content_type": browser_state.lane("agent").document_content_type,
            }
            assert page.evaluate("document.contentType") == "text/plain"
            assert spoofed["document_content_type"] == "text/html; charset=utf-8"
            assert not _browser_content_meaningful(spoofed)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


def test_visible_semantic_elements_are_strict_rendered_evidence():
    """H335: the daemon emits proof for short, non-interactive rendered content.

    The Python boundary rejects malformed/boolean page values fail-closed.
    """

    page = MagicMock()
    page.evaluate.return_value = 1
    assert _count_visible_semantic_elements(page) == 1
    script = page.evaluate.call_args.args[0]
    assert "getBoundingClientRect" in script
    assert "getComputedStyle" in script
    assert "checkVisibility" in script and "checkOpacity: true" in script
    assert "'h1, h2, h3, h4, h5, h6'" in script
    assert "createTreeWalker" in script and "getClientRects" in script
    assert "visibleRatio < 0.25" in script
    assert "elementFromPoint" in script
    assert "canvas" not in script and "img" not in script
    assert "textContent" in script

    for invalid in (True, -1, "1", None):
        page.evaluate.return_value = invalid
        assert _count_visible_semantic_elements(page) == 0

    page.evaluate.side_effect = RuntimeError("page closed")
    assert _count_visible_semantic_elements(page) == 0


def test_visible_dom_text_is_exact_bounded_structured_evidence():

    page = MagicMock()
    page.evaluate.return_value = "Imported Complete 405115"
    assert _visible_dom_text(page) == "Imported Complete 405115"
    script = page.evaluate.call_args.args[0]
    assert "createTreeWalker" in script
    assert "getClientRects" in script
    assert "getComputedStyle" in script
    assert "aria-hidden" in script
    assert "text-transform" not in script
    assert "slice(0, 4000)" in script

    for invalid in (True, 1, [], None):
        page.evaluate.return_value = invalid
        assert _visible_dom_text(page) == ""

    page.evaluate.side_effect = RuntimeError("page closed")
    assert _visible_dom_text(page) == ""


@pytest.mark.integration
def test_visible_dom_text_real_chromium_preserves_authored_case_and_rejects_hidden():
    import base64
    import importlib.resources

    from disco.tools.builtin.browser import _installed_chromium_executable
    from playwright.sync_api import sync_playwright

    executable = _installed_chromium_executable()
    if executable is None:
        pytest.skip("installed Chromium is required")
    font = (
        importlib.resources.files("disco.core.brand")
        .joinpath("fonts/SchibstedGrotesk.ttf")
        .read_bytes()
    )
    font_b64 = base64.b64encode(font).decode("ascii")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=executable,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(
                """
                <style>*{font-family:"DiscoProbe"!important}</style>
                <h2 style="text-transform:uppercase">Imported Complete 405115</h2>
                <p hidden>Hidden attribute</p>
                <p style="display:none">Display hidden</p>
                <p aria-hidden="true">Accessibility hidden</p>
                <p style="color:transparent">Transparent hidden</p>
                """
            )
            page.evaluate(
                """
                async b64 => {
                    const bytes = atob(b64);
                    const data = new Uint8Array(bytes.length);
                    for (let index = 0; index < bytes.length; index++) {
                        data[index] = bytes.charCodeAt(index);
                    }
                    const font = new FontFace('DiscoProbe', data.buffer);
                    await font.load();
                    document.fonts.add(font);
                    await document.fonts.ready;
                }
                """,
                font_b64,
            )
            assert page.locator("h2").inner_text() == "IMPORTED COMPLETE 405115"
            assert _visible_dom_text(page) == "Imported Complete 405115"
        finally:
            browser.close()


@pytest.mark.integration
def test_visible_semantic_elements_real_chromium():
    """H335 executable DOM proof: visible heading only; false-pass shapes stay zero."""
    from disco.tools.builtin.browser import _installed_chromium_executable
    from playwright.sync_api import sync_playwright

    executable = _installed_chromium_executable()
    if executable is None:
        pytest.skip("installed Chromium is required")
    import base64
    import importlib.resources

    font = (
        importlib.resources.files("disco.core.brand")
        .joinpath("fonts/SchibstedGrotesk.ttf")
        .read_bytes()
    )
    font_b64 = base64.b64encode(font).decode("ascii")
    font_css = '<style>*{font-family:"DiscoProbe"!important}</style>'
    load_font = """
        async b64 => {
            const bytes = atob(b64);
            const data = new Uint8Array(bytes.length);
            for (let index = 0; index < bytes.length; index++) {
                data[index] = bytes.charCodeAt(index);
            }
            const font = new FontFace('DiscoProbe', data.buffer, {weight: '100 900'});
            await font.load();
            document.fonts.add(font);
        }
    """

    cases = {
        "<h1>Live Server Up</h1>": 1,
        "<p>Loading...</p>": 0,
        '<canvas width="100" height="100"></canvas>': 0,
        '<img src="/missing.png" width="100" height="100">': 0,
        '<h1 style="position:absolute;left:-9999px">Ghost</h1>': 0,
        '<div style="width:1px;height:1px;overflow:hidden"><h1>Ghost</h1></div>': 0,
        '<h1 style="clip-path:inset(100%)">Ghost</h1>': 0,
        '<h1 style="color:transparent">Ghost</h1>': 0,
        '<h1><span style="opacity:0">Ghost</span></h1>': 0,
        '<h1><span style="visibility:hidden">Ghost</span></h1>': 0,
        (
            '<h1><span style="display:block;width:1px;height:1px;overflow:hidden">Ghost</span></h1>'
        ): 0,
        '<h1><span style="clip-path:inset(100%)">Ghost</span></h1>': 0,
        (
            '<h1 style="position:relative">Ghost<span style="position:absolute;'
            'inset:0;background:white"></span></h1>'
        ): 0,
        (
            '<h1>Covered</h1><div style="position:fixed;inset:0;'
            'background:white;z-index:9999"></div>'
        ): 0,
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=executable,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            for html, expected in cases.items():
                page.set_content(font_css + html)
                page.evaluate(load_font, font_b64)
                page.evaluate("() => document.fonts.ready")
                if "<h1>Live Server Up</h1>" in html:
                    page.wait_for_function(
                        "() => document.querySelector('h1').getBoundingClientRect().height > 0",
                        timeout=2_000,
                    )
                assert _count_visible_semantic_elements(page) == expected, html
        finally:
            browser.close()
