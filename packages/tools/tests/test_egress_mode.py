"""The three-way egress posture + proxy wiring helpers (pure functions, no Docker).

These pin the DECISION logic that gvisor.py/podman.py consume: a spec's egress
mode, and the env/argv the filtered path hands the proxy sidecar."""

from __future__ import annotations

from disco.tools.anatomy import Capability
from disco.tools.sandbox._container import (
    EGRESS_PROXY_PORT,
    egress_mode,
    format_allow,
    proxy_env,
    proxy_run_argv,
)
from disco.tools.sandbox.base import SandboxSpec


def test_egress_mode_three_way():
    assert egress_mode(SandboxSpec()) == "sealed"
    assert egress_mode(SandboxSpec(egress_allow=frozenset({"x.com"}))) == "filtered"
    assert egress_mode(SandboxSpec(public_web=True)) == "public"
    assert egress_mode(SandboxSpec(permitted=frozenset({Capability.NETWORK}))) == "public"


def test_allowlist_takes_precedence_over_network_capability():
    # An allowlist means "only these" even when the legacy NETWORK cap is also present
    # — the safer interpretation wins, so a stray capability can't widen egress.
    spec = SandboxSpec(egress_allow=frozenset({"x.com"}), permitted=frozenset({Capability.NETWORK}))
    assert egress_mode(spec) == "filtered"


def test_format_allow_is_sorted_and_clean():
    out = format_allow(frozenset({"b.com", "a.com", " ", ".pypi.org"}))
    assert out == ".pypi.org,a.com,b.com"  # sorted, blanks dropped


def test_proxy_env_routes_through_sidecar_and_skips_loopback():
    env = proxy_env("disco-egr-123")
    url = f"http://disco-egr-123:{EGRESS_PROXY_PORT}"
    assert env["HTTP_PROXY"] == url and env["https_proxy"] == url
    assert "localhost" in env["NO_PROXY"] and "127.0.0.1" in env["no_proxy"]


def test_proxy_run_argv_carries_the_allowlist():
    argv = proxy_run_argv("api.github.com,.pypi.org")
    assert argv[0] == "python3" and argv[1].endswith("egress_proxy.py")
    assert "--allow" in argv and "api.github.com,.pypi.org" in argv
    assert str(EGRESS_PROXY_PORT) in argv


def test_proxy_run_argv_carries_exact_denied_hosts():
    argv = proxy_run_argv("", public_only=True, deny_hosts=frozenset({"bus.example.com"}))
    assert argv[argv.index("--deny-host") + 1] == "bus.example.com"
