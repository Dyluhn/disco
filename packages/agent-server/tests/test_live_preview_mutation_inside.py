"""Canonical LIVE HTTP dispatch preserves identity, cookies, and single writes."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from disco.agent_server.host_proxy import HostPreviewProxyMiddleware
from disco.agent_server.host_proxy_parts import _live_http
from disco.agent_server.preview_manager import PreviewManager
from disco.agent_server.preview_models import PreviewSession, PreviewStatus
from disco.core.auth import (
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_COOKIE,
    AuthSession,
    PreviewCapabilitySigner,
)
from disco.core.store.sqlite import SqliteEventStore
from starlette.applications import Starlette


@pytest.fixture
async def live(monkeypatch):
    store = SqliteEventStore(":memory:")
    authority = "live:" + "c" * 64
    sandbox = SimpleNamespace(id="sandbox-one", generation=1, shares_host_network=False)
    manager = PreviewManager(sandbox, port_pool=[8000])
    sandbox._preview_manager = manager
    selected = PreviewSession(
        name="web",
        port=8000,
        command="python app.py 8000",
        exec_dir="/workspace",
        intent={},
        status=PreviewStatus.RUNNING,
        sandbox_instance_id=sandbox.id,
        sandbox_generation=1,
    )
    manager._sessions["web"] = selected
    manager._resource._selection_order.append("web")
    state = SimpleNamespace(
        authority=authority, sandbox=sandbox, manager=manager, selected=selected
    )
    signer = PreviewCapabilitySigner(redemption_store=store)
    intent = signer.mint_intent(
        session=AuthSession("owner-a", "csrf", "s", 2**31),
        conversation_id="conv_aaaaaaaafull",
        port=8000,
        target_path="/",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
        authority_id=authority,
    )
    redeemed = signer.redeem_intent(
        intent,
        cid8="aaaaaaaa",
        port=8000,
        path_scope="host",
        partitioned_cookies=True,
    )
    assert redeemed is not None
    state.token = redeemed[0]
    state.inside = AsyncMock(
        return_value=(
            201,
            [
                ("Content-Type", "application/json"),
                ("Set-Cookie", "app_session=accepted; HttpOnly; SameSite=Strict; Path=/"),
                ("Set-Cookie", "second=two; Path=/"),
                ("Set-Cookie", f"{PREVIEW_COOKIE}=spoofed; Path=/"),
                ("Connection", "X-Private"),
                ("X-Private", "hidden"),
            ],
            b'{"ok":true}',
        )
    )
    monkeypatch.setattr(_live_http, "request_inside", state.inside)
    state.upstream = AsyncMock(return_value="http://127.0.0.1:1")
    state.resolver = AsyncMock(side_effect=lambda cid, owner: state.sandbox)
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=state.upstream,
        session_resolver=state.resolver,
        require_capability=True,
        redemption_store=store,
        canonical_authority_resolver=lambda *_: state.authority,
    )
    state.headers = {
        "Cookie": f"{PREVIEW_COOKIE}={state.token}; app_session=prior",
        "Origin": "http://p2-aaaaaaaa-8000.localhost",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://p2-aaaaaaaa-8000.localhost"
    ) as client:
        state.client = client
        state.app = app
        yield state
    store.close()


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
async def test_live_mutation_uses_single_in_session_dispatch(live, method):
    response = await live.client.request(
        method,
        "/auth/login?next=%2F",
        headers=live.headers,
        content=b"credentials" if method == "POST" else b"",
    )
    assert response.status_code == 201
    live.inside.assert_awaited_once()
    live.upstream.assert_not_awaited()
    args = live.inside.call_args.args
    assert args[:4] == (live.sandbox, 8000, method, "/auth/login?next=%2F")
    assert ("cookie", "app_session=prior") in [(k.lower(), v) for k, v in args[4]]
    assert all(PREVIEW_COOKIE not in value for _, value in args[4])
    assert all(key.lower() != "content-length" for key, _ in args[4])
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2 and all("Partitioned" in cookie for cookie in cookies)
    assert "HttpOnly" in cookies[0] and "SameSite=None" in cookies[0]
    assert "x-private" not in response.headers
    assert "no-store" in response.headers["cache-control"]
    assert live.resolver.await_args_list[0].args == ("aaaaaaaa", "owner-a")


@pytest.mark.parametrize(
    "failure",
    ["missing", "manager", "foreign", "port", "instance", "generation", "stopped", "crashed"],
)
async def test_live_unbound_target_never_dispatches(live, failure):
    if failure == "missing":
        live.sandbox = None
    elif failure == "manager":
        live.sandbox._preview_manager = None
    elif failure == "foreign":
        live.manager._resource._sandbox = object()
    elif failure == "port":
        live.selected.port = 8001
    elif failure == "instance":
        live.selected.sandbox_instance_id = "other"
    elif failure == "generation":
        live.sandbox.generation += 1
    else:
        live.selected.status = PreviewStatus(failure)
    response = await live.client.post("/auth/login", headers=live.headers)
    assert response.status_code == 503
    live.inside.assert_not_awaited()
    live.upstream.assert_not_awaited()


@pytest.mark.parametrize("failure", ["authority", "generation", "selection", "detached"])
async def test_live_binding_is_rechecked_after_request_body(live, failure):
    async def body():
        yield b"first"
        if failure == "authority":
            live.authority = "live:" + "d" * 64
        elif failure == "generation":
            live.sandbox.generation += 1
        elif failure == "selection":
            live.manager._sessions["web"] = replace(live.selected, projection_id="pv_new")
        else:
            live.sandbox = None
        yield b"last"

    response = await live.client.post("/auth/login", headers=live.headers, content=body())
    assert response.status_code == 409
    live.inside.assert_not_awaited()
    live.upstream.assert_not_awaited()


@pytest.mark.parametrize("failure", ["unknown", "exception"])
async def test_live_ambiguous_mutation_never_retries(live, failure):
    if failure == "exception":
        live.inside.side_effect = TimeoutError("outcome unknown")
    else:
        live.inside.return_value = None
    response = await live.client.post("/auth/login", headers=live.headers)
    assert response.status_code == 503
    live.inside.assert_awaited_once()
    live.upstream.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["origin", "capability", "stale", "secret", "oversize", "framing"]
)
async def test_live_request_guards_precede_dispatch(live, failure):
    headers = dict(live.headers)
    path = "/auth/login"
    body = b"input"
    expected = 403
    if failure == "origin":
        headers["Origin"] = "https://foreign.invalid"
    elif failure == "capability":
        headers.pop("Cookie")
    elif failure == "stale":
        live.authority = "live:" + "d" * 64
        expected = 409
    elif failure == "secret":
        path = "/.env"
        expected = 404
    elif failure == "oversize":
        body = b"x" * (1024 * 1024 + 1)
        expected = 413
    else:
        headers["Content-Length"] = "99"
        expected = 400
    response = await live.client.post(path, headers=headers, content=body)
    assert response.status_code == expected
    live.inside.assert_not_awaited()
    live.upstream.assert_not_awaited()


async def test_host_shared_live_keeps_published_transport(live, monkeypatch):
    from disco.agent_server import host_proxy

    live.sandbox.shares_host_network = True
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(202, stream=httpx.ByteStream(b"accepted"))
        )
    )
    monkeypatch.setattr(host_proxy, "_get_client", lambda: upstream)
    try:
        response = await live.client.post("/auth/login", headers=live.headers)
    finally:
        await upstream.aclose()
    assert response.status_code == 202
    live.inside.assert_not_awaited()
    live.upstream.assert_awaited_once()


async def test_live_authority_rechecked_after_awaited_session_resolution(live):
    async def resolve(cid, owner):
        assert (cid, owner) == ("aaaaaaaa", "owner-a")
        if live.resolver.await_count == 2:
            live.authority = "live:" + "d" * 64
        return live.sandbox

    live.resolver.side_effect = resolve
    response = await live.client.post("/auth/login", headers=live.headers)
    assert response.status_code == 409
    live.inside.assert_not_awaited()
    live.upstream.assert_not_awaited()


async def test_live_owner_lookup_error_never_downgrades_to_unowned_lookup(live):
    live.resolver.side_effect = TypeError("owner lookup failed")
    response = await live.client.post("/auth/login", headers=live.headers)
    assert response.status_code == 503
    live.resolver.assert_awaited_once_with("aaaaaaaa", "owner-a")
    live.inside.assert_not_awaited()
    live.upstream.assert_not_awaited()
