"""P3 tests: NOVNC_PORT in USER_PORTS, live-url route returns 503 when disabled.

P5 live jail acceptance (loopback-bind, per-conv jail, view-only, idle teardown)
is HARDWARE-DEFERRED — VM 201 (the gVisor sandbox host) is destroyed. These
integration tests require a real sandbox backend; run them manually with a live
podman/local backend when re-provisioned.
"""
import pytest
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
    import json
    from unittest.mock import MagicMock
    import httpx
    from disco.core.store.sqlite import SqliteEventStore
    from disco.agent_server.app import create_app
    from disco.core.llm.config import RouterConfig, LiveBrowserSettings
    from disco.core.llm import ModelEntry

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
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            body = resp.json()
            assert body["reason"] == "disabled"

    asyncio.run(run())


def test_live_url_route_no_sandbox_returns_503():
    """When enabled but no sandbox running, GET /browser/live-url → 503 with no_sandbox reason."""
    import asyncio
    import json
    from unittest.mock import MagicMock
    import httpx
    from disco.core.store.sqlite import SqliteEventStore
    from disco.agent_server.app import create_app
    from disco.core.llm.config import RouterConfig, LiveBrowserSettings
    from disco.core.llm import ModelEntry

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
            resp = await client.get("/conversations/conv_aabbccdd11223344/browser/live-url")
            assert resp.status_code == 503
            body = resp.json()
            assert body["reason"] == "no_sandbox"

    asyncio.run(run())
