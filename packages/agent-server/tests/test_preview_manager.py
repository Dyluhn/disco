"""EPIC F — PreviewManager: the platform owns ports/serving/health; the model never
picks a port. These tests prove the north star at the API level (no port can be
supplied or overridden), plus start→URL, status/logs/stop, restart-on-crash, distinct
ports, and graceful URL degradation.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from disco.agent_server.preview_manager import (
    NoPreviewPortAvailableError,
    PreviewManager,
    PreviewReloadStrategy,
    PreviewStatus,
    preview_requires_node_dependencies,
)
from disco.agent_server.preview_projection import (
    ActiveLivePreviewProjection,
    SealedPreviewRuntimeContract,
)
from disco.agent_server.preview_service import PreviewService

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


# --------------------------------------------------------------------------- north star


def test_start_has_no_port_parameter() -> None:
    """The platform owns the port: there is no way to pass one to start()."""
    params = inspect.signature(PreviewManager.start).parameters
    assert "port" not in params


@pytest.mark.parametrize(
    ("command", "framework", "expected"),
    (
        (None, "vite", True),
        (None, "node", True),
        ("npm run dev", None, True),
        ("NODE_ENV=production node server.js", None, True),
        ("python3 server.py", None, False),
        (None, "static", False),
    ),
)
def test_node_dependency_lifecycle_comes_from_typed_preview_intent(
    command: str | None,
    framework: str | None,
    expected: bool,
) -> None:
    assert preview_requires_node_dependencies(command=command, framework=framework) is expected


@pytest.mark.asyncio
async def test_sealed_node_dependency_restore_uses_the_single_immutable_lockfile() -> None:
    class _DependencySession:
        def __init__(self) -> None:
            self.files = {
                "package.json": b'{"dependencies":{"vite":"1.0.0"}}',
                "package-lock.json": b"{}",
            }
            self.commands: list[str] = []

        async def read_file(self, path: str) -> bytes:
            return self.files[path]

        async def file_exists(self, path: str) -> bool:
            return path in self.files

        async def exec_shell(self, command: str, *, timeout_s: int) -> SimpleNamespace:
            self.commands.append(command)
            self.files["node_modules"] = b"directory-marker"
            return SimpleNamespace(exit_code=0, timed_out=False, stdout="", stderr="")

    session = _DependencySession()
    contract = SimpleNamespace(command=None, framework="vite", cwd=None)

    dependency_dir = await PreviewService._prepare_sealed_node_dependencies(session, contract)

    assert dependency_dir == "node_modules"
    assert session.commands == ["npm ci --no-audit --no-fund"]


@pytest.mark.asyncio
async def test_sealed_node_dependency_restore_refuses_unlocked_graph() -> None:
    class _UnlockedSession:
        async def read_file(self, path: str) -> bytes:
            assert path == "package.json"
            return b'{"devDependencies":{"vite":"latest"}}'

        async def file_exists(self, path: str) -> bool:
            return False

    with pytest.raises(RuntimeError, match="exactly one supported immutable lockfile"):
        await PreviewService._prepare_sealed_node_dependencies(
            _UnlockedSession(),
            SimpleNamespace(command=None, framework="vite", cwd=None),
        )


@pytest.mark.asyncio
async def test_sealed_node_dependency_restore_honors_workspace_relative_cwd() -> None:
    class _DependencySession:
        def __init__(self) -> None:
            self.files = {
                "web/package.json": b'{"devDependencies":{"vite":"1.0.0"}}',
                "web/package-lock.json": b"{}",
            }
            self.commands: list[str] = []

        async def read_file(self, path: str) -> bytes:
            return self.files[path]

        async def file_exists(self, path: str) -> bool:
            return path in self.files

        async def exec_shell(self, command: str, *, timeout_s: int) -> SimpleNamespace:
            self.commands.append(command)
            self.files["web/node_modules"] = b"directory-marker"
            return SimpleNamespace(exit_code=0, timed_out=False, stdout="", stderr="")

    session = _DependencySession()
    dependency_dir = await PreviewService._prepare_sealed_node_dependencies(
        session,
        SimpleNamespace(command=None, framework="vite", cwd="/workspace/web"),
    )

    assert dependency_dir == "web/node_modules"
    assert session.commands == ["npm --prefix ./web ci --no-audit --no-fund"]


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


def test_every_sealed_runtime_normalizes_cwd_before_non_node_launch() -> None:
    normalized = PreviewService._normalized_sealed_runtime_contract(
        _sealed_contract_with_cwd("/workspace/web")
    )
    assert normalized.cwd == "./web"
    assert normalized.command == "python3 server.py"

    with pytest.raises(RuntimeError, match="inside /workspace"):
        PreviewService._normalized_sealed_runtime_contract(_sealed_contract_with_cwd("/etc"))


@pytest.mark.parametrize("cwd", ("/etc", "../web", "web/../../escape", " web"))
@pytest.mark.asyncio
async def test_sealed_node_dependency_restore_rejects_cwd_outside_workspace(cwd: str) -> None:
    class _NeverReadSession:
        async def read_file(self, path: str) -> bytes:
            raise AssertionError(f"unsafe cwd reached workspace read: {path}")

    with pytest.raises(RuntimeError, match="workspace|POSIX"):
        await PreviewService._prepare_sealed_node_dependencies(
            _NeverReadSession(),
            SimpleNamespace(command=None, framework="vite", cwd=cwd),
        )


@pytest.mark.asyncio
async def test_post_install_restore_removes_script_mutations_but_keeps_dependencies() -> None:
    sealed = (
        ("web/package.json", b'{"scripts":{"postinstall":"mutate"}}'),
        ("web/package-lock.json", b"sealed-lock"),
        ("web/src/main.ts", b"sealed-source"),
    )

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

    session = _LifecycleMutationSession()
    await PreviewService._replace_with_sealed_workspace(
        session,
        sealed,
        dependency_dir="web/node_modules",
        contract_id="sealed-preview:" + "a" * 64,
    )

    assert session.files == {
        "web/package.json": b'{"scripts":{"postinstall":"mutate"}}',
        "web/package-lock.json": b"sealed-lock",
        "web/src/main.ts": b"sealed-source",
        "web/node_modules/vite/package.json": b"installed",
    }


@pytest.mark.asyncio
async def test_port_is_platform_allocated_not_model_supplied() -> None:
    """_allocate_port is THE single place a port is chosen — verify it draws from the
    curated pool and the model never feeds in."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173, 8080])
    assert await mgr._allocate_port() == 3000  # first of the platform pool


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
    assert session.reload_strategy is PreviewReloadStrategy.RELOAD
    assert session.to_dict()["generation"] == session.projection_id
    assert session.to_dict()["reload_strategy"] == "reload"


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


