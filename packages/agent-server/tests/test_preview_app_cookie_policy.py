"""Embedded cookie adaptation is scoped to verified canonical Preview sessions."""

from dataclasses import replace

import httpx
import pytest
from disco.agent_server.host_proxy_parts._cookies import _rewrite_embedded_app_cookie
from disco.agent_server.host_proxy_parts._forwarding import (
    _injected_html_headers,
    _response_forward_headers,
)
from disco.core.auth import (
    PreviewCapability,
)
from test_host_proxy import upstream_http as upstream_http


def _scope(embedded=True):
    cap = PreviewCapability(
        "owner",
        "conv_aaaaaaaaowner",
        8000,
        ("GET", "POST"),
        "/",
        2**31,
        authority_id="live:" + "a" * 64,
        partitioned_cookies=embedded,
    )
    return {"type": "http", "state": {"canonical_preview_capability": cap}}


@pytest.mark.parametrize(
    "cookie,expected",
    [
        (
            "tc_session=token; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400",
            "tc_session=token; HttpOnly; Path=/; Max-Age=86400; Secure; SameSite=None; Partitioned",
        ),
        (
            "tc_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
            "tc_session=; HttpOnly; Path=/; Max-Age=0; Secure; SameSite=None; Partitioned",
        ),
        (
            "app=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/app; "
            "Domain=preview.example; SameSite=Lax",
            "app=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/app; "
            "Domain=preview.example; Secure; SameSite=None; Partitioned",
        ),
        (
            "app=quoted=token; sAmEsItE=Lax; Secure; SameSite=Strict; partitioned; Priority=High",
            "app=quoted=token; Priority=High; Secure; SameSite=None; Partitioned",
        ),
        ("theme=dark", "theme=dark; Secure; SameSite=None; Partitioned"),
    ],
)
def test_embedded_cookie_preserves_value_scope_expiry_and_deletion(cookie, expected):
    assert _rewrite_embedded_app_cookie(cookie, _scope()) == expected
    assert _rewrite_embedded_app_cookie(cookie, _scope(False)) == cookie


@pytest.mark.parametrize(
    "cookie",
    [
        "__Host-session=value; Secure; HttpOnly; Path=/; SameSite=Strict",
        "__Secure-session=value; Secure; HttpOnly; Path=/auth",
        "__Http-session=value; Secure; HttpOnly; SameSite=Strict",
        "__Host-Http-session=value; Secure; HttpOnly; Path=/",
    ],
)
def test_valid_cookie_prefix_requirements_remain_present(cookie):
    changed = _rewrite_embedded_app_cookie(cookie, _scope())
    assert changed.startswith(cookie.split(";", 1)[0] + ";")
    assert "; Secure" in changed and "; HttpOnly" in changed
    if cookie.startswith(("__Host-", "__Host-Http-")):
        assert "; Path=/;" in changed and "Domain=" not in changed
    assert changed.endswith("SameSite=None; Partitioned")


@pytest.mark.parametrize(
    "cookie,retained",
    [
        (
            "__Host-invalid=x; Domain=other.example; Path=/wrong",
            ["Domain=other.example", "Path=/wrong"],
        ),
        ("__Http-invalid=x; Path=/", ["Path=/"]),
    ],
)
def test_cookie_adaptation_does_not_repair_invalid_prefix_scope_or_add_httponly(cookie, retained):
    changed = _rewrite_embedded_app_cookie(cookie, _scope())
    for attribute in retained:
        assert attribute in changed
    assert "HttpOnly" not in changed


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {},
        {"headers": [(b"sec-fetch-dest", b"iframe"), (b"sec-fetch-site", b"cross-site")]},
        {"state": {"canonical_preview_capability": {"partitioned_cookies": True}}},
    ],
)
def test_request_metadata_or_unverified_policy_never_enables_adaptation(scope):
    cookie = "app=secret; HttpOnly; SameSite=Strict; Path=/"
    assert _rewrite_embedded_app_cookie(cookie, scope) == cookie


def test_noncanonical_or_old_capability_keeps_cookie_unchanged():
    scope = _scope()
    cap = scope["state"]["canonical_preview_capability"]
    scope["state"]["canonical_preview_capability"] = replace(cap, authority_id=None)
    cookie = "app=secret; SameSite=Strict"
    assert _rewrite_embedded_app_cookie(cookie, scope) == cookie
    assert _rewrite_embedded_app_cookie(cookie, _scope(False)) == cookie


@pytest.mark.parametrize("html", [False, True])
def test_shared_response_paths_adapt_repeated_app_cookies_after_reserved_filtering(html):
    response = httpx.Response(
        200,
        headers=[
            ("Set-Cookie", "tc_session=opaque; HttpOnly; SameSite=Strict; Path=/"),
            ("Set-Cookie", "theme=dark; SameSite=Lax; Path=/"),
            ("Set-Cookie", "disco_session=bad; SameSite=Strict"),
            ("Set-Cookie", "disco_preview_cap=bad; SameSite=Strict"),
            ("Set-Cookie", "disco_path_preview_abc=bad; SameSite=Strict"),
            ("Set-Cookie", "disco_local_preview_19141=bad; SameSite=Strict"),
        ],
    )
    args = {"local_gateway": True, "upstream": "http://127.0.0.1:8000", "request_scope": _scope()}
    if html:
        headers = _injected_html_headers(response, set(), **args, changed=True, injected=b"html")
    else:
        headers = _response_forward_headers(response, set(), **args)
    assert [v.decode() for k, v in headers if k == b"set-cookie"] == [
        "tc_session=opaque; HttpOnly; Path=/; Secure; SameSite=None; Partitioned",
        "theme=dark; Path=/; Secure; SameSite=None; Partitioned",
    ]
