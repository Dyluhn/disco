"""P3 tests: NOVNC_PORT in USER_PORTS, live-url route returns 503 when disabled.

P5 live jail acceptance (loopback-bind, per-conv jail, view-only, idle teardown)
is HARDWARE-DEFERRED — VM 201 (the gVisor sandbox host) is destroyed. These
integration tests require a real sandbox backend; run them manually with a live
podman/local backend when re-provisioned.
"""

from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS


def test_novnc_port_in_user_ports():
    """NOVNC_PORT must be in USER_PORTS so host_proxy.py admits noVNC traffic.
    This is the security-critical gate: the proxy 404s any unknown port, so the
    allowlist IS the exposure surface — adding a port adds a network surface."""
    assert NOVNC_PORT in USER_PORTS, (
        f"NOVNC_PORT {NOVNC_PORT} is not in USER_PORTS {USER_PORTS}; "
        "host_proxy.py will 404 all noVNC traffic"
    )


def test_novnc_port_value():
    """NOVNC_PORT is conventionally 6080 (websockify default for noVNC)."""
    assert NOVNC_PORT == 6080


def test_live_url_route_disabled_returns_503():
    """When live_browser.enabled=False in config, GET /browser/live-url → 503."""
    import asyncio
    from unittest.mock import MagicMock

    import httpx
    from disco.agent_server.app import create_app
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.core.store.sqlite import SqliteEventStore

    # Build a minimal config with live_browser DISABLED
    entry = ModelEntry(model_id="m", provider="local", context_window=8192)
    cfg = RouterConfig(
        models={"m": entry},
        default_model="m",
        live_browser=LiveBrowserSettings(enabled=False),
    )

    store = MagicMock(spec=SqliteEventStore)
    runtime = MagicMock()
    runtime._config_store.load.return_value = cfg
    runtime.live_session.return_value = None

    app = create_app(store, runtime=runtime)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            body = resp.json()
            assert body["reason"] == "disabled"

    asyncio.run(run())


def test_live_url_route_no_sandbox_returns_503():
    """When enabled but no sandbox running, GET /browser/live-url → 503 with no_sandbox reason."""
    import asyncio
    from unittest.mock import MagicMock

    import httpx
    from disco.agent_server.app import create_app
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.core.store.sqlite import SqliteEventStore

    entry = ModelEntry(model_id="m", provider="local", context_window=8192)
    cfg = RouterConfig(
        models={"m": entry},
        default_model="m",
        live_browser=LiveBrowserSettings(enabled=True),
    )

    store = MagicMock(spec=SqliteEventStore)
    runtime = MagicMock()
    runtime._config_store.load.return_value = cfg
    runtime.live_session.return_value = None  # no sandbox

    app = create_app(store, runtime=runtime)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            body = resp.json()
            assert body["reason"] == "no_sandbox"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Happy-path + intermediate-failure orchestration (mocked sandbox).
#
# These prove the ROUTE's wiring end-to-end WITHOUT a real sandbox backend
# (the P5 hardware-deferred bit): daemon-health probe → live_start POST →
# wake_for_preview → 200. They mock the SandboxSession's async exec_shell
# (the two curls) and runtime.wake_for_preview (the proxy-url resolver), so
# the orchestration logic in preview.py:browser_live_url is fully exercised.
# ---------------------------------------------------------------------------


def _enabled_app(*, session, upstream):
    """Build an app whose runtime is enabled, returns `session` for live_session,
    and `upstream` (str | None) from the async wake_for_preview."""
    from unittest.mock import AsyncMock, MagicMock

    from disco.agent_server.app import create_app
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.core.store.sqlite import SqliteEventStore

    entry = ModelEntry(model_id="m", provider="local", context_window=8192)
    cfg = RouterConfig(
        models={"m": entry},
        default_model="m",
        live_browser=LiveBrowserSettings(enabled=True),
    )
    store = MagicMock(spec=SqliteEventStore)
    runtime = MagicMock()
    runtime._config_store.load.return_value = cfg
    runtime.live_session.return_value = session
    runtime.wake_for_preview = AsyncMock(return_value=upstream)
    return create_app(store, runtime=runtime)


def _shell_result(exit_code: int, stdout: str = ""):
    from unittest.mock import MagicMock

    res = MagicMock()
    res.exit_code = exit_code
    res.stdout = stdout
    return res


def _fake_session(exec_results: list):
    """A session whose async exec_shell yields `exec_results` in order."""
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()
    session.exec_shell = AsyncMock(side_effect=exec_results)
    return session