@pytest.mark.asyncio
async def test_restart_canonical_reuses_exact_stored_intent() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    original = await mgr.start(command="node server.js", name="preview", supervise=False)
    original_intent = dict(original.intent)
    original_projection = original.projection_id

    assert await mgr.stop("preview") == ["preview"]
    restarted = await mgr.restart_canonical()

    assert restarted is not None
    assert restarted.status is PreviewStatus.RUNNING
    assert restarted.intent == original_intent
    assert restarted.projection_id != original_projection
    assert sandbox.sessions.exec_calls[-1][1] == restarted.command


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [PreviewStatus.STARTING, PreviewStatus.RESTARTING])
async def test_restart_canonical_recovers_dead_transitional_process(
    state: PreviewStatus,
) -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(command="node server.js", name="preview", supervise=False)
    sandbox.sessions.crash(session.name)
    session.status = state
    before = len(sandbox.sessions.exec_calls)

    restarted = await mgr.restart_canonical()

    assert restarted is session
    assert restarted.status is PreviewStatus.RUNNING
    assert len(sandbox.sessions.exec_calls) == before + 1


@pytest.mark.asyncio
async def test_restart_canonical_without_accepted_intent_fails_closed() -> None:
    mgr = _mgr(_FakeSandbox(), port_pool=[3000])
    assert await mgr.restart_canonical() is None


@pytest.mark.asyncio
async def test_sealed_restore_revalidates_raw_intent_and_binds_fresh_generation() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    expected_intent = {
        "serve_dir": None,
        "command": "python3 server.py",
        "framework": None,
        "cwd": None,
        "launch_kind": "custom",
    }
    contract = SimpleNamespace(
        contract_id="sealed-preview:" + "a" * 64,
        start_kwargs=lambda: {
            "serve_dir": None,
            "command": "python3 server.py",
            "framework": None,
            "cwd": None,
            "name": "web",
            "supervise": True,
        },
        intent=lambda: expected_intent,
    )

    restored = await mgr.restore_sealed(contract)

    assert restored is not None and restored.status is PreviewStatus.RUNNING
    assert restored.port == 3000
    assert restored.intent == expected_intent
    assert sandbox.sessions.exec_calls == [("web", "PORT=3000 python3 server.py", "/workspace")]
    assert await mgr.resolve_sealed_contract(contract) is restored
    await mgr.aclose()


@pytest.mark.asyncio
async def test_sealed_binding_fails_closed_after_intent_or_sandbox_identity_changes() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    expected_intent = {
        "serve_dir": None,
        "command": "python3 server.py",
        "framework": None,
        "cwd": None,
        "launch_kind": "custom",
    }
    contract = SimpleNamespace(
        contract_id="sealed-preview:" + "b" * 64,
        start_kwargs=lambda: {
            "command": "python3 server.py",
            "name": "web",
            "supervise": True,
        },
        intent=lambda: expected_intent,
    )
    restored = await mgr.restore_sealed(contract)
    assert restored is not None

    restored.intent = {**expected_intent, "command": "python3 other.py"}
    assert await mgr.resolve_sealed_contract(contract) is None
    restored.intent = expected_intent
    sandbox.id = "sbx-foreign-generation"
    sandbox.generation = 2
    assert await mgr.resolve_sealed_contract(contract) is None
    await mgr.aclose()


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
async def test_active_projection_matches_only_the_original_live_generation() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(command="python3 server.py", supervise=False)
    data = session.to_dict()
    projection = ActiveLivePreviewProjection(
        projection_id=data["projection_id"],
        session_name=data["name"],
        port=data["port"],
        launch_kind=data["launch_kind"],
        intent_digest=data["intent_digest"],
        sandbox_instance_id=data["sandbox_instance_id"],
        sandbox_generation=data["sandbox_generation"],
        source_action_id="evt_action",
        source_action_seq=1,
        source_observation_id="evt_observation",
        source_observation_seq=2,
    )

    assert await mgr.resolve_active_projection(projection) is session
    assert len(sandbox.sessions.exec_calls) == 1

    # A sandbox recreation can run the same name/command/port, but it is not the
    # generation the terminal proof authorized.  The cached PreviewSession still
    # carries the old identity here, which must not bless the replacement box.
    sandbox.id = "sbx_preview_manager_recreated"
    sandbox.generation = 2
    assert await mgr.resolve_active_projection(projection) is None
    assert len(sandbox.sessions.exec_calls) == 1


