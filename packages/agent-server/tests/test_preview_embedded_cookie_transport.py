"""Embedded Preview cookie transport regression.

Protocol evidence only (not browser acceptance): iframe bootstrap mints a
Secure/SameSite=None/Partitioned capability, but later same-origin fetches
carry no iframe metadata, so upstream ``Set-Cookie`` passes through
unpartitioned. The embedded case asserts the normalized policy and should
fail until forwarding normalizes it; the top-level case pins passthrough.
"""

import disco.agent_server.host_proxy as host_proxy_module
import httpx
import pytest
from disco.agent_server.host_proxy import HostPreviewProxyMiddleware
from disco.core.auth import (
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_COOKIE,
    AuthSession,
    PreviewCapabilitySigner,
)
from disco.core.store.sqlite import SqliteEventStore
from starlette.applications import Starlette
from test_host_proxy import upstream_http as upstream_http  # noqa: F401

_AUTHORITY = "live:" + "a" * 64
_CONVERSATION = "conv_aaaaaaaaowner"
_HOST = "p2-aaaaaaaa-8000.localhost"


def _make_app(upstream_http_url, store):
    app = Starlette()
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda cid8, port, _owner=None: (
            upstream_http_url if cid8 == "aaaaaaaa" and port == 8000 else None
        ),
        require_capability=True,
        redemption_store=store,
        canonical_authority_resolver=lambda *_args: _AUTHORITY,
    )
    return app


def _mint_intent(signer):
    session = AuthSession("dummy-owner", "csrf", "session", 2**31)
    return signer.mint_intent(
        session=session,
        conversation_id=_CONVERSATION,
        port=8000,
        target_path="/",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
        authority_id=_AUTHORITY,
    )


def _assert_reserved_filtered(set_cookies):
    assert not any(c.lstrip().startswith(f"{PREVIEW_COOKIE}=") for c in set_cookies)
    for prefix in ("disco_preview_cap_extra=preserved", "Disco_preview_cap=case-distinct"):
        cookie = next(value for value in set_cookies if value.split(";", 1)[0] == prefix)
        assert "Path=/" in [part.strip() for part in cookie.split(";")[1:]]


@pytest.mark.asyncio
@pytest.mark.parametrize("embedded", [True, False], ids=["embedded", "top-level"])
async def test_preview_cookie_transport_bootstrap_cases(upstream_http, monkeypatch, embedded):
    store = SqliteEventStore(":memory:")
    upstream_client = httpx.AsyncClient()
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: upstream_client)
    try:
        app = _make_app(upstream_http, store)
        signer = PreviewCapabilitySigner(redemption_store=store)
        intent = _mint_intent(signer)
        bootstrap_headers = (
            {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"}
            if embedded
            else {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-origin"}
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{_HOST}") as client:
            redemption = await client.post(
                PREVIEW_BOOTSTRAP_PATH,
                data={"intent": intent},
                headers=bootstrap_headers,
            )
            assert redemption.status_code == 200
            capability_cookie = redemption.headers["set-cookie"].split(";", 1)[0]
            assert capability_cookie.startswith(f"{PREVIEW_COOKIE}=")
            fetch = await client.get(
                "/cookies",
                headers={
                    "Cookie": capability_cookie,
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            assert fetch.status_code == 200
            set_cookies = fetch.headers.get_list("set-cookie")
            _assert_reserved_filtered(set_cookies)
            app_session = next(c for c in set_cookies if c.startswith("app_session="))
            if embedded:
                assert "Secure" in app_session
                assert "samesite=none" in app_session.lower()
                assert "Partitioned" in app_session
            else:
                assert app_session == "app_session=preserved; Path=/"
    finally:
        await upstream_client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_preview_denied_origin_stays_rejected(upstream_http, monkeypatch):
    store = SqliteEventStore(":memory:")
    upstream_client = httpx.AsyncClient()
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: upstream_client)
    try:
        app = _make_app(upstream_http, store)
        signer = PreviewCapabilitySigner(redemption_store=store)
        intent = _mint_intent(signer)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{_HOST}") as client:
            redemption = await client.post(
                PREVIEW_BOOTSTRAP_PATH,
                data={"intent": intent},
                headers={"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"},
            )
            assert redemption.status_code == 200
            capability_cookie = redemption.headers["set-cookie"].split(";", 1)[0]
            rejected = await client.post(
                "/api/submit",
                content=b"must not reach upstream",
                headers={
                    "Cookie": capability_cookie,
                    "Origin": "https://evil.example",
                },
            )
            assert rejected.status_code == 403
            assert rejected.text == "preview origin required"
    finally:
        await upstream_client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_preview_forged_capability_without_bootstrap_rejected(upstream_http, monkeypatch):
    store = SqliteEventStore(":memory:")
    upstream_client = httpx.AsyncClient()
    monkeypatch.setattr(host_proxy_module, "_get_client", lambda: upstream_client)
    try:
        app = _make_app(upstream_http, store)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{_HOST}") as client:
            forged = await client.get(
                "/cookies",
                headers={
                    "Cookie": f"{PREVIEW_COOKIE}=forged-invalid-token",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            assert forged.status_code == 403
    finally:
        await upstream_client.aclose()
        store.close()
