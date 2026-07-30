"""EPIC F — PreviewManager: the platform owns ports/serving/health; the model never
picks a port. These tests prove the north star at the API level (no port can be
supplied or overridden), plus start→URL, status/logs/stop, restart-on-crash, distinct
ports, and graceful URL degradation.
"""

from __future__ import annotations

import inspect
import socket
from types import SimpleNamespace

import pytest
from _preview_manager_fakes import (  # noqa: E402
    _DependencySession,
    _FakeSandbox,
    _FakeSessions,
    _ForeignNamespaceOwnerSandbox,
    _ForeignOwnerSandbox,
    _HostExecSandbox,
    _LifecycleMutationSession,
    _manager_fixture,
    _manufacture_server_side_time_wait,
    _mgr,
    _NeverReadSession,
    _OurOwnerSandbox,
    _sealed_contract_with_cwd,
    _SocketOccupiedSandbox,
    _StalePreviewSandbox,
    _StaticServingSandbox,
    _TrackedSandbox,
    _UnattributedSharedSandbox,
    _UnlockedSession,
    _ViteSessions,
)
from disco.agent_server.preview_manager import (
    NoPreviewPortAvailableError,
    PreviewManager,
    PreviewReloadStrategy,
    PreviewStatus,
    preview_requires_node_dependencies,
)
from disco.agent_server.preview_projection import (
    ActiveLivePreviewProjection,
)
from disco.agent_server.preview_service import PreviewService

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

    session = _DependencySession()
    contract = SimpleNamespace(command=None, framework="vite", cwd=None)

    dependency_dir = await PreviewService._prepare_sealed_node_dependencies(session, contract)

    assert dependency_dir == "node_modules"
    assert session.commands == ["npm ci --no-audit --no-fund"]


@pytest.mark.asyncio
async def test_sealed_node_dependency_restore_refuses_unlocked_graph() -> None:

    with pytest.raises(RuntimeError, match="exactly one supported immutable lockfile"):
        await PreviewService._prepare_sealed_node_dependencies(
            _UnlockedSession(),
            SimpleNamespace(command=None, framework="vite", cwd=None),
        )


@pytest.mark.asyncio
async def test_sealed_node_dependency_restore_honors_workspace_relative_cwd() -> None:

    session = _DependencySession("web/")
    dependency_dir = await PreviewService._prepare_sealed_node_dependencies(
        session,
        SimpleNamespace(command=None, framework="vite", cwd="/workspace/web"),
    )

    assert dependency_dir == "web/node_modules"
    assert session.commands == ["npm --prefix ./web ci --no-audit --no-fund"]


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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173, 8080])
    assert await mgr._allocate_port() == 3000  # first of the platform pool


@pytest.mark.asyncio
async def test_start_allocates_url_from_platform() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    session = await mgr.start(command="npm run dev --port 9999", supervise=False)
    assert session.port == 3000  # platform pool, NOT 9999
    assert "9999" not in session.command  # the model's port was scrubbed
    assert "PORT=3000" in session.command  # platform port injected
    assert session.url == "http://preview.test/3000/"


@pytest.mark.asyncio
async def test_framework_adapter_binds_explicit_vite_script_to_platform_port() -> None:
    """A project-specific script does not demote a declared runtime to PORT-only.

    Vite deliberately ignores the generic PORT environment variable.  The configured
    adapter must therefore add Vite's own CLI binding and retain framework/HMR
    authority; otherwise the process serves 5173 while canonical Preview owns 3000.
    """

    sandbox = _FakeSandbox()
    sandbox.sessions = _ViteSessions(sandbox._serving)
    mgr = _mgr(sandbox, port_pool=[3000])

    session = await mgr.start(
        command="npm run dev",
        framework="vite",
        supervise=False,
    )

    assert session.status is PreviewStatus.RUNNING
    assert session.port == 3000
    assert session.intent["launch_kind"] == "framework"
    assert session.reload_strategy is PreviewReloadStrategy.HMR
    assert session.command == ("PORT=3000 npm run dev -- --port 3000 --host 0.0.0.0 --strictPort")
    assert 5173 not in sandbox._serving


@pytest.mark.parametrize(
    ("framework", "binding"),
    (
        ("next", "-- -p 3000"),
        ("nextjs", "-- -p 3000"),
        ("astro", "-- --port 3000 --host 0.0.0.0"),
        ("svelte", "-- --port 3000 --host 0.0.0.0"),
    ),
)
@pytest.mark.asyncio
async def test_configured_runtime_adapts_project_script(
    framework: str,
    binding: str,
) -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000])

    session = await mgr.start(
        command="npm run develop",
        framework=framework,
        supervise=False,
    )

    assert session.command == f"PORT=3000 npm run develop {binding}"
    assert session.intent["launch_kind"] == "framework"