@pytest.mark.asyncio
async def test_active_projection_is_revoked_by_automatic_process_restart() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(command="python3 server.py", supervise=True)
    data = session.to_dict()
    projection = ActiveLivePreviewProjection(
        projection_id=data["projection_id"],
        session_name=data["name"],
        port=data["port"],
        launch_kind=data["launch_kind"],
        intent_digest=data["intent_digest"],
        sandbox_instance_id=data["sandbox_instance_id"],
        sandbox_generation=data["sandbox_generation"],
        source_action_id="evt_action",
        source_action_seq=1,
        source_observation_id="evt_observation",
        source_observation_seq=2,
    )
    assert await mgr.resolve_active_projection(projection) is session

    sandbox.sessions.crash(session.name)
    await mgr._supervise_once()

    assert session.status is PreviewStatus.RUNNING
    assert len(sandbox.sessions.exec_calls) == 2
    assert session.projection_id != projection.projection_id
    assert session.to_dict()["generation"] == session.projection_id
    assert await mgr.resolve_active_projection(projection) is None
    await mgr.aclose()


@pytest.mark.asyncio
async def test_canonical_selection_tracks_explicit_success_and_falls_back() -> None:
    """H333: canonical routing follows the newest successful explicit selection."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    api = await mgr.start(serve_dir="api", name="api", supervise=False)
    web = await mgr.start(serve_dir="web", name="web", supervise=False)
    assert mgr.canonical_port() == web.port == 5173

    # An explicit idempotent start is a deliberate reselection, not a duplicate launch.
    assert await mgr.start(serve_dir="api", name="api", supervise=False) is api
    assert mgr.canonical_port() == api.port == 3000

    await mgr.stop("api")
    assert mgr.canonical_port() == web.port
    web.status = PreviewStatus.CRASHED
    assert mgr.canonical_port() is None
    assert mgr.canonical_lifecycle_session() is web


@pytest.mark.asyncio
async def test_crashed_newest_selection_never_substitutes_older_healthy_app() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    older = await mgr.start(serve_dir="older", name="older", supervise=False)
    newest = await mgr.start(serve_dir="newest", name="newest", supervise=False)

    newest.status = PreviewStatus.CRASHED
    newest.detail = "runtime exited"

    assert older.status is PreviewStatus.RUNNING
    assert mgr.canonical_session() is None
    assert mgr.canonical_port() is None
    assert mgr.canonical_lifecycle_session() is newest


@pytest.mark.asyncio
async def test_reload_strategy_comes_from_authoritative_launch_intent() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173, 8080])

    vite = await mgr.start(framework="vite", name="vite", supervise=False)
    node = await mgr.start(framework="node", name="node", supervise=False)
    custom = await mgr.start(command="npm run dev", name="custom", supervise=False)

    assert vite.intent["launch_kind"] == "framework"
    assert vite.reload_strategy is PreviewReloadStrategy.HMR
    assert node.intent["launch_kind"] == "framework"
    assert node.reload_strategy is PreviewReloadStrategy.RELOAD
    assert custom.intent["launch_kind"] == "custom"
    assert custom.reload_strategy is PreviewReloadStrategy.RELOAD


@pytest.mark.asyncio
async def test_delayed_health_activates_newest_explicit_canonical_selection() -> None:
    """A STARTING explicit selection becomes canonical when supervision proves health."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    first = await mgr.start(serve_dir="first", name="first", supervise=False)
    assert mgr.canonical_port() == first.port == 3000

    real_fetch = sandbox.fetch_inside
    delay_second = True

    async def delayed_fetch(port: int, path: str, *, timeout_s: int = 5):  # noqa: ANN202
        if delay_second and port == 5173:
            return None
        return await real_fetch(port, path, timeout_s=timeout_s)

    sandbox.fetch_inside = delayed_fetch  # type: ignore[method-assign]
    second = await mgr.start(command="python3 server.py", name="second", supervise=True)
    assert second.status is PreviewStatus.STARTING
    assert mgr.canonical_port() is None
    assert mgr.canonical_lifecycle_session() is second

    delay_second = False
    await mgr._supervise_once()

    assert second.status is PreviewStatus.RUNNING
    assert mgr.canonical_port() == second.port == 5173
    await mgr.aclose()


@pytest.mark.asyncio
async def test_status_logs_stop() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=False)

    status = await mgr.status("app")
    assert len(status) == 1 and status[0].status is PreviewStatus.RUNNING

    logs = await mgr.logs("app")
    assert "Serving on port 3000" in logs["app"]

    stopped = await mgr.stop("app")
    assert stopped == ["app"]
    after = await mgr.status("app")
    assert after[0].status is PreviewStatus.STOPPED
    assert after[0].url is None
    assert sandbox.sessions.stop_server_calls == [("app", session.command, 3000)]


@pytest.mark.asyncio
async def test_restart_on_crash() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app")  # supervised
    original_projection_id = session.projection_id
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
    assert session.projection_id != original_projection_id
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


@pytest.mark.asyncio
async def test_never_healthy_startup_failure_is_not_silently_retried() -> None:
    """H319: one broken start must not become four hidden process launches."""

    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])

    async def _exec_exits(name, command, exec_dir):  # noqa: ANN001
        sandbox.sessions.exec_calls.append((name, command, exec_dir))
        sandbox.sessions._running[name] = False

    sandbox.sessions.exec = _exec_exits  # type: ignore[method-assign]
    session = await mgr.start(serve_dir="dist", name="app", supervise=True)
    assert session.status is PreviewStatus.CRASHED
    assert session.restart_count == 0

    for _ in range(PreviewManager.MAX_RESTARTS + 2):
        await mgr._supervise_once()

    assert len(sandbox.sessions.exec_calls) == 1
    assert session.restart_count == 0
    assert session.status is PreviewStatus.CRASHED
    await mgr.aclose()