def test_live_url_route_happy_path_returns_200_and_NO_raw_url():
    """enabled + sandbox + healthy daemon + live_start ok + upstream-ready → 200 with
    {ready, port, novnc_path} and CRUCIALLY no raw sandbox host:port URL (the BLOCK fix:
    a raw URL would bypass the cid-scoped auth proxy). Proves the full orchestration."""
    import asyncio

    import httpx

    # exec_shell #1 = health curl (ok), #2 = live_start POST (ok JSON)
    session = _fake_session(
        [
            _shell_result(0, "ok"),
            _shell_result(0, '{"ok": true, "novnc_port": 6080, "display": ":1"}'),
        ]
    )
    # wake_for_preview resolves a RAW upstream (the readiness signal); the route must
    # NOT leak it to the browser.
    raw = "http://192.168.1.77:49213"
    app = _enabled_app(session=session, upstream=raw)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ready"] is True
            assert body["port"] == 6080
            # view_only=1 is enforced in the server-returned path, not just the client.
            assert "view_only=1" in body["novnc_path"]
            # SECURITY: the raw sandbox host:port must never reach the browser.
            assert "url" not in body
            assert raw not in resp.text

    asyncio.run(run())


def test_live_stop_route_is_idempotent_200():
    """POST /browser/live-stop → 200 ok even with no sandbox (teardown is best-effort;
    a no-op is success, not an error). Proves the close→teardown path exists server-side."""
    import asyncio

    import httpx

    app = _enabled_app(session=None, upstream=None)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-stop")
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True

    asyncio.run(run())


def test_live_touch_route_is_best_effort_200():
    """POST /browser/live-touch (the open-pane heartbeat) → 200 ok, even with no
    sandbox; it refreshes the idle watchdog so an active view is not reaped."""
    import asyncio

    import httpx

    # With a live session, the touch curl is issued; with none, it's a no-op 200.
    session = _fake_session([_shell_result(0, '{"ok": true, "live": true}')])
    app = _enabled_app(session=session, upstream=None)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-touch")
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True

    asyncio.run(run())


def test_port_upstream_gates_novnc_when_disabled():
    """BLOCKER-2 gate: PreviewService.port_upstream must refuse NOVNC_PORT when the
    live-browser feature is disabled — even if a stack is listening — so disabling Live
    closes the proxy surface, not just the button. A normal USER port is unaffected."""
    from unittest.mock import MagicMock

    from disco.agent_server.preview_service import PreviewService
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.tools.sandbox._container import NOVNC_PORT

    entry = ModelEntry(model_id="m", provider="local", context_window=8192)

    def _svc(enabled: bool) -> PreviewService:
        cfg = RouterConfig(
            models={"m": entry},
            default_model="m",
            live_browser=LiveBrowserSettings(enabled=enabled),
        )
        rt = MagicMock()
        rt._config_store.load.return_value = cfg
        # A live session whose expose_port would otherwise hand back a URL.
        sess = MagicMock()
        sess._service.name = "gvisor"
        sess.expose_port.return_value = "http://host:40000"
        executor = MagicMock()
        executor._sandbox = sess
        rt._executors = {"conv_aabbccdd11223344": executor}
        return PreviewService(rt)

    # Disabled → NOVNC_PORT refused (None), even though expose_port would resolve.
    assert _svc(False).port_upstream("conv_aabbccdd11223344", NOVNC_PORT) is None
    # Enabled → NOVNC_PORT resolves normally.
    assert _svc(True).port_upstream("conv_aabbccdd11223344", NOVNC_PORT) == "http://host:40000"
    # A non-noVNC USER port is never gated by the live-browser flag.
    assert _svc(False).port_upstream("conv_aabbccdd11223344", 5173) == "http://host:40000"


