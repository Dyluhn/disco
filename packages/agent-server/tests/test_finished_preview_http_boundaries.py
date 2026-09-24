"""Request collection and manager-generation authorization before HTTP dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from disco.agent_server.preview_content import PreviewContentService
from disco.agent_server.routes.preview_http import finished_http_response
from fastapi import Request
from test_preview_resource_contract import _FakeSandbox, _manager, _sealed_contract


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_conversation",
        "wrong_port",
        "stale_generation",
        "missing_manager",
        "unbound_contract",
        "wrong_projection",
        "novnc",
    ],
)
async def test_finished_request_requires_exact_manager_runtime(failure, monkeypatch):
    sandbox = _FakeSandbox()
    manager = _manager(sandbox, port_pool=[3000])
    contract = _sealed_contract(1, "a")
    host = SimpleNamespace(_preview_manager=manager)
    service = PreviewContentService(SimpleNamespace(live_session=lambda _cid: host))
    dispatched = AsyncMock(side_effect=AssertionError("unbound request must not dispatch"))
    monkeypatch.setattr("disco.agent_server.preview_request.request_inside", dispatched)
    try:
        restored = await manager.restore_sealed(contract)
        assert restored is not None
        cid, port, generation = contract.conversation_id, 3000, restored.projection_id
        if failure == "wrong_conversation":
            cid = "conv_someone_else"
        elif failure == "wrong_port":
            port = 5173
        elif failure == "stale_generation":
            sandbox.generation += 1
        elif failure == "missing_manager":
            host._preview_manager = None
        elif failure == "unbound_contract":
            contract = _sealed_contract(2, "b")
        elif failure == "wrong_projection":
            generation = "pv_other"
        elif failure == "novnc":
            port = 6080
        assert (
            await service.request_finished_inside(
                cid,
                contract,
                port,
                "POST",
                "/write",
                [],
                b"mutation",
                projection_id=generation,
            )
            is None
        )
        dispatched.assert_not_awaited()
    finally:
        await manager.aclose()


@pytest.mark.parametrize(
    "case,expected",
    [
        ("declared_oversize", 413),
        ("chunked_oversize", 413),
        ("disconnect", 400),
        ("truncated", 400),
        ("negative_length", 413),
    ],
)
async def test_incomplete_or_oversized_body_never_dispatches(case, expected):
    headers = []
    frames = [{"type": "http.request", "body": b"short", "more_body": False}]
    if case == "declared_oversize":
        headers = [(b"content-length", b"1048577")]
    elif case == "chunked_oversize":
        frames[0]["body"] = b"x" * (1024 * 1024 + 1)
    elif case == "disconnect":
        frames = [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    elif case == "truncated":
        headers = [(b"content-length", b"10")]
    elif case == "negative_length":
        headers = [(b"content-length", b"-1")]
    receive = AsyncMock(side_effect=frames)
    request = Request({"type": "http", "method": "POST", "headers": headers}, receive)
    dispatched = AsyncMock()
    runtime = SimpleNamespace(
        preview=SimpleNamespace(
            content=SimpleNamespace(
                request_finished_inside=dispatched,
            )
        )
    )
    response = await finished_http_response(
        runtime,
        request,
        "conv",
        _sealed_contract(1, "a"),
        3000,
        "pv_current",
        "/write",
        inject_selection=True,
    )
    assert response.status_code == expected
    dispatched.assert_not_awaited()


async def test_app_request_headers_preserve_auth_and_strip_all_reserved_cookies():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "headers": [
                (b"host", b"preview.example"),
                (b"authorization", b"Bearer app-auth"),
                (b"cookie", b"disco_session=secret; disco_preview_cap=secret; app_one=1"),
                (
                    b"cookie",
                    b"disco_path_preview_abc=secret; disco_local_preview_19141=secret; app_two=2",
                ),
                (b"connection", b"x-private"),
                (b"connection", b"x-other"),
                (b"x-private", b"must-not-pass"),
                (b"x-other", b"must-not-pass"),
                (b"proxy-authorization", b"secret"),
                (b"x-repeated", b"one"),
                (b"x-repeated", b"two"),
                (b"content-length", b"3"),
            ],
        },
        AsyncMock(return_value={"type": "http.request", "body": b"app", "more_body": False}),
    )
    dispatched = AsyncMock(
        return_value=(
            201,
            [
                ("Set-Cookie", "app_one=1"),
                ("Set-Cookie", "app_two=2"),
                ("Set-Cookie", "disco_session=forged"),
                ("Set-Cookie", "disco_preview_cap=forged"),
                ("Set-Cookie", "disco_path_preview_abc=forged"),
                ("Set-Cookie", "disco_local_preview_19141=forged"),
                ("Connection", "x-private"),
                ("X-Private", "must-not-pass"),
                ("X-Latin", "\xc3\xa9"),
                ("X-Repeated", "one"),
                ("X-Repeated", "two"),
                ("Location", "http://127.0.0.1:3000/private?x=1"),
            ],
            b"\x00\xff",
        )
    )
    runtime = SimpleNamespace(
        preview=SimpleNamespace(
            content=SimpleNamespace(
                request_finished_inside=dispatched,
            )
        )
    )
    response = await finished_http_response(
        runtime,
        request,
        "conv",
        _sealed_contract(1, "a"),
        3000,
        "pv_current",
        "/login",
        inject_selection=True,
    )
    assert response.status_code == 201 and response.body == b"\x00\xff"
    sent_headers = dispatched.await_args.args[5]
    assert ("authorization", "Bearer app-auth") in sent_headers
    assert [v for k, v in sent_headers if k == "cookie"] == ["app_one=1", "app_two=2"]
    assert [v for k, v in sent_headers if k == "x-repeated"] == ["one", "two"]
    assert not any(
        k in {"connection", "x-private", "x-other", "proxy-authorization", "content-length", "host"}
        for k, _ in sent_headers
    )
    assert response.headers.getlist("set-cookie") == ["app_one=1", "app_two=2"]
    assert response.headers.getlist("x-repeated") == ["one", "two"]
    assert (b"x-latin", b"\xc3\xa9") in response.raw_headers
    assert "x-private" not in response.headers
    assert response.headers["location"] == "http://preview.example/private?x=1"