@pytest.mark.asyncio
async def test_explicit_start_recovers_never_healthy_session_after_repair() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    working_exec = sandbox.sessions.exec

    async def _exec_exits(name, command, exec_dir):  # noqa: ANN001
        sandbox.sessions.exec_calls.append((name, command, exec_dir))
        sandbox.sessions._running[name] = False

    sandbox.sessions.exec = _exec_exits  # type: ignore[method-assign]
    session = await mgr.start(serve_dir="dist", name="app", supervise=True)
    assert session.status is PreviewStatus.CRASHED
    await mgr._supervise_once()
    assert len(sandbox.sessions.exec_calls) == 1

    sandbox.sessions.exec = working_exec  # type: ignore[method-assign]
    recovered = await mgr.start(serve_dir="dist", name="app", supervise=True)

    assert recovered is session
    assert session.status is PreviewStatus.RUNNING
    assert session.restart_count == 0
    assert len(sandbox.sessions.exec_calls) == 2
    await mgr.aclose()


@pytest.mark.asyncio
async def test_explicit_recovery_revalidates_and_replaces_failed_command() -> None:
    """H319: a same-name repair must execute current intent, not stale intent."""

    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])

    async def _exec_exits(name, command, exec_dir):  # noqa: ANN001
        sandbox.sessions.exec_calls.append((name, command, exec_dir))
        sandbox.sessions._running[name] = False

    sandbox.sessions.exec = _exec_exits  # type: ignore[method-assign]
    session = await mgr.start(
        command="python3 -m http.server {port} -d broken",
        name="app",
        supervise=True,
    )
    assert session.status is PreviewStatus.CRASHED

    working_exec = _FakeSessions.exec.__get__(sandbox.sessions, _FakeSessions)
    sandbox.sessions.exec = working_exec  # type: ignore[method-assign]
    recovered = await mgr.start(
        command="python3 -m http.server {port} -d repaired",
        name="app",
        supervise=True,
    )

    assert recovered is session
    assert session.status is PreviewStatus.RUNNING
    assert session.restart_count == 0
    assert session._auto_restart_armed is True
    assert sandbox.sessions.exec_calls[-1][1].endswith("-d repaired")
    await mgr.aclose()


@pytest.mark.asyncio
async def test_explicit_start_recovers_exhausted_session_and_resets_budget_on_health() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=False)
    sandbox.sessions.crash("app")
    session.status = PreviewStatus.CRASHED
    session.restart_count = PreviewManager.MAX_RESTARTS
    before = len(sandbox.sessions.exec_calls)

    recovered = await mgr.start(serve_dir="dist", name="app", supervise=False)

    assert recovered is session
    assert session.status is PreviewStatus.RUNNING
    assert session.restart_count == 0
    assert len(sandbox.sessions.exec_calls) == before + 1


@pytest.mark.asyncio
async def test_failed_explicit_exhausted_recovery_does_not_rearm_supervisor() -> None:
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=True)

    async def _exec_exits(name, command, exec_dir):  # noqa: ANN001
        sandbox.sessions.exec_calls.append((name, command, exec_dir))
        sandbox.sessions._running[name] = False

    sandbox.sessions.exec = _exec_exits  # type: ignore[method-assign]
    sandbox.sessions.crash("app")
    session.status = PreviewStatus.CRASHED
    session.restart_count = PreviewManager.MAX_RESTARTS
    before = len(sandbox.sessions.exec_calls)

    await mgr.start(serve_dir="dist", name="app", supervise=True)

    assert session.status is PreviewStatus.CRASHED
    assert session.restart_count == PreviewManager.MAX_RESTARTS
    assert "explicit recovery failed" in session.detail
    assert "static preview root did not return a successful HTTP response" in session.detail
    assert len(sandbox.sessions.exec_calls) == before + 1
    for _ in range(PreviewManager.MAX_RESTARTS + 2):
        await mgr._supervise_once()
    assert len(sandbox.sessions.exec_calls) == before + 1
    await mgr.aclose()


@pytest.mark.asyncio
async def test_live_crashed_recovery_does_not_stage_unlaunched_command() -> None:
    """A CRASHED label on a live process cannot mutate its launch generation."""

    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(
        command="python3 -m http.server {port} -d original",
        name="app",
        supervise=True,
    )
    original_command = session.command
    original_intent = dict(session.intent)
    sandbox._serving.discard(session.port)
    session.status = PreviewStatus.CRASHED
    before = len(sandbox.sessions.exec_calls)

    recovered = await mgr.start(
        command="python3 -m http.server {port} -d replacement",
        name="app",
        supervise=True,
    )

    assert recovered is session
    assert len(sandbox.sessions.exec_calls) == before
    assert session.command == original_command
    assert session.intent == original_intent
    assert session.status is PreviewStatus.STARTING
    assert session._auto_restart_armed is False
    await mgr.aclose()


