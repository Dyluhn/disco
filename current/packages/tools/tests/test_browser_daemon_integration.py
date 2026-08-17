"""Browser daemon integration tests, split out of test_browser_daemon.py
(PY-0887: the parent module was over the 1200-logical-line test-module cap).

Every test here is `@pytest.mark.integration` (real Chromium / a real process
sandbox) — split out as its own coherent behavior area ("prove it end to
end"), unchanged from the parent module.
"""

import asyncio
import os
import pathlib

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.browser import BrowserArgs, BrowserTool
from disco.tools.sandbox.process import ProcessSandboxService
from disco.tools.sandbox.session import SandboxSession
from disco.tools.sandbox.shell_sessions import ShellSessionManager


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
    from disco.tools.builtin.browser import _installed_chromium_executable
    from playwright.sync_api import sync_playwright

    executable = _installed_chromium_executable()
    if executable is None:
        pytest.skip("installed Chromium is required")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable, args=["--no-sandbox"]
        )
        try:
            calibration_page = browser.new_page()
            renderer_ready = daemon_mod._text_renderer_ready(calibration_page)
        finally:
            browser.close()
    if not renderer_ready:
        pytest.skip("INFRA: installed Chromium cannot lay out ordinary system-font text")

    # 1. Setup a fixture page: a counter button, a cookie-setter, and a console error.
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    fixture_file = fixture_dir / "index.html"
    fixture_file.write_text("""
        <html>
        <body>
            <h1 id="counter">0</h1>
            <h2 style="position:absolute;left:-9999px">offscreen ghost</h2>
            <canvas width="100" height="100"></canvas>
            <img src="/missing.png" width="100" height="100">
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

    fixture_server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
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
            "PMX_WORKSPACE": str(tmp_path),
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
        # H335: only the rendered in-viewport heading counts. The offscreen
        # heading, empty canvas, and broken image cannot fabricate content proof.
        assert data["visible_semantic_elements"] == 1
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
        assert "1" in data["text"]  # counter incremented

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_browser_uses_workspace_owned_ephemeral_port():
    """Real BrowserTool path: tmux env, dynamic daemon port, and screenshot jail."""
    import socket
    import uuid

    conversation_id = "brws" + uuid.uuid4().hex
    service = ProcessSandboxService()
    inst = await service.create(
        spec=None,
        owner_id="browser-integration",
        conversation_id=conversation_id,
    )

    async def get_inst():
        return inst

    sessions = ShellSessionManager(get_inst, namespace=f"{conversation_id[:8]}-")
    try:
        await inst.write_file(
            "/workspace/index.html",
            b"<html><body><h1>workspace-owned browser</h1></body></html>",
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            fixture_port = int(probe.getsockname()[1])
        started = await sessions.exec(
            "fixture",
            f"python3 -m http.server {fixture_port}",
            None,
        )
        assert started.running
        for _ in range(20):
            health = await inst.exec_shell(
                f"curl -sf http://127.0.0.1:{fixture_port}/", timeout_s=2
            )
            if health.exit_code == 0:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("fixture server did not become healthy")

        ctx = ToolContext(
            sandbox=inst,
            sessions=sessions,
            workspace_path=inst.workspace_path or "",
            timeout_s=30,
            capabilities={
                Capability.NETWORK,
                Capability.DISPLAY,
                Capability.FILESYSTEM,
                Capability.SHELL,
            },
            owner_id="browser-integration",
            conversation_id=conversation_id,
        )
        outcome = await BrowserTool().run(
            BrowserArgs(
                action="navigate",
                url=f"http://127.0.0.1:{fixture_port}/",
            ),
            ctx,
        )
        if not outcome.success:
            daemon_view = await sessions.view("__browser")
            pytest.fail(f"{outcome.error}\n--- browser daemon pane ---\n{daemon_view.output}")
        assert outcome.structured is not None
        assert "workspace-owned browser" in str(outcome.structured.get("text"))

        daemon_port = int((await inst.read_file("/workspace/.pmx/browser-port")).decode().strip())
        assert 1 <= daemon_port <= 65535
        assert daemon_port != 8901
        screenshot = str(outcome.structured["screenshot_path"])
        assert inst.workspace_path is not None
        assert (pathlib.Path(inst.workspace_path) / screenshot).is_file()
    finally:
        await inst.destroy()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_browser_uses_runtime_through_production_session_wrapper():
    """The production wrapper must preserve process-backend runtime selection."""
    import socket
    import uuid

    conversation_id = "brws" + uuid.uuid4().hex
    session = SandboxSession(
        ProcessSandboxService(),
        owner_id="browser-integration",
        conversation_id=conversation_id,
    )
    try:
        await session.write_file(
            "/workspace/index.html",
            b"<html><body><h1>wrapped browser runtime</h1></body></html>",
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            fixture_port = int(probe.getsockname()[1])
        started = await session.sessions.exec(
            "fixture",
            f"python3 -m http.server {fixture_port}",
            None,
        )
        assert started.running
        for _ in range(20):
            health = await session.exec_shell(
                f"curl -sf http://127.0.0.1:{fixture_port}/", timeout_s=2
            )
            if health.exit_code == 0:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("fixture server did not become healthy")

        ctx = ToolContext(
            sandbox=session,
            sessions=session.sessions,
            workspace_path=session.workspace_path or "",
            timeout_s=30,
            capabilities={
                Capability.NETWORK,
                Capability.DISPLAY,
                Capability.FILESYSTEM,
                Capability.SHELL,
            },
            owner_id="browser-integration",
            conversation_id=conversation_id,
        )
        outcome = await BrowserTool().run(
            BrowserArgs(
                action="navigate",
                url=f"http://127.0.0.1:{fixture_port}/",
            ),
            ctx,
        )
        if not outcome.success:
            daemon_view = await session.sessions.view("__browser")
            pytest.fail(f"{outcome.error}\n--- browser daemon pane ---\n{daemon_view.output}")
        assert outcome.structured is not None
        assert "wrapped browser runtime" in str(outcome.structured.get("text"))
        assert session.shares_host_network is True
    finally:
        await session.destroy()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_browser_recovers_stale_busy_daemon_session():
    """Public BrowserTool boundary recovers after losing a live daemon's port."""
    import socket
    import uuid

    conversation_id = "brws" + uuid.uuid4().hex
    session = SandboxSession(
        ProcessSandboxService(),
        owner_id="browser-integration",
        conversation_id=conversation_id,
    )
    try:
        await session.write_file(
            "/workspace/index.html",
            b"<html><body><h1>stale daemon recovered</h1></body></html>",
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            fixture_port = int(probe.getsockname()[1])
        started = await session.sessions.exec(
            "fixture",
            f"python3 -m http.server {fixture_port}",
            None,
        )
        assert started.running
        for _ in range(20):
            health = await session.exec_shell(
                f"curl -sf http://127.0.0.1:{fixture_port}/", timeout_s=2
            )
            if health.exit_code == 0:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("fixture server did not become healthy")

        ctx = ToolContext(
            sandbox=session,
            sessions=session.sessions,
            workspace_path=session.workspace_path or "",
            timeout_s=30,
            capabilities={
                Capability.NETWORK,
                Capability.DISPLAY,
                Capability.FILESYSTEM,
                Capability.SHELL,
            },
            owner_id="browser-integration",
            conversation_id=conversation_id,
        )
        first = await BrowserTool().run(
            BrowserArgs(action="navigate", url=f"http://127.0.0.1:{fixture_port}/"),
            ctx,
        )
        assert first.success, first.error
        assert (await session.sessions.view("__browser")).running

        with socket.socket() as stale_probe:
            stale_probe.bind(("127.0.0.1", 0))
            stale_port = int(stale_probe.getsockname()[1])
        await session.write_file("/workspace/.pmx/browser-port", str(stale_port).encode("ascii"))

        recovered = await BrowserTool().run(
            BrowserArgs(action="navigate", url=f"http://127.0.0.1:{fixture_port}/"),
            ctx,
        )
        assert recovered.success, recovered.error
        assert "stale daemon recovered" in str((recovered.structured or {}).get("text"))
        replacement_port = int(
            (await session.read_file("/workspace/.pmx/browser-port")).decode().strip()
        )
        assert replacement_port != stale_port
    finally:
        await session.destroy()


@pytest.mark.integration
def test_gradient_text_heading_is_visible_but_cloaked_text_is_not():
    """`background-clip: text` with a transparent fill is how a gradient heading
    is painted — the glyphs ARE visible.

    Treating the transparent fill as invisible dropped the heading from the
    extracted text, so a page that plainly showed the required copy failed its
    `must_contain` (counted seed 440034: `<h1>Node Seed 440034</h1>` styled with a
    linear-gradient). Any model choosing that very common heading treatment
    failed regardless of how correct its build was.

    The exemption is exactly the gradient idiom: a transparent fill WITHOUT a
    clipped background is still cloaked copy and must stay excluded.
    """
    import base64
    import importlib.resources

    from disco.tools.builtin._browser_daemon import _visible_dom_text
    from disco.tools.builtin.browser import _installed_chromium_executable
    from playwright.sync_api import sync_playwright

    executable = _installed_chromium_executable()
    if executable is None:
        pytest.skip("installed Chromium is required")
    # This host's headless Chromium cannot rasterize text without an embedded
    # font, so glyphs get zero client rects and NOTHING extracts. Same
    # scaffolding as the sibling extractor test.
    font = (
        importlib.resources.files("disco.core.brand")
        .joinpath("fonts/SchibstedGrotesk.ttf")
        .read_bytes()
    )
    font_b64 = base64.b64encode(font).decode("ascii")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(
                """
                <style>*{font-family:"DiscoProbe"!important}</style>
                <h1 style="background:linear-gradient(135deg,#C65D3B,#E5A93C);
                           -webkit-background-clip:text;background-clip:text;
                           -webkit-text-fill-color:transparent">Node Seed 440034</h1>
                <h2 style="background:linear-gradient(135deg,#C65D3B,#E5A93C);
                           -webkit-background-clip:text;background-clip:text;
                           color:transparent">Gradient Via Color 440034</h2>
                <p style="-webkit-text-fill-color:transparent">Cloaked fill</p>
                <p style="color:transparent">Cloaked color</p>
                <p>Running on Node.js</p>
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
            text = _visible_dom_text(page)

            # the gradient idiom, in both spellings, is visible text
            assert "Node Seed 440034" in text
            assert "Gradient Via Color 440034" in text
            assert "Running on Node.js" in text
            # ...and cloaking without a clipped background is still excluded
            assert "Cloaked fill" not in text
            assert "Cloaked color" not in text
        finally:
            browser.close()