def test_port_upstream_gates_novnc_on_unsupported_backend_even_if_enabled():
    """codex-P1: the NOVNC_PORT proxy gate must require BOTH the feature enabled AND a
    live-view-capable session (supports_live_view ⇐ LIVE_VIEW_BACKENDS), not just the
    persisted flag. The exploit it closes: a user enables Live on gVisor (enabled=true
    persisted), then SWITCHES the sandbox backend to local/podman. The stale enabled flag
    would otherwise hold the noVNC port OPEN on a backend that can't (and shouldn't) serve
    it. A normal USER port stays unaffected by the live capability."""
    from unittest.mock import MagicMock

    from disco.agent_server.preview_service import PreviewService
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.tools.sandbox._container import NOVNC_PORT

    entry = ModelEntry(model_id="m", provider="local", context_window=8192)

    def _svc(*, supports_live_view: bool) -> PreviewService:
        # enabled stays TRUE the whole time (the stale persisted flag after a backend switch).
        cfg = RouterConfig(
            models={"m": entry},
            default_model="m",
            live_browser=LiveBrowserSettings(enabled=True),
        )
        rt = MagicMock()
        rt._config_store.load.return_value = cfg
        sess = MagicMock()
        # a non-gVisor backend that does NOT support live view (e.g. switched to local)
        sess._service.name = "local"
        sess.supports_live_view = supports_live_view
        sess.expose_port.return_value = "http://host:40000"
        executor = MagicMock()
        executor._sandbox = sess
        rt._executors = {"conv_aabbccdd11223344": executor}
        return PreviewService(rt)

    # enabled=True but the session can't stream → noVNC port stays CLOSED (the fix).
    svc_unsupported = _svc(supports_live_view=False)
    assert svc_unsupported.port_upstream("conv_aabbccdd11223344", NOVNC_PORT) is None
    # …yet a normal dev-server USER port is unaffected by the live-view capability.
    assert svc_unsupported.port_upstream("conv_aabbccdd11223344", 5173) == "http://host:40000"
    # A live-view-capable session (gVisor) + enabled → noVNC port resolves normally.
    assert (
        _svc(supports_live_view=True).port_upstream("conv_aabbccdd11223344", NOVNC_PORT)
        == "http://host:40000"
    )


def test_live_url_route_daemon_down_returns_503():
    """enabled + sandbox but the browser daemon health curl fails → 503 no_daemon
    (no false 'live' affordance when the agent never started the browser tool)."""
    import asyncio

    import httpx

    session = _fake_session([_shell_result(7, "")])  # curl exit 7 = connection refused
    app = _enabled_app(session=session, upstream="unused")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            assert resp.json()["reason"] == "no_daemon"

    asyncio.run(run())


def test_live_ready_route_disabled_no_side_effect():
    """W-47: GET /browser/live-ready returns {ready:False, reason:'disabled'} with a 200
    (a poll must never error-spam) and triggers NO live_start / wake_for_preview."""
    import asyncio

    import httpx

    app = _enabled_app(session=None, upstream="unused")  # session None → no_sandbox path
    # Override the config to disabled via the same runtime mock the helper built.
    # Simpler: build a fresh disabled app inline.
    from unittest.mock import MagicMock

    from disco.agent_server.app import create_app
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.core.store.sqlite import SqliteEventStore

    entry = ModelEntry(model_id="m", provider="local", context_window=8192)
    cfg = RouterConfig(
        models={"m": entry}, default_model="m", live_browser=LiveBrowserSettings(enabled=False)
    )
    store = MagicMock(spec=SqliteEventStore)
    runtime = MagicMock()
    runtime._config_store.load.return_value = cfg
    app = create_app(store, runtime=runtime)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-ready")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ready"] is False
            assert body["reason"] == "disabled"

    asyncio.run(run())
    # live_session is never even reached for the disabled case; wake_for_preview not called.
    runtime.wake_for_preview.assert_not_called()


def test_live_ready_route_no_sandbox_returns_not_ready():
    """W-47: enabled but no sandbox → {ready:False, reason:'no_sandbox'}, still 200."""
    import asyncio

    import httpx

    app = _enabled_app(session=None, upstream="unused")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-ready")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ready"] is False
            assert body["reason"] == "no_sandbox"

    asyncio.run(run())


