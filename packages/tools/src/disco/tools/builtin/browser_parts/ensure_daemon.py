"""``BrowserTool._ensure_daemon`` split out into named startup phases.

The original was mccabe-18 across health-check, shipping the daemon source,
building its launch command, starting the session, and polling for health.
Each phase is its own function here, and the (small, unmonkeypatched-by-name)
constants/callables it needs are threaded through explicitly from
``browser.py``'s thin delegate rather than imported back — see
``browser_parts/__init__.py`` for why.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import shlex
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from .daemon_transport import daemon_healthy, process_daemon_url

if TYPE_CHECKING:
    from pathlib import Path

    from ...anatomy import ToolContext
    from ..browser import BrowserUnavailableError

# Register #7 — the daemon is shipped as a standalone script, so the interior
# parts package `_browser_daemon.py` imports must travel with it. The daemon
# names this same package (PEP 366) when it runs without a parent package; the
# two literals are proven to agree by executing the shipped file set in
# `test_shipped_standalone_artifacts.py`, not by comparing them.
_SHIPPED_PACKAGE = "_browser_daemon_shipped"
_PARTS_DIRNAME = "_browser_daemon_parts"
_SHIPPED_PACKAGE_INIT = (
    b'"""Shipped container for the browser daemon\'s interior parts.\n\n'
    b"Written by `browser_parts.ensure_daemon._ship_daemon_files`. The daemon\n"
    b"executes as a script, so it adopts this package name to keep its\n"
    b"package-relative imports resolvable (register #7).\n"
    b'"""\n'
)


async def _current_url_if_healthy(
    ctx: ToolContext, daemon_url_default: str, daemon_port_path: str
) -> str | None:
    process_url = await process_daemon_url(ctx, daemon_port_path=daemon_port_path)
    daemon_url = process_url or daemon_url_default
    if await daemon_healthy(ctx, daemon_url):
        return daemon_url
    return None


async def _ship_daemon_files(
    ctx: ToolContext,
    daemon_src_path: Path,
    live_view_src_path: Path,
    daemon_target_path: str,
) -> None:
    # `BrowserTool.run` proves the sandbox before dispatching; the extraction
    # moved this code out of that narrowed scope, so restate the parent's own
    # invariant (it carried this same assert at browser.py:373/735/795/808/821).
    assert ctx.sandbox is not None
    daemon_src = daemon_src_path.read_text()
    await ctx.sandbox.write_file(daemon_target_path, daemon_src.encode("utf-8"))

    # Register #7: ship the daemon's interior parts package alongside it. Before
    # this, only the two files below were shipped, so the daemon's own
    # `_browser_daemon_parts` imports could not resolve in the sandbox and it
    # died at import. The parts are copied VERBATIM — the shipped bytes of each
    # module are the repo's bytes, so the sandbox runs the code the gates saw.
    shipped_root = posixpath.join(posixpath.dirname(daemon_target_path), _SHIPPED_PACKAGE)
    await ctx.sandbox.write_file(
        posixpath.join(shipped_root, "__init__.py"), _SHIPPED_PACKAGE_INIT
    )
    for part in sorted((daemon_src_path.parent / _PARTS_DIRNAME).glob("*.py")):
        await ctx.sandbox.write_file(
            posixpath.join(shipped_root, _PARTS_DIRNAME, part.name), part.read_bytes()
        )

    # Ship live_view.py alongside the daemon so the daemon can import _live_view.
    if live_view_src_path.exists():
        live_view_src = live_view_src_path.read_text()
        await ctx.sandbox.write_file(
            "/workspace/.pmx/_live_view.py", live_view_src.encode("utf-8")
        )


async def _build_launch_command(
    ctx: ToolContext,
    *,
    daemon_target_path: str,
    daemon_port_path: str,
    browser_executables: Callable[[], dict[str, str]],
    playwright_runtime: Callable[[], tuple[str, str] | None],
) -> str:
    # `BrowserTool.run` proves the sandbox before dispatching; the extraction
    # moved this code out of that narrowed scope, so restate the parent's own
    # invariant (it carried this same assert at browser.py:373/735/795/808/821).
    assert ctx.sandbox is not None
    process_backend = getattr(ctx.sandbox, "shares_host_network", False) is True
    if not process_backend:
        return f"python3 {daemon_target_path}"

    # A stale port file from an unclean daemon exit is not authority. The
    # fresh daemon binds port 0 and atomically publishes the port it owns.
    await ctx.sandbox.exec_shell(f"rm -f {daemon_port_path}", timeout_s=5)

    # HARN-1b/B2: ship a path for EVERY installed engine, not just Chromium's.
    # The daemon picks by measured renderer readiness, and a candidate it cannot
    # launch is not a candidate — with only Chromium's path shipped, a Chromium
    # that cannot paint text left it nothing to fall back to.
    executables = await asyncio.to_thread(browser_executables)
    runtime = await asyncio.to_thread(playwright_runtime)
    executable_env = (
        f" DISCO_BROWSER_EXECUTABLES={shlex.quote(json.dumps(executables, sort_keys=True))}"
        if executables
        else ""
    )
    daemon_python, playwright_pythonpath = runtime or (sys.executable, "")
    pythonpath_env = (
        f" PYTHONPATH={shlex.quote(playwright_pythonpath)}" if playwright_pythonpath else ""
    )
    # Chromium creates its ephemeral profile below TMPDIR and traps when the
    # resulting Unix-domain socket path is too long. Process workspaces
    # commonly exceed that limit. Keep the sandbox's clean HOME/PATH
    # contract, but give this trusted platform daemon the system's short
    # sticky temp root. Playwright creates its random profile directory
    # mode-private; screenshots and all durable browser output remain
    # jailed below DISCO_WORKSPACE.
    return (
        f"TMPDIR=/var/tmp DISCO_BROWSER_PORT=0{executable_env}{pythonpath_env} "
        f"{shlex.quote(daemon_python)} {daemon_target_path}"
    )


