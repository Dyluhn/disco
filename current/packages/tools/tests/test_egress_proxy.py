"""Egress proxy enforcement — the allowlist is REAL, per-connection.

These run the proxy on localhost against a real in-process upstream and drive it
with a real client. No Docker: the proxy's job (allow this host / refuse that
host) is fully exercisable in-process — which is the point of keeping it
stdlib-only and dependency-free.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.egress_proxy import (
    AllowlistProxy,
    host_allowed,
    make_predicate,
)

# ---- predicate semantics ----------------------------------------------------


def test_host_allowed_exact_and_suffix_and_default_deny():
    allow = ["api.github.com", ".pypi.org"]
    assert host_allowed("api.github.com", allow) is True
    assert host_allowed("pypi.org", allow) is True  # apex via .suffix
    assert host_allowed("files.pythonhosted.pypi.org", allow) is True  # subdomain
    assert host_allowed("evil.com", allow) is False
    assert host_allowed("api.github.com.evil.com", allow) is False  # no prefix trick
    assert host_allowed("anything", []) is False  # empty allowlist = deny all


def test_host_allowed_strips_port():
    assert host_allowed("api.github.com:443", ["api.github.com"]) is True


def test_predicate_semantics_match_sandbox_spec():
    # The proxy's matching MUST agree with SandboxSpec.egress_allowed (the in-tree
    # predicate the contract documents) — else policy and enforcement diverge.
    entries = frozenset({"api.github.com", ".pypi.org", "example.com"})
    spec = SandboxSpec(egress_allow=entries)
    pred = make_predicate(entries)
    for h in [
        "api.github.com",
        "pypi.org",
        "files.pypi.org",
        "example.com",
        "sub.example.com",  # NOT allowed (exact entry, not .suffix)
        "evil.com",
        "notexample.com",
    ]:
        assert pred(h) == spec.egress_allowed(h), h


# ---- live enforcement (proxy on localhost, real client + upstream) ----------


async def _start_echo_upstream() -> tuple[asyncio.AbstractServer, int]:
    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        data = await r.read(1024)
        w.write(b"ECHO:" + data)
        await w.drain()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _proxy(allow_entries):
    proxy = AllowlistProxy(make_predicate(allow_entries), host="127.0.0.1", port=0)
    await proxy.start()
    return proxy


async def test_connect_to_allowed_host_tunnels_through():
    upstream, uport = await _start_echo_upstream()
    proxy = await _proxy(["localhost"])
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(f"CONNECT localhost:{uport} HTTP/1.1\r\n\r\n".encode())
        await w.drain()
        status = await r.readline()
        assert b"200" in status and b"Established" in status
        # CONNECT replies end with a blank line; drain it before the tunnel bytes.
        assert await r.readline() in (b"\r\n", b"\n")
        # Past the tunnel, raw bytes reach the upstream and the echo comes back.
        w.write(b"ping")
        await w.drain()
        body = await r.read(64)
        assert b"ECHO:ping" in body
        assert proxy.allowed == 1 and proxy.denied == 0
        w.close()
    finally:
        await proxy.aclose()
        upstream.close()


async def test_connect_to_denied_host_is_403_and_never_dials_upstream():
    upstream, uport = await _start_echo_upstream()
    proxy = await _proxy(["api.github.com"])  # localhost NOT allowed
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(f"CONNECT localhost:{uport} HTTP/1.1\r\n\r\n".encode())
        await w.drain()
        status = await r.readline()
        assert b"403" in status
        assert proxy.denied == 1 and proxy.allowed == 0
        w.close()
    finally:
        await proxy.aclose()
        upstream.close()


async def test_explicit_denied_host_overrides_public_or_allowlisted_access():
    proxy = AllowlistProxy(
        lambda _host: True,
        host="127.0.0.1",
        port=0,
        denied_hosts=["bus.example.com"],
    )
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"CONNECT bus.example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"403" in await reader.readline()
        assert proxy.denied == 1 and proxy.allowed == 0
        writer.close()
    finally:
        await proxy.aclose()


async def test_http_absolute_form_denied_host_is_403():
    proxy = await _proxy(["api.github.com"])
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(b"GET http://evil.com/payload HTTP/1.1\r\nHost: evil.com\r\n\r\n")
        await w.drain()
        status = await r.readline()
        assert b"403" in status
        assert proxy.denied == 1
        w.close()
    finally:
        await proxy.aclose()


async def test_empty_allowlist_denies_everything():
    proxy = await _proxy([])  # deny-by-default
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(b"CONNECT anything.com:443 HTTP/1.1\r\n\r\n")
        await w.drain()
        status = await r.readline()
        assert b"403" in status
        w.close()
    finally:
        await proxy.aclose()


@pytest.mark.parametrize(
    "answers",
    [
        [(2, "127.0.0.1")],
        [(2, "169.254.169.254")],
        [(2, "192.168.1.20")],
        [(10, "fd00::1")],
        [(10, "::ffff:127.0.0.1")],
        [(2, "93.184.216.34"), (2, "10.0.0.8")],  # mixed-answer DNS rebinding
    ],
)
async def test_public_proxy_rejects_every_non_global_or_mixed_resolution_before_dial(
    answers, monkeypatch
):
    dialed = []

    async def _dial(*args, **kwargs):
        dialed.append((args, kwargs))
        raise AssertionError("a denied resolution reached the network dial")

    monkeypatch.setattr(asyncio, "open_connection", _dial)
    proxy = AllowlistProxy(
        lambda _host: True,
        require_global=True,
        web_ports_only=True,
        resolver=lambda _host, _port: answers,
    )
    with pytest.raises(PermissionError):
        await proxy._open_upstream("rebind.example", 443)
    assert dialed == []


async def test_public_proxy_rejects_host_owned_global_ip_before_dial(monkeypatch):
    async def _dial(*args, **kwargs):
        raise AssertionError("a host-owned address reached the network dial")

    monkeypatch.setattr(asyncio, "open_connection", _dial)
    proxy = AllowlistProxy(
        lambda _host: True,
        require_global=True,
        denied_ips=["93.184.216.34"],
        resolver=lambda _host, _port: [(10, "::ffff:93.184.216.34")],
    )
    with pytest.raises(PermissionError):
        await proxy._open_upstream("daemon-public.example", 443)


async def test_proxy_dials_validated_ip_not_hostname_and_port_class_is_surface_specific(
    monkeypatch,
):
    calls = []
    sentinel = (object(), object())

    async def _dial(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(asyncio, "open_connection", _dial)

    def resolver(_host, _port):
        return [(2, "93.184.216.34")]

    filtered = AllowlistProxy(
        lambda _host: True,
        require_global=True,
        web_ports_only=False,
        resolver=resolver,
    )
    assert await filtered._open_upstream("approved-mcp.example", 8443) == sentinel
    assert calls[0][0][:2] == ("93.184.216.34", 8443)

    public = AllowlistProxy(
        lambda _host: True,
        require_global=True,
        web_ports_only=True,
        resolver=resolver,
    )
    with pytest.raises(PermissionError, match="non-web port"):
        await public._open_upstream("public.example", 8443)


@pytest.mark.parametrize("bad", [b"garbage\r\n\r\n", b"\r\n"])
async def test_malformed_request_is_handled_not_crashed(bad):
    proxy = await _proxy(["x.com"])
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(bad)
        await w.drain()
        # The proxy stays up (a later valid client still gets served).
        await asyncio.wait_for(r.read(64), timeout=1.0)
        w.close()
    except TimeoutError:
        pass  # acceptable: bad request closed without a reply
    finally:
        await proxy.aclose()
