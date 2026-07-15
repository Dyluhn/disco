"""SEC-2: the kernel gateway auth token.

The gateway (`jupyter kernelgateway`) is an arbitrary-code-execution endpoint;
these tests pin the token contract that closes the unauthenticated-RCE hole:
the token must be STABLE + re-derivable per sandbox (so a lazily-reused gateway
keeps working across resume/restart), KEYED on the app secret (so it's secret
from other sandboxes and the network), and actually carried on the client's
requests.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import httpx
import pytest
from disco.tools.sandbox.base import SandboxError
from disco.tools.sandbox.kernel import GatewayKernel, _gateway_auth_token


def test_token_is_stable_per_sandbox(monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "test-master-secret")
    a = _gateway_auth_token("sbx_123")
    b = _gateway_auth_token("sbx_123")
    assert a == b  # re-derivable: a fresh GatewayKernel reconnects with the same token


def test_token_differs_per_sandbox(monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "test-master-secret")
    assert _gateway_auth_token("sbx_a") != _gateway_auth_token("sbx_b")


def test_token_is_keyed_on_app_secret(monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "secret-one")
    one = _gateway_auth_token("sbx_x")
    monkeypatch.setenv("DISCO_SECRET_KEY", "secret-two")
    two = _gateway_auth_token("sbx_x")
    assert one != two  # changing the master key changes the token


def test_token_matches_explicit_hmac(monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "master")
    expected = hmac.new(b"master", b"kernel-gateway:sbx_9", hashlib.sha256).hexdigest()
    assert _gateway_auth_token("sbx_9") == expected


def test_legacy_pmx_secret_key_is_honored(monkeypatch):
    monkeypatch.delenv("DISCO_SECRET_KEY", raising=False)
    monkeypatch.setenv("PMX_SECRET_KEY", "legacy")
    expected = hmac.new(b"legacy", b"kernel-gateway:sbx_1", hashlib.sha256).hexdigest()
    assert _gateway_auth_token("sbx_1") == expected


def test_dev_fallback_is_stable_within_process(monkeypatch):
    """No app secret → a process-local random key, still stable across calls."""
    monkeypatch.delenv("DISCO_SECRET_KEY", raising=False)
    monkeypatch.delenv("PMX_SECRET_KEY", raising=False)
    a = _gateway_auth_token("sbx_dev")
    b = _gateway_auth_token("sbx_dev")
    assert a == b and len(a) == 64  # sha256 hexdigest


def test_gateway_kernel_carries_the_token(monkeypatch):
    """The client wires the token into an Authorization header (REST) and as a
    ?token= query param (WS)."""
    monkeypatch.setenv("DISCO_SECRET_KEY", "master")

    class _FakeSandbox:
        id = "sbx_42"

    gk = GatewayKernel(_FakeSandbox(), sessions=None)
    expected = _gateway_auth_token("sbx_42")
    assert gk._auth_headers == {"Authorization": f"token {expected}"}
    # WS url gets the token, with correct separator whether or not a query exists
    assert gk._ws_with_token("ws://h/api/kernels/k/channels") == (
        f"ws://h/api/kernels/k/channels?token={expected}"
    )
    assert gk._ws_with_token("ws://h/c?x=1").endswith(f"&token={expected}")


def test_gateway_kernel_without_sandbox_id_does_not_crash(monkeypatch):
    """A sandbox object lacking `.id` falls back to a default key (no AttributeError)."""
    monkeypatch.setenv("DISCO_SECRET_KEY", "master")

    class _NoId:
        pass

    gk = GatewayKernel(_NoId(), sessions=None)
    assert gk._auth_headers["Authorization"].startswith("token ")


class _MappedSandbox:
    id = "sbx_gateway_startup"

    @staticmethod
    def internal_port_mapping(_port: int) -> tuple[str, int]:
        return ("127.0.0.1", 38899)


class _FakeHTTPClient:
    def __init__(self, *, status: int | None = None) -> None:
        self.status = status
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.status is None:
            raise httpx.ConnectError("raw transport detail must not be retained")
        return SimpleNamespace(status_code=self.status)


@pytest.mark.asyncio
async def test_gateway_startup_nonzero_fails_fast_with_token_free_diagnostic(monkeypatch):
    """H206: a dead launch is not a reason to blind-poll for 120 seconds."""

    class _ExitedSessions:
        view_calls = 0

        async def exec(self, *_args):
            return SimpleNamespace(
                running=False,
                exit_code=127,
                output=f"KG_AUTH_TOKEN={kernel._token} command not found\x00",
            )

        async def view(self, *_args):
            self.view_calls += 1
            raise AssertionError("an immediate startup exit must not enter timeout diagnostics")

    sessions = _ExitedSessions()
    kernel = GatewayKernel(_MappedSandbox(), sessions)
    client = _FakeHTTPClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client)

    started = time.monotonic()
    with pytest.raises(SandboxError) as raised:
        await kernel._ensure_gateway()
    elapsed = time.monotonic() - started
    message = str(raised.value)

    assert elapsed < 1.0
    assert client.calls == 1  # existing-gateway preflight only; no blind readiness poll
    assert sessions.view_calls == 0
    assert "exited during startup" in message
    assert "exit=127" in message
    assert "degraded fallback" in message
    assert kernel._token not in message
    assert "KG_AUTH_TOKEN" not in message
    assert "\x00" not in message


@pytest.mark.asyncio
async def test_gateway_timeout_is_wall_clock_bounded_and_sanitized(monkeypatch):
    """H206: reported startup seconds are a deadline, not a poll-count multiplier."""

    class _RunningSessions:
        async def exec(self, *_args):
            return SimpleNamespace(
                running=True,
                exit_code=None,
                output=f"starting KG_AUTH_TOKEN={kernel._token}",
            )

        async def view(self, *_args):
            return SimpleNamespace(
                running=False,
                output=f"Authorization: token {kernel._token}\nlate failure\x1b",
            )

    monkeypatch.setenv("DISCO_KERNEL_GATEWAY_START_S", "1")
    kernel = GatewayKernel(_MappedSandbox(), _RunningSessions())
    client = _FakeHTTPClient(status=503)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client)

    started = time.monotonic()
    with pytest.raises(SandboxError) as raised:
        await kernel._ensure_gateway()
    elapsed = time.monotonic() - started
    message = str(raised.value)

    assert 0.8 <= elapsed < 2.0
    assert client.calls >= 2  # preflight plus at least one bounded readiness probe
    assert "failed to start within 1s" in message
    assert "final_readiness=http_status=503" in message
    assert "late failure" in message
    assert "degraded fallback" in message
    assert kernel._token not in message
    assert "Authorization" not in message
    assert "\x1b" not in message