# ----------------------------------------------------------- supervise: live ≠ crashed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [PreviewStatus.STARTING, PreviewStatus.UNAVAILABLE, PreviewStatus.RESTARTING],
)
async def test_live_but_unhealthy_session_is_not_restarted(state: PreviewStatus) -> None:
    """P1 #1 regression: a session whose PROCESS is still alive but momentarily not
    answering health (slow/headless boot, or up-but-unroutable) must NOT be restarted
    or misclassified as CRASHED — for ANY non-terminal state, not just RUNNING. The
    previous guard only spared RUNNING, so STARTING/UNAVAILABLE/RESTARTING sessions got
    a spurious restart (re-exec into a busy shell → CRASHED → restart budget burned)."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=True)
    assert session.status is PreviewStatus.RUNNING

    # Process stays ALIVE, but it stops answering health (drop the port from 'serving'
    # WITHOUT killing the shell session).
    sandbox._serving.discard(session.port)
    assert sandbox.sessions._running["app"] is True
    assert await mgr._probe_health(session.port) is False
    session.status = state
    exec_before = len(sandbox.sessions.exec_calls)

    await mgr._supervise_once()

    assert session.status is state  # NOT flipped to CRASHED
    assert session.restart_count == 0  # budget NOT burned
    assert len(sandbox.sessions.exec_calls) == exec_before  # NOT re-launched
    await mgr.aclose()


@pytest.mark.asyncio
async def test_restart_budget_decrements_only_on_genuine_process_exit() -> None:
    """The supervisor spends a restart ONLY when the process has actually exited. An
    alive-but-unhealthy pass leaves the budget intact; a genuine exit then restarts."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=True)

    # Alive but not answering → no restart, budget intact.
    sandbox._serving.discard(session.port)
    await mgr._supervise_once()
    assert session.restart_count == 0
    assert session.status is not PreviewStatus.CRASHED

    # Now the process genuinely EXITS → the supervisor restarts it (budget decrements).
    sandbox.sessions.crash("app")
    assert await mgr._session_alive("app") is False
    await mgr._supervise_once()
    assert session.restart_count == 1
    assert session.status is PreviewStatus.RUNNING  # back up on the same port
    await mgr.aclose()


@pytest.mark.asyncio
async def test_start_refresh_does_not_restart_crashed_but_live_session() -> None:
    """P1 #2 (codex repro): the restart budget must not burn OUTSIDE the supervisor.

    `start()`'s idempotent-refresh path re-probes an existing session and, if it reads
    CRASHED, funnels into `_restart()`. But a process that is still ALIVE (just not
    answering health) is NOT a crash — re-execing it burns a restart for nothing. The
    centralized liveness guard inside `_restart()` must spare it here exactly as it does
    on the supervisor path: no re-exec, no `restart_count` bump.

    Reproduces codex's exact scenario: status=CRASHED, restart_count=1, _session_alive
    True → after start(), exec_calls unchanged and restart_count unchanged.
    """
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=False)
    assert session.status is PreviewStatus.RUNNING

    # Drive into codex's exact state: process ALIVE, not answering health, mislabeled
    # CRASHED with a restart already on the clock.
    sandbox._serving.discard(session.port)  # stops answering health
    assert sandbox.sessions._running["app"] is True  # but the PROCESS is alive
    assert await mgr._probe_health(session.port) is False
    session.status = PreviewStatus.CRASHED
    session.restart_count = 1
    exec_before = len(sandbox.sessions.exec_calls)

    # Re-calling start() refreshes the live session → must NOT re-exec or burn budget.
    again = await mgr.start(serve_dir="dist", name="app", supervise=False)

    assert again is session
    assert len(sandbox.sessions.exec_calls) == exec_before  # exec_calls == 0 new re-execs
    assert session.restart_count == 1  # budget unchanged
    assert session.status is not PreviewStatus.CRASHED  # no longer falsely terminal


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


# ============================================================ P1 #1: raw-command ports

from disco.agent_server.preview_manager import (  # noqa: E402
    PreviewCommandError,
    PreviewSession,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "python3 -m http.server 9999 -d dist",  # positional http.server port
        "uvicorn app:app --host 0.0.0.0 9000",  # trailing host:port-ish positional bind
        "gunicorn app:app -b 0.0.0.0:8000",  # host:port bind argument
        "serve -l :4321",  # bare :port bind
    ],
)
async def test_raw_command_binding_a_hardcoded_port_is_rejected(command: str) -> None:
    """P1 #1: a raw command that binds a MODEL-chosen port through a form the flag-scrub
    can't override is REJECTED — the platform must own the port, so the manager refuses to
    launch a server on a port it doesn't control (rather than believing it owns another)."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=command, supervise=False)
    assert mgr.list() == []  # nothing registered; no port leaked


@pytest.mark.asyncio
async def test_raw_command_port_placeholder_is_filled_with_platform_port() -> None:
    """P1 #1 escape hatch: a raw command may declare WHERE the port goes with the literal
    `{port}` placeholder — the platform fills it with ITS allocated port (never the
    model's), so positional-port servers stay platform-owned."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command="python3 -m http.server {port} -d dist", supervise=False)
    assert session.port == 3000
    assert "http.server 3000" in session.command
    assert "{port}" not in session.command
    assert session.status is PreviewStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "python3 -m http.server {port} 9999",  # placeholder AND a positional port
        "gunicorn app:app -b :{port} -b :8000",  # placeholder AND a host:port bind
        "uvicorn app:app --host 0.0.0.0 {port} 9000",  # placeholder AND trailing port
    ],
)
async def test_placeholder_with_extra_hardcoded_port_is_rejected(command: str) -> None:
    """P1 #1: the `{port}` placeholder is the sanctioned way to position the platform port,
    but a SECOND, hardcoded/positional port alongside it would still bind a model-chosen
    port the platform doesn't own. The hardcoded-port rejection runs even on the placeholder
    path, so such a command is REFUSED (it can no longer slip past by also carrying `{port}`)."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=command, supervise=False)
    assert mgr.list() == []  # nothing registered; no port leaked, port 9999 never bound


# ====================================== P1 (re-sweep): grammar restriction at the input

# The regex-only hardcoded-port detection can't beat arbitrary shell (a `&`-chained or
# substituted second listener). So the manager RESTRICTS the grammar: a raw preview
# command must be a SINGLE FOREGROUND process (no control/chaining/background/pipe/
# substitution operators) AND carry no bare positional port-like token. The operator-ban
# + post-launch ownership probe are the guarantees; the positional scan is belt-and-braces.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        # The exact smuggle: a backgrounded first listener on a curated port (4321 is
        # positional-after-flags so the old regex missed it) `&`-chained to the {port}
        # platform server, which passes the ownership probe while 4321 is also bound.
        "python3 -m http.server --bind 0.0.0.0 4321 -d d & python3 -m http.server {port} -d d",
        "python3 -m http.server {port} -d dist; python3 -m http.server 4321",  # `;` chain
        "python3 -m http.server {port} -d dist | tee log",  # `|` pipe
        "true && python3 -m http.server {port} -d dist",  # `&&`
        "false || python3 -m http.server {port} -d dist",  # `||`
        "python3 -m http.server `echo {port}` -d dist",  # backtick subst
        "python3 -m http.server $(echo {port}) -d dist",  # $() subst
        ">(python3 -m http.server {port})",  # >( ) proc subst
        "python3 -m http.server {port}\npython3 -m http.server 4321",  # newline
    ],
)
async def test_raw_command_with_shell_operator_is_rejected(command: str) -> None:
    """Grammar restriction: any shell control / chaining / background / pipe / substitution
    operator (or newline) refuses the command at the root — killing the chained/backgrounded
    second-listener smuggle vector before any detection regex has to win."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=command, supervise=False)
    assert mgr.list() == []  # nothing registered; no port leaked, 4321 never bound