def test_live_ready_route_healthy_daemon_is_ready_with_NO_live_start():
    """W-47 KEY INVARIANT: a healthy daemon → {ready:True}. The probe issues ONLY the
    read-only /health curl — NEVER the live_start POST and NEVER wake_for_preview (the
    side-effects that make /browser/live-url unpollable). Built inline so we can hold the
    runtime mock and assert wake_for_preview was never awaited."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import httpx
    from disco.agent_server.app import create_app
    from disco.core.llm import ModelEntry
    from disco.core.llm.config import LiveBrowserSettings, RouterConfig
    from disco.core.store.sqlite import SqliteEventStore

    # A session whose exec_shell would yield a live_start JSON if (wrongly) called twice.
    session = _fake_session(
        [
            _shell_result(0, "OK"),  # the health curl
            _shell_result(0, '{"ok": true}'),  # MUST NOT be consumed (no live_start)
        ]
    )

    entry = ModelEntry(model_id="m", provider="local", context_window=8192)
    cfg = RouterConfig(
        models={"m": entry}, default_model="m", live_browser=LiveBrowserSettings(enabled=True)
    )
    store = MagicMock(spec=SqliteEventStore)
    runtime = MagicMock()
    runtime._config_store.load.return_value = cfg
    runtime.live_session.return_value = session
    runtime.wake_for_preview = AsyncMock(return_value="http://192.168.1.77:49213")
    app = create_app(store, runtime=runtime)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-ready")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ready"] is True
            assert body["reason"] == "ready"

    asyncio.run(run())

    # Exactly ONE exec_shell (the /health probe) — NOT the second live_start curl.
    assert session.exec_shell.await_count == 1
    only_call = session.exec_shell.await_args_list[0]
    assert "/health" in only_call.args[0]
    # The side-effecting port-expose was NEVER triggered by the readiness probe.
    runtime.wake_for_preview.assert_not_called()


def test_live_ready_route_daemon_down_not_ready():
    """W-47: enabled + sandbox but the /health curl fails → {ready:False, no_daemon}."""
    import asyncio

    import httpx

    session = _fake_session([_shell_result(7, "")])  # curl exit 7 = connection refused
    app = _enabled_app(session=session, upstream="unused")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-ready")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ready"] is False
            assert body["reason"] == "no_daemon"

    asyncio.run(run())


def test_live_ready_unsupported_backend_not_ready():
    """HONESTY GATE: a sandbox whose backend can't run the stack (supports_live_view
    False — e.g. the process/local/podman backend) reports {ready:False,
    reason:'unsupported_backend'} and NEVER probes the daemon. This is what stops the
    frontend from auto-starting a doomed stack (the old live_start_failed bug)."""
    import asyncio

    import httpx

    # A session that WOULD answer /health, but the backend can't stream.
    session = _fake_session([_shell_result(0, "OK")])
    session.supports_live_view = False
    app = _enabled_app(session=session, upstream="unused")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-ready")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ready"] is False
            assert body["reason"] == "unsupported_backend"

    asyncio.run(run())
    # The daemon /health probe is NEVER reached on an unsupported backend.
    assert session.exec_shell.await_count == 0


def test_live_url_unsupported_backend_returns_503():
    """Defense in depth: even a direct GET /browser/live-url on an unsupported backend
    returns 503 unsupported_backend BEFORE any daemon health / live_start curl — so a
    stale frontend can never coax a doomed stack into a 'Failed to start' error."""
    import asyncio

    import httpx

    session = _fake_session([_shell_result(0, "OK"), _shell_result(0, '{"ok": true}')])
    session.supports_live_view = False
    app = _enabled_app(session=session, upstream="http://192.168.1.77:49213")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            assert resp.json()["reason"] == "unsupported_backend"

    asyncio.run(run())
    # No daemon health probe / live_start curl was issued.
    assert session.exec_shell.await_count == 0


def test_session_supports_live_view_only_gvisor():
    """SandboxSession.supports_live_view sources the ONE LIVE_VIEW_BACKENDS set: only the
    gVisor backend can stream; process/local/podman cannot — so the Settings gate and the
    runtime live-ready can't drift."""
    from types import SimpleNamespace

    from disco.tools.sandbox._container import LIVE_VIEW_BACKENDS
    from disco.tools.sandbox.session import SandboxSession

    assert LIVE_VIEW_BACKENDS == frozenset({"gvisor"})
    cases = (("gvisor", True), ("local", False), ("process", False), ("podman", False))
    for name, expected in cases:
        sess = SandboxSession.__new__(SandboxSession)
        sess._service = SimpleNamespace(name=name)
        assert sess.supports_live_view is expected, name


def test_live_url_route_no_upstream_returns_503():
    """Daemon brought the stack up but the proxy hasn't exposed NOVNC_PORT yet →
    503 no_upstream rather than a 200 pointing at a dead URL (no false affordance)."""
    import asyncio

    import httpx

    session = _fake_session(
        [
            _shell_result(0, "ok"),
            _shell_result(0, '{"ok": true}'),
        ]
    )
    app = _enabled_app(session=session, upstream=None)  # wake_for_preview → None

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            assert resp.json()["reason"] == "no_upstream"

    asyncio.run(run())
