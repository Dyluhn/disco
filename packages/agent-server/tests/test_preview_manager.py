"""EPIC F — PreviewManager: the platform owns ports/serving/health; the model never
picks a port. These tests prove the north star at the API level (no port can be
supplied or overridden), plus start→URL, status/logs/stop, restart-on-crash, distinct
ports, and graceful URL degradation.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass

import pytest
from disco.agent_server.preview_manager import (
    NoPreviewPortAvailableError,
    PreviewManager,
    PreviewStatus,
)

_PORT_RE = re.compile(r"(?:http\.server\s+|--port[= ]|-p[= ]|PORT=)(\d+)")


@dataclass
class _View:
    running: bool
    output: str


class _FakeSessions:
    """Minimal ShellSessionManager stand-in: tracks which named sessions are 'running'
    and which ports are 'serving'. `exec` simulates a server binding the platform port."""

    def __init__(self, serving: set[int]) -> None:
        self.namespace = ""
        self._serving = serving
        self._running: dict[str, bool] = {}
        self._name_port: dict[str, int] = {}
        self._logs: dict[str, str] = {}
        self.exec_calls: list[tuple[str, str, str | None]] = []

    @staticmethod
    def _port_of(command: str) -> int | None:
        m = _PORT_RE.search(command)
        return int(m.group(1)) if m else None

    async def exec(self, name: str, command: str, exec_dir: str | None) -> None:
        self.exec_calls.append((name, command, exec_dir))
        port = self._port_of(command)
        if port is not None:
            self._name_port[name] = port
            self._serving.add(port)  # server bound the platform port
        self._running[name] = True
        self._logs[name] = f"$ {command}\nServing on port {port}\n"

    async def view(self, name: str, tail_chars: int = 2000) -> _View:
        return _View(running=self._running.get(name, False), output=self._logs.get(name, ""))

    async def kill_foreground(self, name: str) -> str:
        self._running[name] = False
        port = self._name_port.get(name)
        if port is not None:
            self._serving.discard(port)
        return f"killed {name}"

    # test-only crash injector
    def crash(self, name: str) -> None:
        self._running[name] = False
        port = self._name_port.get(name)
        if port is not None:
            self._serving.discard(port)


class _FakeSandbox:
    """SandboxSession-shaped fake exposing exactly the surface PreviewManager uses."""

    def __init__(self, *, backend_name: str = "gvisor", can_expose: bool = True) -> None:
        self.backend_name = backend_name
        self.workspace_path = "/workspace"
        self._serving: set[int] = set()
        self.sessions = _FakeSessions(self._serving)
        self._can_expose = can_expose

    def expose_port(self, port: int) -> str | None:
        # Routing is independent of health; a backend that can't route returns None.
        if not self._can_expose:
            return None
        return f"http://preview.test/{port}/"

    async def fetch_inside(self, port: int, path: str, *, timeout_s: int = 5):
        if port in self._serving:
            return (200, b"<html></html>", "text/html")
        return None


def _mgr(sandbox: _FakeSandbox, **kw) -> PreviewManager:
    # interval 0 / a single health attempt → deterministic + instant
    kw.setdefault("health_attempts", 1)
    kw.setdefault("health_interval_s", 0.0)
    return PreviewManager(sandbox, **kw)


# --------------------------------------------------------------------------- north star


def test_start_has_no_port_parameter() -> None:
    """The platform owns the port: there is no way to pass one to start()."""
    params = inspect.signature(PreviewManager.start).parameters
    assert "port" not in params


def test_port_is_platform_allocated_not_model_supplied() -> None:
    """_allocate_port is THE single place a port is chosen — verify it draws from the
    curated pool and the model never feeds in."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173, 8080])
    assert mgr._allocate_port() == 3000  # first of the platform pool


@pytest.mark.asyncio
async def test_start_allocates_url_from_platform() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(serve_dir="dist", supervise=False)
    assert session.status is PreviewStatus.RUNNING
    assert session.port == 3000  # platform chose it
    assert session.url == "http://preview.test/3000/"
    # the command the platform actually ran bakes in ITS port, serving the dir
    assert "http.server 3000" in session.command
    assert "dist" in session.command