@pytest.mark.asyncio
async def test_positional_port_after_flags_is_rejected() -> None:
    """The half of the smuggle the flag-scrub regex missed on its own: a port sitting
    positionally AFTER flags (`http.server --bind 0.0.0.0 4321 -d dist`). shlex tokenizing
    sees 4321 as a bare positional port (not a flag value) and rejects it."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    cmd = "python3 -m http.server --bind 0.0.0.0 4321 -d dist"
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=cmd, supervise=False)
    assert mgr.list() == []


@pytest.mark.asyncio
async def test_clean_single_foreground_placeholder_command_works() -> None:
    """The sanctioned form passes untouched: one foreground command with `{port}`."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command="python3 -m http.server {port} -d dist", supervise=False)
    assert session.status is PreviewStatus.RUNNING
    assert session.port == 3000
    assert "http.server 3000" in session.command and "{port}" not in session.command


@pytest.mark.asyncio
async def test_numeric_flag_value_is_not_misread_as_port() -> None:
    """A numeric token following a flag is that flag's VALUE, not a port: a legit
    `uvicorn app:app --port {port} --workers 4` is accepted (4 is `--workers`'s value),
    and the platform port is placed via the placeholder."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command="uvicorn app:app --port {port} --workers 4", supervise=False)
    assert session.status is PreviewStatus.RUNNING
    assert session.port == 3000
    assert "--workers 4" in session.command  # the non-port numeric arg survived
    assert "--port 3000" in session.command  # placeholder filled with the platform port


# ============================== P1 (re-sweep): quoted / =-joined concrete-port bypass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        'npm run dev --port "8000"',  # quoted value — old regex needed BARE digits
        "npm run dev --port='8000'",  # =-joined + quoted — one token after shlex
        "npm run dev -p8000",  # short flag, directly joined (no separator)
        "npm run dev -p 8000",  # short flag, space-separated
        "npm run dev -p='8000'",  # short flag, =-joined + quoted
        "PORT='8000' npm run dev",  # leading PORT= env-assignment, quoted
    ],
)
async def test_quoted_or_joined_concrete_port_flag_is_scrubbed_to_platform_port(
    command: str,
) -> None:
    """RE-SWEEP P1: the port-flag scrub used to be a regex on the RAW string that only fired
    on BARE digits (`--port\\s+\\d+`), so a quoted / `=`-joined / directly-joined value slipped
    past it — AND past the positional-port rejection (after shlex the value is the flag's
    token, not a bare integer). Handling the scrub on the shlex'd ARGV TOKENS normalizes every
    form, so each model-chosen port is dropped and only the PLATFORM port is bound."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command=command, supervise=False)
    assert session.port == 3000  # platform pool, NOT 8000
    assert "8000" not in session.command  # the model's port was scrubbed in every form
    assert "PORT=3000" in session.command  # platform port injected
    assert session.status is PreviewStatus.RUNNING
    assert 8000 not in sandbox._serving  # the model-chosen port was NEVER bound


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expect",
    [
        ("uvicorn app:app --port {port}", "--port 3000"),  # long flag, space-separated
        ("uvicorn app:app --port={port}", "--port=3000"),  # long flag, =-joined
        ("uvicorn app:app -p {port}", "-p 3000"),  # short flag, space-separated
        ("uvicorn app:app -p{port}", "-p3000"),  # short flag, directly joined
        ("uvicorn app:app -p={port}", "-p=3000"),  # short flag, =-joined
    ],
)
async def test_port_placeholder_in_every_flag_form_is_filled_with_platform_port(
    command: str, expect: str
) -> None:
    """The `{port}` placeholder is the sanctioned way to express the serve port on a flag,
    in EVERY normalized form (space / `=`-joined / directly-joined, long or short flag): it
    is kept and filled with the PLATFORM port, never dropped — so a server that reads the
    port off argv (not env) still gets the platform's port."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command=command, supervise=False)
    assert session.port == 3000
    assert expect in session.command  # placeholder filled with the platform port
    assert "{port}" not in session.command
    assert session.status is PreviewStatus.RUNNING


@pytest.mark.asyncio
async def test_quoted_concrete_port_with_workers_keeps_workers_drops_port() -> None:
    """A real mixed command: a quoted concrete `--port` is dropped while a legit
    `--workers N` survives — proving the scrub is flag-specific, not a blanket digit purge."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    session = await mgr.start(command='uvicorn app:app --port "8000" --workers 4', supervise=False)
    assert session.port == 3000
    assert "8000" not in session.command
    assert "--workers 4" in session.command  # non-port numeric arg untouched
    assert "PORT=3000" in session.command
    assert session.status is PreviewStatus.RUNNING


