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
