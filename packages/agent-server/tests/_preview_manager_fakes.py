"""Reusable PreviewManager test fakes and setup mechanics."""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from disco.agent_server.preview_manager import PreviewManager
from disco.agent_server.preview_projection import (
    ActiveLivePreviewProjection,
    SealedPreviewRuntimeContract,
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
        self.stop_server_calls: list[tuple[str, str, int]] = []

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

    async def stop_foreground_server(
        self, name: str, *, expected_command: str, expected_port: int
    ) -> str:
        self.stop_server_calls.append((name, expected_command, expected_port))
        return await self.kill_foreground(name)

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
        self.id = "sbx_preview_manager_test"
        self.generation = 1
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

    async def exec_shell(self, command: str, *, timeout_s: int = 5):
        assert command == "pwd"
        return SimpleNamespace(exit_code=0, stdout="/workspace\n", stderr="")


def _mgr(sandbox: _FakeSandbox, **kw) -> PreviewManager:
    # interval 0 / a single health attempt → deterministic + instant
    kw.setdefault("health_attempts", 1)
    kw.setdefault("health_interval_s", 0.0)
    return PreviewManager(sandbox, **kw)


def _sealed_contract_with_cwd(cwd: str | None) -> SealedPreviewRuntimeContract:
    projection = ActiveLivePreviewProjection(
        projection_id="pv_" + "a" * 32,
        session_name="sealed-web",
        port=3000,
        launch_kind="custom",
        intent_digest="b" * 64,
        sandbox_instance_id="sbx-recorded",
        sandbox_generation=1,
        source_action_id="evt-action",
        source_action_seq=1,
        source_observation_id="evt-observation",
        source_observation_seq=2,
    )
    return SealedPreviewRuntimeContract(
        contract_id="sealed-preview:" + "c" * 64,
        conversation_id="conv-sealed",
        version_seq=3,
        tree_digest="d" * 64,
        terminal_seq=4,
        app_entry="index.html",
        projection=projection,
        session_name="sealed-web",
        serve_dir=None,
        command="python3 server.py",
        framework=None,
        cwd=cwd,
    )


class _Owner:
    def __init__(self, pid: int | None, session: str | None) -> None:
        self.pid = pid
        self.session = session


class _ForeignOwnerSandbox(_FakeSandbox):
    """The allocated port is answered by the LEGACY auto-preview's session, never ours."""

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self._serving:
            return _Owner(pid=4242, session="disco-preview")  # auto-preview, name 'preview'
        return _Owner(pid=None, session=None)


class _OurOwnerSandbox(_FakeSandbox):
    """The allocated port is owned by THIS preview's shell session (`disco-<name>`)."""

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self._serving:
            return _Owner(pid=10, session="disco-app")
        return _Owner(pid=None, session=None)


class _ForeignNamespaceOwnerSandbox(_FakeSandbox):
    """Another CONVERSATION's preview (different namespace, SAME common name `preview`)
    answers on the allocated port — `disco-othercid-preview`, not this session's
    `disco-preview`."""

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self._serving:
            return _Owner(pid=4242, session="disco-othercid-preview")
        return _Owner(pid=None, session=None)


class _TrackedSandbox(_FakeSandbox):
    def __init__(self, tracked: set[int], **kw) -> None:
        super().__init__(**kw)
        self._tracked = set(tracked)

    def tracked_ports(self) -> list[int]:
        return sorted(self._tracked)


class _SocketOccupiedSandbox(_FakeSandbox):
    """Ports can be occupied even when they do not answer HTTP health."""

    def __init__(self, occupied: set[int]) -> None:
        super().__init__()
        self.occupied = set(occupied)

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self.occupied:
            return _Owner(pid=9000 + port, session="foreign-daemon")
        return _Owner(pid=None, session=None)


class _HostExecSandbox(_FakeSandbox):
    """Process-backend-shaped fake that REALLY executes shell probes on the host
    (the process backend's sandbox IS the host), so the bind test's kernel
    semantics — TIME_WAIT ghosts vs live listeners — are exercised for real.
    `port_owner` deliberately sees nothing either way: the kernel-level bind
    test must be the deciding authority even in an attribution blind spot."""

    def __init__(self) -> None:
        super().__init__(backend_name="process")
        self.shares_host_network = True

    async def port_owner(self, port: int):  # noqa: ANN201
        return _Owner(pid=None, session=None)

    async def exec_shell(self, command: str, *, timeout_s: int = 5):
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return SimpleNamespace(
            exit_code=proc.returncode, stdout=stdout.decode(), stderr=stderr.decode()
        )


def _manufacture_server_side_time_wait() -> int:
    """Create the exact kernel state a torn-down sibling preview leaves behind:
    the server side actively closes first (http.server per-request close; node
    keepAliveTimeout), then the listener goes away. Only a TIME_WAIT ghost
    remains on the port — no listener, no owner, no lease."""
    for _ in range(10):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        cli = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn, _ = srv.accept()
        conn.close()  # server actively closes first → server-side TIME_WAIT
        cli.settimeout(5)
        with contextlib.suppress(OSError):
            cli.recv(1)
        cli.close()
        srv.close()  # listener gone; only the ghost remains
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                probe.close()
                return port  # strict bind refuses ⇒ the ghost is present
            probe.close()
            time.sleep(0.02)
    pytest.skip("kernel did not exhibit a TIME_WAIT ghost")


class _StalePreviewSessions(_FakeSessions):
    def __init__(self, serving: set[int], stale: dict[int, str]) -> None:
        super().__init__(serving)
        self._stale = stale
        self.kill_calls: list[str] = []

    async def kill_foreground(self, name: str) -> str:
        self.kill_calls.append(name)
        full = f"disco-{name}"
        for port, session in list(self._stale.items()):
            if session == full:
                del self._stale[port]
                self._serving.discard(port)
        return await super().kill_foreground(name)


class _StalePreviewSandbox(_FakeSandbox):
    def __init__(self) -> None:
        super().__init__()
        self._stale = {3000: "disco-old"}
        self.sessions = _StalePreviewSessions(self._serving, self._stale)

    async def port_owner(self, port: int):  # noqa: ANN201
        session = self._stale.get(port)
        if session is not None:
            return _Owner(pid=4321, session=session)
        return _Owner(pid=None, session=None)


class _StaticServingSessions:
    """Simulates `python3 -m http.server <port> -d <dir>` launched from `exec_dir`,
    resolving the served root the way a real shell would (so `-d dist` from a `dist`
    cwd would serve `dist/dist` — the exact P1 #3 bug)."""

    def __init__(self) -> None:
        self.namespace = ""
        self._running: dict[str, bool] = {}
        self.port_root: dict[int, str] = {}

    async def exec(self, name: str, command: str, exec_dir: str | None) -> None:
        import posixpath

        m = re.search(r"http\.server\s+(\d+)\s+-d\s+(\S+)", command)
        assert m is not None
        port, d = int(m.group(1)), m.group(2)
        base = exec_dir or "/"
        root = d if d.startswith("/") else posixpath.normpath(posixpath.join(base, d))
        self.port_root[port] = root
        self._running[name] = True

    async def view(self, name: str, tail_chars: int = 2000) -> _View:
        return _View(self._running.get(name, False), "")

    async def kill_foreground(self, name: str) -> str:
        self._running[name] = False
        return "killed"


class _StaticServingSandbox:
    def __init__(self, files: set[str]) -> None:
        self.backend_name = "gvisor"
        self.workspace_path = "/workspace"
        self.sessions = _StaticServingSessions()
        self._files = files

    def expose_port(self, port: int) -> str | None:
        return f"http://preview.test/{port}/"

    async def fetch_inside(self, port: int, path: str, *, timeout_s: int = 5):  # noqa: ANN201
        import posixpath

        root = self.sessions.port_root.get(port)
        if root is None:
            return None
        target = (
            posixpath.join(root, "index.html")
            if path in ("", "/")
            else (posixpath.normpath(posixpath.join(root, path.lstrip("/"))))
        )
        if target in self._files:
            return (200, b"<h1>hi</h1>", "text/html")
        return (404, b"Not Found", "text/plain")  # http.server answers, but wrong tree


class _UnlockedSession:
    async def read_file(self, path: str) -> bytes:
        assert path == "package.json"
        return b'{"devDependencies":{"vite":"latest"}}'

    async def file_exists(self, path: str) -> bool:
        return False


class _NeverReadSession:
    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"unsafe cwd reached workspace read: {path}")


