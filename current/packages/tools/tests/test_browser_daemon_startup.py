"""Browser daemon startup/lifecycle tests, split out of test_browser_daemon.py
(PY-0887: the parent module was over the 1200-logical-line test-module cap).

Covers: `BrowserState.start()`'s anti-fingerprint context/launch args, the
renderer-readiness gate, and `BrowserTool._ensure_daemon`'s health-check /
restart / diagnostic behavior. Split by coherent behavior area (daemon
startup), not truncated arbitrarily — every test that ran before still runs,
unchanged.
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.browser import BrowserArgs, BrowserTool
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.shell_sessions import ExecOutcome, SessionView


def test_daemon_start_applies_anti_fingerprint(monkeypatch):
    """W-46: state.start() must launch with the anti-automation flag + a realistic
    desktop-Chrome context (UA without 'HeadlessChrome', locale/timezone/Accept-Language)
    and inject the stealth init-script that erases the webdriver/plugins/languages tells.
    Asserts the actual launch/new_context kwargs via a fake playwright (no real browser)."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    rec: dict = {}

    class _FakeStartPage:
        def set_content(self, _html):
            pass

        def evaluate(self, _script):
            return True

        def close(self):
            pass

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
        def set_content(self, _html):
            pass

        def evaluate(self, _script):
            return True

        def close(self):
            pass

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


def test_h344_daemon_start_refuses_fontless_renderer(monkeypatch):
    """A live Chromium process is not ready evidence if it cannot paint text."""
    import disco.tools.builtin._browser_daemon as daemon_mod

    class _FontlessPage:
        def set_content(self, _html):
            pass

        def evaluate(self, _script):
            return False

        def close(self):
            pass

    class _Context:
        def add_init_script(self, _script):
            pass

        def new_page(self):
            return _FontlessPage()

        def close(self):
            pass

    class _Browser:
        def new_context(self, **_kwargs):
            return _Context()

        def close(self):
            pass

    class _Chromium:
        def launch(self, **_kwargs):
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        def stop(self):
            pass

    class _Manager:
        def start(self):
            return _Playwright()

    monkeypatch.setattr(daemon_mod, "sync_playwright", lambda: _Manager())
    state = daemon_mod.BrowserState()

    with pytest.raises(daemon_mod.BrowserRendererUnavailable, match="system-font text"):
        state.start()

    assert state.render_ready is False
    assert "renderer unavailable" in state.render_error
    assert state.page is None
    state.stop()


@pytest.mark.asyncio
async def test_browser_ensure_daemon_restart_on_failure():
    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sessions = AsyncMock()

    # First health check fails (exit 1), then succeeds (exit 0)
    ctx.sandbox.exec_shell.side_effect = [
        ExecResult(exit_code=1, stdout="", stderr=""),  # health check 1
        ExecResult(exit_code=0, stdout="", stderr=""),  # health check 2 (after start)
    ]

    await tool._ensure_daemon(ctx)

    # Verify it tried to write the daemon and start it
    assert ctx.sandbox.write_file.called
    assert ctx.sessions.exec.called
    assert ctx.sessions.exec.call_args[0] == (
        "__browser",
        "python3 /workspace/.pmx/_browser_daemon.py",
        None,
    )


@pytest.mark.asyncio
async def test_browser_ensure_daemon_recovers_stale_internal_session():
    """An unhealthy daemon may leave its platform-owned tmux pane busy.

    Recovery belongs inside BrowserTool: model-facing shell tools correctly reject
    ``__browser`` and must never be asked to kill or inspect it.
    """
    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sessions = AsyncMock()

    ctx.sandbox.exec_shell.side_effect = [
        ExecResult(exit_code=1, stdout="", stderr=""),
        ExecResult(exit_code=0, stdout="", stderr=""),
    ]

    await tool._ensure_daemon(ctx)

    ctx.sessions.kill_foreground.assert_awaited_once_with("__browser")
    ctx.sessions.exec.assert_awaited_once_with(
        "__browser", "python3 /workspace/.pmx/_browser_daemon.py", None
    )