@pytest.mark.asyncio
async def test_custom_and_unknown_runtime_commands_remain_flexible() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])

    custom = await mgr.start(
        command="python3 server.py",
        name="custom",
        supervise=False,
    )
    future = await mgr.start(
        command="future-preview serve",
        framework="future-native-webview",
        name="future",
        supervise=False,
    )

    assert custom.command == "PORT=3000 python3 server.py"
    assert custom.intent["launch_kind"] == "custom"
    assert future.command == "PORT=5173 future-preview serve"
    assert future.intent["launch_kind"] == "custom"
    assert "--port" not in future.command


@pytest.mark.asyncio
async def test_explicit_framework_port_placeholder_is_not_duplicated() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000])

    session = await mgr.start(
        command="npm run dev -- --port {port}",
        framework="vite",
        supervise=False,
    )

    assert session.command.count("--port") == 1
    assert "--port 3000" in session.command
    assert session.intent["launch_kind"] == "framework"


@pytest.mark.asyncio
async def test_two_previews_get_distinct_ports_without_model_choosing() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173, 8080])
    a = await mgr.start(serve_dir="api", name="api", supervise=False)
    b = await mgr.start(serve_dir="web", name="web", supervise=False)
    assert a.port != b.port
    assert {a.port, b.port} == {3000, 5173}
    # neither call supplied a port; the platform handed out both
    assert a.url and b.url and a.url != b.url


@pytest.mark.asyncio
async def test_restart_canonical_reuses_exact_stored_intent() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    original = await mgr.start(command="node server.js", name="preview", supervise=False)
    original_intent = dict(original.intent)
    original_projection = original.projection_id

    assert await mgr.stop("preview") == ["preview"]
    restarted = await mgr.restart_canonical()

    assert restarted is not None
    assert restarted.status is PreviewStatus.RUNNING
    assert restarted.intent == original_intent
    assert restarted.projection_id != original_projection
    # Every (re)launch binds the generation's exec_dir explicitly: the reused
    # tmux shell keeps the PRIOR generation's cwd otherwise (seed 440025).
    executed = sandbox.sessions.exec_calls[-1][1]
    assert executed.endswith(restarted.command)
    if restarted.exec_dir:
        assert executed.startswith(f"cd {restarted.exec_dir}") or executed.startswith("cd '")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [PreviewStatus.STARTING, PreviewStatus.RESTARTING])
async def test_restart_canonical_recovers_dead_transitional_process(
    state: PreviewStatus,
) -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    assert sandbox.sessions.exec_calls == [
        ("web", "cd /workspace && PORT=3000 python3 server.py", "/workspace")
    ]
    assert await mgr.resolve_sealed_contract(contract) is restored
    await mgr.aclose()


@pytest.mark.asyncio
async def test_sealed_binding_fails_closed_after_intent_or_sandbox_identity_changes() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    first = await mgr.start(serve_dir="dist", name="app", supervise=False)
    second = await mgr.start(serve_dir="dist", name="app", supervise=False)
    assert first is second
    assert first.port == second.port
    assert len(mgr.list()) == 1  # not duplicated
    assert len(sandbox.sessions.exec_calls) == 1  # not re-launched


@pytest.mark.asyncio
async def test_active_projection_matches_only_the_original_live_generation() -> None:
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173, 8080])

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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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

    sandbox, mgr = _manager_fixture(port_pool=[3000])

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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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

    sandbox, mgr = _manager_fixture(port_pool=[3000])

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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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

    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000])
    await mgr.start(serve_dir="a", name="a", supervise=False)
    with pytest.raises(NoPreviewPortAvailableError):
        await mgr.start(serve_dir="b", name="b", supervise=False)


def test_shared_host_pool_scales_beyond_framework_defaults_and_excludes_controls() -> None:
    """Process previews get a broad host-only pool; containers retain published ports."""
    from disco.agent_server.preview_manager import _default_port_pool
    from disco.core.loop.preview_target import is_managed_host_preview_port

    shared = _default_port_pool(_FakeSandbox(backend_name="process"))
    assert 8000 not in shared and 5173 not in shared
    assert len(shared) > 4
    assert any(is_managed_host_preview_port(port) for port in shared)
    isolated = _default_port_pool(_FakeSandbox(backend_name="gvisor"))
    assert 8000 in isolated  # inside an isolated box 8000 is the box's own
    assert not any(is_managed_host_preview_port(port) for port in isolated)


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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=command, supervise=False)
    assert mgr.list() == []  # nothing registered; no port leaked