# ---------------------------------------------- P1 #1/#2: post-launch port ownership


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


@pytest.mark.asyncio
async def test_foreign_owner_answering_is_not_marked_running() -> None:
    """P1 #2: our process EADDRINUSE'd but the legacy auto-preview answers on the SAME
    port — health alone would falsely PASS. The post-launch ownership check sees a FOREIGN
    tmux session owns the port and refuses RUNNING (→ CRASHED), so a build is never
    declared live against a server that isn't ours."""
    sandbox = _ForeignOwnerSandbox()
    sandbox._serving.add(3000)  # auto-preview already answers on the port
    mgr = _mgr(sandbox, port_pool=[3000])
    # Bypass allocation (which now SKIPS an occupied port) to drive the EADDRINUSE race
    # directly: a session already pinned to the port a foreign server answers on.
    session = PreviewSession(
        name="app",
        port=3000,
        command="python3 -m http.server 3000 -d dist",
        exec_dir="/workspace",
        intent={},
        _supervise=False,
    )
    mgr._sessions["app"] = session
    await mgr._launch(session)
    assert session.status is PreviewStatus.CRASHED
    assert "different process" in session.detail.lower()
    assert session.url is None


@pytest.mark.asyncio
async def test_owned_port_is_marked_running() -> None:
    """The positive case: when THIS preview's session owns the answering port, ownership
    confirms and the preview goes RUNNING with its URL."""
    sandbox = _OurOwnerSandbox()
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=False)
    assert session.status is PreviewStatus.RUNNING
    assert session.port == 3000
    assert session.url == "http://preview.test/3000/"


class _ForeignNamespaceOwnerSandbox(_FakeSandbox):
    """Another CONVERSATION's preview (different namespace, SAME common name `preview`)
    answers on the allocated port — `disco-othercid-preview`, not this session's
    `disco-preview`."""

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self._serving:
            return _Owner(pid=4242, session="disco-othercid-preview")
        return _Owner(pid=None, session=None)


@pytest.mark.asyncio
async def test_foreign_namespace_owner_with_same_name_is_not_misattributed() -> None:
    """P1 #2: a different conversation's `disco-othercid-preview` ends in `-preview`, so a
    loose `-{name}` suffix match would have mis-accepted it as OURS and false-marked RUNNING
    against a foreign listener during the launch race. The exact-identity check requires the
    full `disco-{ns}{name}` session id, so the foreign owner → CRASHED, not RUNNING."""
    sandbox = _ForeignNamespaceOwnerSandbox()
    sandbox._serving.add(3000)  # foreign conversation already answers on the port
    mgr = _mgr(sandbox, port_pool=[3000])
    session = PreviewSession(
        name="preview",
        port=3000,
        command="python3 -m http.server 3000 -d dist",
        exec_dir="/workspace",
        intent={},
        _supervise=False,
    )
    mgr._sessions["preview"] = session
    await mgr._launch(session)
    assert session.status is PreviewStatus.CRASHED
    assert session.url is None
    assert "different process" in session.detail.lower()


def test_owner_match_requires_full_exact_session_identity() -> None:
    """Unit-level: `_owner_is_this_session` accepts ONLY the exact `disco-{ns}{name}` id.
    A foreign-namespace session ending in `-{name}`, or the bare `{name}`, is rejected."""
    sandbox = _FakeSandbox()
    sandbox.sessions.namespace = ""  # this conversation: id is `disco-preview`
    mgr = _mgr(sandbox, port_pool=[3000])
    assert mgr._owner_is_this_session("disco-preview", "preview") is True
    assert mgr._owner_is_this_session("disco-othercid-preview", "preview") is False
    assert mgr._owner_is_this_session("preview", "preview") is False
    # And it honors a non-empty namespace exactly.
    sandbox.sessions.namespace = "mycid-"
    assert mgr._owner_is_this_session("disco-mycid-preview", "preview") is True
    assert mgr._owner_is_this_session("disco-preview", "preview") is False


# ----------------------------------------- P1 #2: allocation skips already-claimed ports


class _TrackedSandbox(_FakeSandbox):
    def __init__(self, tracked: set[int], **kw) -> None:
        super().__init__(**kw)
        self._tracked = set(tracked)

    def tracked_ports(self) -> list[int]:
        return sorted(self._tracked)


@pytest.mark.asyncio
async def test_allocation_skips_port_tracked_by_sandbox() -> None:
    """P1 #2: a port the SANDBOX already tracks as a service (the legacy auto-preview on
    8000, or an agent dev server) must not be allocated for a new preview — else the
    existing server's response would falsely validate the new one."""
    sandbox = _TrackedSandbox({3000})  # legacy auto-preview owns 3000
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    assert await mgr._allocate_port() == 5173  # 3000 skipped (sandbox-tracked)


@pytest.mark.asyncio
async def test_allocation_skips_port_with_live_listener() -> None:
    """P1 #2: even an UNtracked but currently-listening curated port is skipped — a real
    owner we don't track would otherwise answer health for a process that EADDRINUSE'd."""
    sandbox = _FakeSandbox()
    sandbox._serving.add(3000)  # a real listener already owns 3000
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    assert await mgr._allocate_port() == 5173  # 3000 skipped (live listener)