@pytest.mark.asyncio
async def test_browser_ensure_daemon_keeps_healthy_internal_session():
    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sessions = AsyncMock()
    ctx.sandbox.exec_shell.return_value = ExecResult(exit_code=0, stdout="", stderr="")

    assert await tool._ensure_daemon(ctx) == "http://127.0.0.1:8901"

    ctx.sessions.kill_foreground.assert_not_awaited()
    ctx.sessions.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_unavailable_is_terminal_not_retryable(monkeypatch):
    """ROOT-3 (slides spiral): when the daemon never comes up, run() must return a
    clear, terminal unverified-browser ToolOutcome (success=False + structured
    browser_unavailable flag) — NOT a raw exception, false waiver, or generic error
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
    ctx.sessions.exec.return_value = ExecOutcome(
        running=True, exit_code=None, output="starting browser daemon"
    )
    ctx.sessions.view.return_value = SessionView(
        running=False, output="chromium launch failed: missing runtime"
    )

    # _ensure_daemon raises the TYPED terminal error...
    with pytest.raises(BrowserUnavailableError):
        await tool._ensure_daemon(ctx)

    # ...and run() converts it into a terminal, non-retryable outcome (no exception).
    out = await tool.run(BrowserArgs(action="navigate", url="http://127.0.0.1:8000/"), ctx)
    assert out.success is False
    assert out.structured["browser_unavailable"] is True
    assert out.structured["startup_diagnostic"] == ("chromium launch failed: missing runtime")
    assert out.error == BROWSER_UNAVAILABLE_MSG
    assert "do not retry" in out.error
    assert "Browser rendering remains unverified" in out.error
    assert "report the missing browser proof explicitly" in out.error
    assert "does not require a browser" not in out.error


@pytest.mark.asyncio
async def test_browser_early_daemon_exit_preserves_bounded_diagnostic(monkeypatch):
    from disco.tools.builtin.browser import BROWSER_UNAVAILABLE_MSG

    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sessions = AsyncMock()
    ctx.timeout_s = 30
    ctx.sandbox.exec_shell.return_value = ExecResult(exit_code=1, stdout="", stderr="")
    ctx.sessions.exec.return_value = ExecOutcome(
        running=False,
        exit_code=1,
        output="X" * 1500 + "\nplaywright chromium launch boom",
    )

    out = await tool.run(BrowserArgs(action="navigate", url="http://127.0.0.1:8000/"), ctx)

    assert out.success is False
    assert out.error == BROWSER_UNAVAILABLE_MSG
    diagnostic = out.structured["startup_diagnostic"]
    assert diagnostic.startswith("exit=1; …")
    assert diagnostic.endswith("playwright chromium launch boom")
    assert len(diagnostic) <= 1280
    assert ctx.sessions.view.await_count == 0


@pytest.mark.asyncio
async def test_process_daemon_grants_installed_playwright_package_root(monkeypatch):
    """H070: clean process sessions must still import the shipped browser runtime."""

    import disco.tools.builtin.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_installed_chromium_executable", lambda: None)
    monkeypatch.setattr(
        browser_mod,
        "_installed_playwright_runtime",
        lambda: ("/opt/disco/bin/python3", "/opt/disco/site-packages"),
    )
    tool = BrowserTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.sandbox = AsyncMock()
    ctx.sandbox.shares_host_network = True
    ctx.sandbox.workspace_path = "/tmp/process-workspace"
    ctx.sandbox.exec_shell.return_value = ExecResult(exit_code=1, stdout="", stderr="")
    ctx.sandbox.read_file.side_effect = FileNotFoundError
    ctx.sessions = AsyncMock()
    ctx.sessions.exec.return_value = ExecOutcome(
        running=False,
        exit_code=1,
        output="expected stop",
    )

    with pytest.raises(browser_mod.BrowserUnavailableError):
        await tool._ensure_daemon(ctx)

    command = ctx.sessions.exec.await_args.args[1]
    assert command.startswith("TMPDIR=/var/tmp DISCO_BROWSER_PORT=0")
    assert "PYTHONPATH=/opt/disco/site-packages" in command
    assert "/opt/disco/bin/python3 /workspace/.pmx/_browser_daemon.py" in command


def test_installed_playwright_runtime_imports_under_clean_env():
    """The derived interpreter must match Playwright's compiled dependencies."""

    from disco.tools.builtin.browser import _installed_playwright_runtime

    runtime = _installed_playwright_runtime()
    if runtime is None:
        pytest.skip("installed Playwright runtime is required")
    interpreter, pythonpath = runtime
    result = subprocess.run(
        [interpreter, "-c", "from playwright.sync_api import sync_playwright"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_browser_startup_diagnostic_redacts_secret_shaped_output():
    from disco.tools.builtin.browser import _bounded_startup_diagnostic

    diagnostic = _bounded_startup_diagnostic(
        "API_KEY=do-not-retain Authorization: Bearer also-secret\nlaunch failed",
        1,
    )

    assert diagnostic == "exit=1; <redacted> <redacted>\nlaunch failed"
    assert "do-not-retain" not in diagnostic
    assert "also-secret" not in diagnostic