@pytest.mark.asyncio
async def test_raw_command_port_placeholder_is_filled_with_platform_port() -> None:
    """P1 #1 escape hatch: a raw command may declare WHERE the port goes with the literal
    `{port}` placeholder — the platform fills it with ITS allocated port (never the
    model's), so positional-port servers stay platform-owned."""
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=command, supervise=False)
    assert mgr.list() == []  # nothing registered; no port leaked, 4321 never bound


@pytest.mark.asyncio
async def test_positional_port_after_flags_is_rejected() -> None:
    """The half of the smuggle the flag-scrub regex missed on its own: a port sitting
    positionally AFTER flags (`http.server --bind 0.0.0.0 4321 -d dist`). shlex tokenizing
    sees 4321 as a bare positional port (not a flag value) and rejects it."""
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    cmd = "python3 -m http.server --bind 0.0.0.0 4321 -d dist"
    with pytest.raises(PreviewCommandError):
        await mgr.start(command=cmd, supervise=False)
    assert mgr.list() == []


@pytest.mark.asyncio
async def test_clean_single_foreground_placeholder_command_works() -> None:
    """The sanctioned form passes untouched: one foreground command with `{port}`."""
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    session = await mgr.start(command="python3 -m http.server {port} -d dist", supervise=False)
    assert session.status is PreviewStatus.RUNNING
    assert session.port == 3000
    assert "http.server 3000" in session.command and "{port}" not in session.command


@pytest.mark.asyncio
async def test_numeric_flag_value_is_not_misread_as_port() -> None:
    """A numeric token following a flag is that flag's VALUE, not a port: a legit
    `uvicorn app:app --port {port} --workers 4` is accepted (4 is `--workers`'s value),
    and the platform port is placed via the placeholder."""
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
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
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    session = await mgr.start(command=command, supervise=False)
    assert session.port == 3000
    assert expect in session.command  # placeholder filled with the platform port
    assert "{port}" not in session.command
    assert session.status is PreviewStatus.RUNNING


@pytest.mark.asyncio
async def test_quoted_concrete_port_with_workers_keeps_workers_drops_port() -> None:
    """A real mixed command: a quoted concrete `--port` is dropped while a legit
    `--workers N` survives — proving the scrub is flag-specific, not a blanket digit purge."""
    sandbox, mgr = _manager_fixture(port_pool=[3000, 5173])
    session = await mgr.start(command='uvicorn app:app --port "8000" --workers 4', supervise=False)
    assert session.port == 3000
    assert "8000" not in session.command
    assert "--workers 4" in session.command  # non-port numeric arg untouched
    assert "PORT=3000" in session.command
    assert session.status is PreviewStatus.RUNNING


# ---------------------------------------------- P1 #1/#2: post-launch port ownership


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


@pytest.mark.asyncio
async def test_shared_host_unattributed_listener_cannot_certify_preview() -> None:
    """A host PID with no conversation session is foreign until proven otherwise."""

    sandbox = _UnattributedSharedSandbox()
    sandbox._serving.add(39011)
    mgr = _mgr(sandbox, port_pool=[39011])
    session = PreviewSession(
        name="app",
        port=39011,
        command="python3 -m http.server 39011 -d dist",
        exec_dir="/workspace",
        intent={},
        _supervise=False,
    )
    mgr._sessions["app"] = session

    await mgr._launch(session)

    assert session.status is PreviewStatus.CRASHED
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


@pytest.mark.asyncio
async def test_allocation_skips_socket_occupied_ports_until_free_candidate() -> None:
    """PORT-FIX: allocation must probe in-sandbox socket occupancy, not just HTTP
    health. A non-HTTP listener on the first N candidates is occupied and must be
    skipped; the allocator retries to the next free platform port."""
    sandbox = _SocketOccupiedSandbox({3000, 5173})
    mgr = _mgr(sandbox, port_pool=[3000, 5173, 8080])
    assert await mgr._allocate_port() == 8080


@pytest.mark.asyncio
async def test_allocation_scans_past_twenty_busy_candidates() -> None:
    """A broad pool's capacity is not silently truncated by an allocation retry cap."""

    ports = list(range(39020, 39046))
    sandbox = _SocketOccupiedSandbox(set(ports[:-1]))
    mgr = _mgr(sandbox, port_pool=ports)
    assert await mgr._allocate_port() == ports[-1]