@pytest.mark.asyncio
async def test_model_cannot_override_port_via_command() -> None:
    """Even if a model jams a port into a raw command, the platform port wins and the
    model's port is scrubbed out."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command="npm run dev --port 9999", supervise=False)
    assert session.port == 3000  # platform pool, NOT 9999
    assert "9999" not in session.command  # the model's port was scrubbed
    assert "PORT=3000" in session.command  # platform port injected
    assert session.url == "http://preview.test/3000/"


@pytest.mark.asyncio
async def test_two_previews_get_distinct_ports_without_model_choosing() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173, 8080])
    a = await mgr.start(serve_dir="api", name="api", supervise=False)
    b = await mgr.start(serve_dir="web", name="web", supervise=False)
    assert a.port != b.port
    assert {a.port, b.port} == {3000, 5173}
    # neither call supplied a port; the platform handed out both
    assert a.url and b.url and a.url != b.url


# --------------------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    first = await mgr.start(serve_dir="dist", name="app", supervise=False)
    second = await mgr.start(serve_dir="dist", name="app", supervise=False)
    assert first is second
    assert first.port == second.port
    assert len(mgr.list()) == 1  # not duplicated
    assert len(sandbox.sessions.exec_calls) == 1  # not re-launched


@pytest.mark.asyncio
async def test_status_logs_stop() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    await mgr.start(serve_dir="dist", name="app", supervise=False)

    status = await mgr.status("app")
    assert len(status) == 1 and status[0].status is PreviewStatus.RUNNING

    logs = await mgr.logs("app")
    assert "Serving on port 3000" in logs["app"]

    stopped = await mgr.stop("app")
    assert stopped == ["app"]
    after = await mgr.status("app")
    assert after[0].status is PreviewStatus.STOPPED
    assert after[0].url is None


@pytest.mark.asyncio
async def test_restart_on_crash() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app")  # supervised
    assert session.status is PreviewStatus.RUNNING
    assert session.restart_count == 0

    # the server dies mid-session
    sandbox.sessions.crash("app")
    assert await mgr._probe_health(3000) is False

    # one supervision pass detects the crash and re-issues the SAME command/port
    await mgr._supervise_once()
    assert session.restart_count == 1
    assert session.status is PreviewStatus.RUNNING  # back up on the same platform port
    assert session.port == 3000
    assert len(sandbox.sessions.exec_calls) == 2  # launched, then restarted
    await mgr.aclose()


@pytest.mark.asyncio
async def test_restart_budget_exhausts_to_crashed() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=True)

    # Make every restart fail to bring the port back up (server is hard-broken).
    async def _exec_no_bind(name, command, exec_dir):  # noqa: ANN001
        sandbox.sessions.exec_calls.append((name, command, exec_dir))
        sandbox.sessions._running[name] = False  # exits immediately

    sandbox.sessions.exec = _exec_no_bind  # type: ignore[method-assign]
    sandbox.sessions.crash("app")

    for _ in range(PreviewManager.MAX_RESTARTS + 2):
        await mgr._supervise_once()
    assert session.status is PreviewStatus.CRASHED
    assert session.restart_count == PreviewManager.MAX_RESTARTS
    await mgr.aclose()


# --------------------------------------------------------------------------- degrade / limits


@pytest.mark.asyncio
async def test_graceful_url_degrade_when_backend_cannot_expose() -> None:
    """SEAM note: a healthy preview whose backend can't route a URL is reported
    UNAVAILABLE with a clear reason — never a failure, never a fake URL."""
    sandbox = _FakeSandbox(backend_name="podman", can_expose=False)
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=False)
    assert session.status is PreviewStatus.UNAVAILABLE
    assert session.url is None
    assert "can't expose" in session.detail


@pytest.mark.asyncio
async def test_port_pool_exhaustion_raises() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    await mgr.start(serve_dir="a", name="a", supervise=False)
    with pytest.raises(NoPreviewPortAvailableError):
        await mgr.start(serve_dir="b", name="b", supervise=False)


def test_shared_host_pool_excludes_control_ports() -> None:
    """On process/local the agent-server's own ports (8000/5173/8800) must never be
    allocated for a preview — the platform must not collide with the dev stack."""
    from disco.agent_server.preview_manager import _default_port_pool

    shared = _default_port_pool(_FakeSandbox(backend_name="process"))
    assert 8000 not in shared and 5173 not in shared
    isolated = _default_port_pool(_FakeSandbox(backend_name="gvisor"))
    assert 8000 in isolated  # inside an isolated box 8000 is the box's own