class _SocketOccupiedSandbox(_FakeSandbox):
    """Ports can be occupied even when they do not answer HTTP health."""

    def __init__(self, occupied: set[int]) -> None:
        super().__init__()
        self.occupied = set(occupied)

    async def port_owner(self, port: int):  # noqa: ANN201
        if port in self.occupied:
            return _Owner(pid=9000 + port, session="foreign-daemon")
        return _Owner(pid=None, session=None)


@pytest.mark.asyncio
async def test_allocation_skips_socket_occupied_ports_until_free_candidate() -> None:
    """PORT-FIX: allocation must probe in-sandbox socket occupancy, not just HTTP
    health. A non-HTTP listener on the first N candidates is occupied and must be
    skipped; the allocator retries to the next free platform port."""
    sandbox = _SocketOccupiedSandbox({3000, 5173})
    mgr = _mgr(sandbox, port_pool=[3000, 5173, 8080])
    assert await mgr._allocate_port() == 8080


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


@pytest.mark.asyncio
async def test_allocation_reclaims_stale_same_conversation_preview() -> None:
    """PORT-FIX: if a stopped preview from this manager leaked its server, reclaim it
    through the same stop path instead of skipping the port forever."""
    sandbox = _StalePreviewSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    mgr._sessions["old"] = PreviewSession(
        name="old",
        port=3000,
        command="python3 -m http.server 3000 -d dist",
        exec_dir="/workspace",
        intent={},
        status=PreviewStatus.STOPPED,
    )

    assert await mgr._allocate_port() == 3000
    assert sandbox.sessions.kill_calls == ["old"]
    assert sandbox._stale == {}


@pytest.mark.asyncio
async def test_start_coordinates_auto_preview_standdown() -> None:
    """P1 #2: the first manager-owned start stands the legacy auto-preview DOWN (so the
    two can't both claim a curated port). The manager calls `disable_auto_preview` once."""
    calls: list[int] = []

    class _CoordSandbox(_FakeSandbox):
        async def disable_auto_preview(self) -> None:
            calls.append(1)

    sandbox = _CoordSandbox()
    mgr = _mgr(sandbox, port_pool=[3000, 5173])
    await mgr.start(serve_dir="dist", name="app", supervise=False)
    await mgr.start(serve_dir="web", name="web", supervise=False)
    assert calls == [1]  # invoked exactly once, not per-start


# ----------------------------------------------------- P1 #3: static serve_dir path


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


@pytest.mark.asyncio
async def test_static_serve_dir_serves_the_directory_not_dist_dist() -> None:
    """P1 #3: a real index.html under serve_dir is actually fetched (200) — the server
    runs from the workspace with `-d serve_dir`, so the served root is workspace/serve_dir,
    NOT serve_dir/serve_dir (which 404'd while health still falsely read 'answered')."""
    sandbox = _StaticServingSandbox(files={"/workspace/dist/index.html"})
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="dist", name="app", supervise=False)
    assert session.status is PreviewStatus.RUNNING
    assert sandbox.sessions.port_root[3000] == "/workspace/dist"  # not /workspace/dist/dist
    status, _body, _ctype = await sandbox.fetch_inside(3000, "/")
    assert status == 200  # the index is really served (not a 404 on the wrong tree)


@pytest.mark.asyncio
async def test_container_without_host_workspace_discovers_guest_cwd_for_relative_static_dir() -> (
    None
):
    """Production containers intentionally report workspace_path=None.

    A relative serve_dir must still run from the guest workspace, never from that
    same relative directory (which produces release/release).
    """

    sandbox = _StaticServingSandbox(files={"/workspace/release/index.html"})
    sandbox.workspace_path = None  # type: ignore[assignment]

    async def _pwd(command: str, *, timeout_s: int = 5):  # noqa: ANN202, ARG001
        assert command == "pwd"
        return SimpleNamespace(exit_code=0, stdout="/workspace\n", stderr="")

    sandbox.exec_shell = _pwd  # type: ignore[attr-defined,method-assign]
    mgr = _mgr(sandbox, port_pool=[3000])
    session = await mgr.start(serve_dir="release", name="app", supervise=False)

    assert session.status is PreviewStatus.RUNNING
    assert session.exec_dir == "/workspace"
    assert sandbox.sessions.port_root[3000] == "/workspace/release"


@pytest.mark.asyncio
async def test_static_preview_root_404_is_never_marked_health_verified() -> None:
    sandbox = _StaticServingSandbox(files=set())
    mgr = _mgr(sandbox, port_pool=[3000])

    session = await mgr.start(serve_dir="release", name="app", supervise=False)

    assert session.status is PreviewStatus.CRASHED
    assert session.url is None
    assert session.detail == "static preview root did not return a successful HTTP response"


@pytest.mark.asyncio
async def test_default_static_root_404_is_never_marked_health_verified() -> None:
    sandbox = _StaticServingSandbox(files=set())
    mgr = _mgr(sandbox, port_pool=[3000])

    session = await mgr.start(name="app", supervise=False)

    assert session.status is PreviewStatus.CRASHED
    assert session.intent["launch_kind"] == "static"


@pytest.mark.asyncio
async def test_known_dev_framework_uses_liveness_not_static_root_success() -> None:
    sandbox = _FakeSandbox()

    async def _not_found(port: int, path: str, *, timeout_s: int = 5):  # noqa: ARG001
        if port in sandbox._serving:
            return (404, b"framework fallback", "text/plain")
        return None

    sandbox.fetch_inside = _not_found  # type: ignore[method-assign]
    mgr = _mgr(sandbox, port_pool=[3000])

    session = await mgr.start(
        serve_dir="release",
        framework="vite",
        name="app",
        supervise=False,
    )

    assert session.status is PreviewStatus.RUNNING
    assert session.intent["launch_kind"] == "framework"