@pytest.mark.asyncio
async def test_default_shared_host_pool_supports_more_than_four_concurrent_managers() -> None:
    """Independent conversations retain one kernel lease each beyond the old ceiling."""

    managers: list[PreviewManager] = []
    sessions = []
    try:
        for index in range(6):
            sandbox = _FakeSandbox(backend_name="process")
            sandbox.conversation_id = f"conv_capacity_{index}"
            sandbox.shares_host_network = True
            manager = _mgr(sandbox)
            managers.append(manager)
            sessions.append(
                await manager.start(
                    serve_dir="dist",
                    name=f"preview-{index}",
                    supervise=False,
                )
            )

        assert len({session.port for session in sessions}) == 6
        assert all(session.status is PreviewStatus.RUNNING for session in sessions)
    finally:
        for manager in managers:
            await manager.aclose()


@pytest.mark.asyncio
async def test_allocation_accepts_port_with_only_time_wait_ghosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,  # noqa: ANN001
) -> None:
    """Seed 405210's earliest broken link: a just-torn-down sibling preview leaves
    server-side TIME_WAIT sockets for ~60s. No listener exists (server_status:
    FREE) and every server the platform launches binds with SO_REUSEADDR, so
    allocation must accept the port instead of reporting the pool exhausted."""
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    port = _manufacture_server_side_time_wait()
    mgr = _mgr(_HostExecSandbox(), port_pool=[port])
    try:
        assert await mgr._allocate_port() == port
    finally:
        await mgr.aclose()


@pytest.mark.asyncio
async def test_allocation_still_rejects_live_listener_unseen_by_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,  # noqa: ANN001
) -> None:
    """Positive control for the TIME_WAIT rule: SO_REUSEADDR never permits binding
    over a LIVE listener, so a real foreign server still disqualifies the port
    even when the ownership probe cannot attribute it."""
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    mgr = _mgr(_HostExecSandbox(), port_pool=[port])
    try:
        with pytest.raises(NoPreviewPortAvailableError):
            await mgr._allocate_port()
    finally:
        await mgr.aclose()
        srv.close()


@pytest.mark.asyncio
async def test_shared_host_port_lease_closes_cross_process_allocation_race() -> None:
    """Independent managers cannot both reserve the same apparently-free host port."""

    first_sandbox = _FakeSandbox(backend_name="process")
    first_sandbox.shares_host_network = True
    second_sandbox = _FakeSandbox(backend_name="process")
    second_sandbox.shares_host_network = True
    first = _mgr(first_sandbox, port_pool=[39001, 39002])
    second = _mgr(second_sandbox, port_pool=[39001, 39002])

    first_session = await first.start(serve_dir="dist", name="first", supervise=False)
    second_session = await second.start(serve_dir="dist", name="second", supervise=False)

    assert first_session.port == 39001
    assert second_session.port == 39002
    await first.stop("first")

    third_sandbox = _FakeSandbox(backend_name="process")
    third_sandbox.shares_host_network = True
    third = _mgr(third_sandbox, port_pool=[39001, 39002])
    third_session = await third.start(serve_dir="dist", name="third", supervise=False)
    assert third_session.port == 39001

    await second.aclose()
    await third.aclose()


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


@pytest.mark.asyncio
async def test_every_launch_binds_exec_dir_against_stale_shell_cwd() -> None:
    """Counted seed 440025: the preview tmux shell survives generations and keeps
    the PRIOR generation's cwd — even after the model legitimately deleted that
    directory (project moved from react-app/ to the workspace root); node then
    dies instantly on uv_cwd ENOENT for every relaunch, including corrected
    commands. Every (re)launch must therefore bind the session's exec_dir
    explicitly in the executed command (an absolute cd succeeds regardless of
    the shell's current, possibly deleted, directory)."""
    sandbox = _FakeSandbox()
    mgr = _mgr(sandbox)
    session = await mgr.start(serve_dir="dist", name="web", supervise=False)

    name, executed, exec_dir = sandbox.sessions.exec_calls[-1]
    assert name == "web"
    assert exec_dir == session.exec_dir
    assert session.exec_dir, "the launch workspace must resolve to a concrete directory"
    assert executed.startswith("cd "), executed
    assert session.exec_dir in executed
    assert executed.endswith(session.command)

    # The stopped-session relaunch (a NEW generation into the SAME tmux name)
    # binds the exec_dir again — the exact path that regressed live.
    await mgr.stop("web")
    relaunched = await mgr.start(serve_dir="dist", name="web", supervise=False)
    name2, executed2, _ = sandbox.sessions.exec_calls[-1]
    assert name2 == "web"
    assert executed2.startswith("cd "), executed2
    assert executed2.endswith(relaunched.command)