async def _start_daemon_session(
    ctx: ToolContext,
    command: str,
    *,
    unavailable_message: str,
    bounded_startup_diagnostic: Callable[..., str],
    browser_unavailable_error: type[BrowserUnavailableError],
) -> None:
    # `BrowserTool.run` proves the sandbox before dispatching; the extraction
    # moved this code out of that narrowed scope, so restate the parent's own
    # invariant (it carried this same assert at browser.py:373/735/795/808/821).
    assert ctx.sandbox is not None
    assert ctx.sessions is not None
    started = await ctx.sessions.exec("__browser", command, None)
    if getattr(started, "running", None) is False:
        diagnostic = bounded_startup_diagnostic(
            getattr(started, "output", ""),
            getattr(started, "exit_code", None),
        )
        raise browser_unavailable_error(
            unavailable_message,
            startup_diagnostic=diagnostic or "browser daemon exited during startup",
        )


async def _poll_for_health(
    ctx: ToolContext,
    daemon_url_default: str,
    daemon_port_path: str,
    *,
    attempts: int = 10,
    interval_s: float = 1.0,
) -> str | None:
    for _ in range(attempts):
        daemon_url = await _current_url_if_healthy(ctx, daemon_url_default, daemon_port_path)
        if daemon_url is not None:
            return daemon_url
        await asyncio.sleep(interval_s)
    return None


async def _startup_failure_diagnostic(
    ctx: ToolContext, *, bounded_startup_diagnostic: Callable[..., str]
) -> str:
    # `BrowserTool.run` proves the sandbox before dispatching; the extraction
    # moved this code out of that narrowed scope, so restate the parent's own
    # invariant (it carried this same assert at browser.py:373/735/795/808/821).
    assert ctx.sandbox is not None
    assert ctx.sessions is not None
    diagnostic = "browser daemon did not publish a healthy endpoint"
    try:
        view = await ctx.sessions.view("__browser")
        pane = bounded_startup_diagnostic(getattr(view, "output", ""))
        if pane:
            diagnostic = pane
    except Exception:  # noqa: BLE001 — retain the stable fallback diagnostic
        pass
    return diagnostic


async def ensure_daemon(
    ctx: ToolContext,
    *,
    daemon_src_path: Path,
    live_view_src_path: Path,
    daemon_target_path: str,
    daemon_port_path: str,
    daemon_url_default: str,
    browser_executables: Callable[[], dict[str, str]],
    playwright_runtime: Callable[[], tuple[str, str] | None],
    bounded_startup_diagnostic: Callable[..., str],
    unavailable_message: str,
    browser_unavailable_error: type[BrowserUnavailableError],
) -> str:
    """Former ``BrowserTool._ensure_daemon``."""
    assert ctx.sandbox is not None
    assert ctx.sessions is not None

    healthy_url = await _current_url_if_healthy(ctx, daemon_url_default, daemon_port_path)
    if healthy_url is not None:
        return healthy_url

    # The process backend's tmux session outlives the Agent process. After an
    # Agent restart its daemon endpoint/port file can be gone while the
    # platform-owned ``__browser`` pane is still busy with the old python
    # process. Starting directly in that pane raises SessionBusy and exposes
    # an impossible recovery recipe to the model: model-facing shell tools
    # intentionally reject reserved ``__`` sessions. Recover our own bounded
    # internal session here before shipping one fresh daemon. The session
    # manager treats a missing session as an idempotent no-op.
    await ctx.sessions.kill_foreground("__browser")

    await _ship_daemon_files(ctx, daemon_src_path, live_view_src_path, daemon_target_path)
    command = await _build_launch_command(
        ctx,
        daemon_target_path=daemon_target_path,
        daemon_port_path=daemon_port_path,
        browser_executables=browser_executables,
        playwright_runtime=playwright_runtime,
    )
    await _start_daemon_session(
        ctx,
        command,
        unavailable_message=unavailable_message,
        bounded_startup_diagnostic=bounded_startup_diagnostic,
        browser_unavailable_error=browser_unavailable_error,
    )

    healthy_url = await _poll_for_health(ctx, daemon_url_default, daemon_port_path)
    if healthy_url is not None:
        return healthy_url

    diagnostic = await _startup_failure_diagnostic(
        ctx, bounded_startup_diagnostic=bounded_startup_diagnostic
    )
    raise browser_unavailable_error(unavailable_message, startup_diagnostic=diagnostic)