class _LifecycleMutationSession:
    def __init__(self) -> None:
        self.files = {
            "web/package.json": b"mutated-by-lifecycle-script",
            "web/package-lock.json": b"sealed-lock",
            "web/src/main.ts": b"mutated-by-lifecycle-script",
            "web/generated-foreign.js": b"foreign",
            "web/node_modules/vite/package.json": b"installed",
            "root-foreign.txt": b"foreign",
        }
        self.staged: dict[str, bytes] = {}

    async def exec_shell(self, command: str, *, timeout_s: int) -> SimpleNamespace:
        if "mv -- web/node_modules .disco-preview-dependencies-" in command:
            self.staged = {
                path.removeprefix("web/node_modules/"): data
                for path, data in self.files.items()
                if path.startswith("web/node_modules/")
            }
            self.files.clear()
        elif command.startswith("mkdir -p -- web"):
            self.files.update(
                {f"web/node_modules/{path}": data for path, data in self.staged.items()}
            )
            self.staged.clear()
        else:  # pragma: no cover - command drift should fail loudly
            raise AssertionError(command)
        return SimpleNamespace(exit_code=0, timed_out=False, stdout="", stderr="")

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]


class _ViteSessions(_FakeSessions):
    async def exec(self, name: str, command: str, exec_dir: str | None) -> None:
        self.exec_calls.append((name, command, exec_dir))
        match = re.search(r"--port[= ](\d+)", command)
        port = int(match.group(1)) if match else 5173
        self._name_port[name] = port
        self._serving.add(port)
        self._running[name] = True
        self._logs[name] = f"$ {command}\nVite serving on port {port}\n"


class _UnattributedSharedSandbox(_FakeSandbox):
    shares_host_network = True

    def __init__(self) -> None:
        super().__init__(backend_name="process")

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self._serving:
            return _Owner(pid=7331, session=None)
        return _Owner(pid=None, session=None)


class _DependencySession:
    def __init__(self, prefix: str = "") -> None:
        self.files = {
            f"{prefix}package.json": b'{"dependencies":{"vite":"1.0.0"}}',
            f"{prefix}package-lock.json": b"{}",
        }
        self.commands: list[str] = []
        self._prefix = prefix

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def file_exists(self, path: str) -> bool:
        return path in self.files

    async def exec_shell(self, command: str, *, timeout_s: int) -> SimpleNamespace:
        self.commands.append(command)
        self.files[f"{self._prefix}node_modules"] = b"directory-marker"
        return SimpleNamespace(exit_code=0, timed_out=False, stdout="", stderr="")


def _manager_fixture(
    *, port_pool: list[int], **sandbox_kwargs: object
) -> tuple[_FakeSandbox, PreviewManager]:
    sandbox = _FakeSandbox(**sandbox_kwargs)
    return sandbox, _mgr(sandbox, port_pool=port_pool)
