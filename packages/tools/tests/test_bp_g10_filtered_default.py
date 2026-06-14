"""BP-G10 — flip the Build surface's default egress from "open" to "filtered".

UNIT part only (live VM 201 host-reach is deferred). The Build sandbox spec
now defaults to egress="filtered" by default, so build boxes get the
allowlisting proxy (E8 wired the proxy on every backend — gVisor, podman,
local — so flipping the default is safe everywhere). Open remains an
explicit escape hatch (PMX_BUILD_EGRESS=open) for debug/dev use.

Acceptance:
  • _build_sandbox_spec() with no env var set  →  egress_allow = REGISTRY_EGRESS_ALLOW
  • _build_sandbox_spec() with PMX_BUILD_EGRESS=open  →  NETWORK capability granted
  • _mcp_proxy_env()  defaults to a NON-None proxy env (was None)
  • gVisor filtered wiring is UNCHANGED (the E8 regression guard still holds)
  • The npm/pip/git registry hosts are covered by the default allowlist

References:
  - packages/agent-server/src/disco/agent_server/runtime.py  `_build_sandbox_spec` (BP-G10 default flip)
  - packages/agent-server/src/disco/agent_server/runtime.py  `_mcp_proxy_env`       (matching default flip)
  - packages/tools/src/disco/tools/sandbox/base.py:79  REGISTRY_EGRESS_ALLOW
  - packages/tools/src/disco/tools/sandbox/gvisor.py   `_setup_filtered_egress` (UNCHANGED)
  - packages/tools/src/disco/tools/sandbox/podman.py   `_setup_filtered_egress` (E8 wired)
  - packages/tools/src/disco/tools/sandbox/local.py    (E8 wired)
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from disco.tools import REGISTRY_EGRESS_ALLOW, Capability
from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxConfig,
    SandboxSpec,
)

# Reuse the hermetic fakes from test_gvisor.py — they cover the full docker-py
# surface the GvisorSandboxService drives, and importing them keeps the BP-G10
# surface minimal (one regression hub for the egress posture).
from test_gvisor import FakeDockerClient


# ---------------------------------------------------------------------------
# 1. The default flip — the core acceptance
# ---------------------------------------------------------------------------


def _runtime():
    """A minimal ConversationRuntime — the spec helpers read `_sandbox_spec`
    and `os.environ` only, so the rest of the runtime is irrelevant here."""
    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SqliteEventStore

    return ConversationRuntime(SqliteEventStore(":memory:"))


def test_default_build_spec_is_filtered():
    """BP-G10 core: with NO env var set, the Build spec defaults to filtered
    (REGISTRY_EGRESS_ALLOW populated, Capability.NETWORK NOT granted). This
    is the flip — was "open" before BP-G10 (Capability.NETWORK granted, empty
    egress_allow), which silently gave every build box the full internet."""
    rt = _runtime()
    # Make sure the env var is NOT set for this test (other tests / CI may
    # have set it; the default only matters when it is absent).
    env = {k: v for k, v in os.environ.items() if k != "PMX_BUILD_EGRESS"}
    with mock.patch.dict(os.environ, env, clear=True):
        spec = rt._build_sandbox_spec()
    assert spec.egress_allow == REGISTRY_EGRESS_ALLOW, (
        f"default Build spec must carry the registry allowlist; got {spec.egress_allow!r}"
    )
    assert Capability.NETWORK not in spec.permitted, (
        "filtered spec must NOT grant NETWORK (the allowlist is the gate)"
    )


def test_default_build_spec_egress_mode_resolves_to_filtered():
    """The end-to-end predicate: `egress_mode(spec)` resolves a filtered
    default spec to "filtered" — the mode the runtime/the proxy/the allowlist
    converge on. (egress_mode lives in _container.py.)"""
    from disco.tools.sandbox._container import egress_mode

    rt = _runtime()
    env = {k: v for k, v in os.environ.items() if k != "PMX_BUILD_EGRESS"}
    with mock.patch.dict(os.environ, env, clear=True):
        spec = rt._build_sandbox_spec()
    assert egress_mode(spec) == "filtered"


def test_explicit_open_escape_hatch_still_works():
    """The escape hatch: PMX_BUILD_EGRESS=open explicitly grants NETWORK (no
    allowlist, full egress). This is the path debug/dev use when a real
    network is genuinely required; it is NO LONGER the default, but it must
    still be reachable for opt-in."""
    rt = _runtime()
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "open"}):
        spec = rt._build_sandbox_spec()
    assert Capability.NETWORK in spec.permitted
    assert not spec.egress_allow


def test_explicit_filtered_unchanged():
    """Regression guard for BP-09: explicit PMX_BUILD_EGRESS=filtered is the
    same as the new default (registry allowlist, no NETWORK). This MUST hold
    — anyone setting the var explicitly is depending on this exact shape."""
    rt = _runtime()
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "filtered"}):
        spec = rt._build_sandbox_spec()
    assert spec.egress_allow == REGISTRY_EGRESS_ALLOW
    assert Capability.NETWORK not in spec.permitted


# ---------------------------------------------------------------------------
# 2. _mcp_proxy_env — the orchestrator-side flip (single source of truth)
# ---------------------------------------------------------------------------


def test_default_mcp_proxy_env_is_not_none():
    """The matching default flip on `_mcp_proxy_env`: the orchestrator-side
    MCP HTTP client must route through the same proxy the build box routes
    through (single source of truth — no divergent egress policy). Default
    posture is filtered → proxy env is NOT None; was None under the old
    open default."""
    rt = _runtime()
    env = {k: v for k, v in os.environ.items() if k != "PMX_BUILD_EGRESS"}
    with mock.patch.dict(os.environ, env, clear=True):
        proxy = rt._mcp_proxy_env()
    assert proxy is not None, (
        "default posture is now filtered → _mcp_proxy_env must return a proxy env"
    )
    assert proxy is not None
    assert proxy["HTTPS_PROXY"].startswith("http://")
    assert ":8888" in proxy["HTTPS_PROXY"]


def test_explicit_open_mcp_proxy_env_is_none():
    """The escape hatch on the orchestrator side: PMX_BUILD_EGRESS=open also
    drops the proxy env (matching the open sandbox posture — no sidecar to
    route through)."""
    rt = _runtime()
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "open"}):
        assert rt._mcp_proxy_env() is None


# ---------------------------------------------------------------------------
# 3. The allowlist covers npm / pip / git (legitimate build fetches)
# ---------------------------------------------------------------------------


def test_registry_egress_allow_covers_npm_pip_git():
    """The BP-G10 default relies on REGISTRY_EGRESS_ALLOW covering the
    registries a build actually fetches from. If npm / pip / git are missing
    the default flipped posture would BREAK legitimate builds. Confirm they
    are present (exact or .suffix form)."""
    spec = SandboxSpec(egress_allow=REGISTRY_EGRESS_ALLOW)

    # npm
    assert spec.egress_allowed("registry.npmjs.org"), "npm registry not in allowlist"
    # .npmjs.org suffix (any subdomain)
    assert spec.egress_allowed("anything.npmjs.org"), ".npmjs.org suffix missing"

    # pip
    assert spec.egress_allowed("pypi.org"), "pypi.org not in allowlist"
    assert spec.egress_allowed("files.pythonhosted.org"), "PyPI host files missing"

    # git (GitHub)
    assert spec.egress_allowed("github.com"), "github.com not in allowlist"
    assert spec.egress_allowed("codeload.github.com"), "GitHub codeload missing"
    # .githubusercontent.com suffix (raw.githubusercontent.com, objects.githubusercontent.com, …)
    assert spec.egress_allowed("raw.githubusercontent.com"), "githubusercontent suffix missing"

    # Debian apt (build images often apt-install)
    assert spec.egress_allowed("deb.debian.org"), "Debian mirror missing"


def test_registry_egress_allow_does_not_cover_alternative_git_hosts():
    """GAP (documented, not a blocker for BP-G10 default flip): GitLab
    (gitlab.com) and Bitbucket (bitbucket.org) are NOT in the default
    allowlist. A build that clones from GitLab over the default
    `filtered` posture will be denied by the proxy — an explicit
    egress_allow augmentation is required (the orchestrator-side
    `_mcp_egress_hosts` unioning pattern would extend the same way for
    git hosts once BP-G11/etc. widens the surface). This is NOT a
    regression of BP-G10 — it's the same posture as before, just the
    DEFAULT. It's called out so the gap is visible to anyone reading
    the allowlist audit."""
    spec = SandboxSpec(egress_allow=REGISTRY_EGRESS_ALLOW)
    # These are intentionally absent — the test pins the gap so a future
    # allowlist audit can confirm whether to add them.
    assert not spec.egress_allowed("gitlab.com"), "gitlab.com is not in the default allowlist"
    assert not spec.egress_allowed("bitbucket.org"), "bitbucket.org is not in the default allowlist"


# ---------------------------------------------------------------------------
# 4. gVisor filtered path is UNCHANGED (BP-G10 must not regress E8)
# ---------------------------------------------------------------------------


def _gvisor_svc(tmp_path) -> tuple[GvisorSandboxService, FakeDockerClient]:
    cfg = SandboxConfig(workspace_root=str(tmp_path))
    client = FakeDockerClient(has_image=True)
    return GvisorSandboxService(cfg, client=client), client


async def test_gvisor_filtered_path_unchanged_no_regression(tmp_path):
    """BP-G10 must NOT change the gVisor filtered path (E8 contract). The
    filtered wiring — internal no-NAT net, sidecar, proxy env — must remain
    identical. A regression here would mean the default flip has silently
    broken a backend that the default posture now reaches."""
    svc, client = _gvisor_svc(tmp_path)
    inst = await svc.create(
        SandboxSpec(egress_allow=REGISTRY_EGRESS_ALLOW),
        owner_id="o",
        conversation_id="c",
    )
    # (a) The same internal no-NAT network pattern (E8's core guarantee).
    assert len(client.networks.created) == 1
    net = client.networks.created[0]
    assert net.attrs.get("internal") is True
    # (b) The sidecar exists, started (boot-time NICs), connected to the net.
    assert len(client.runs) == 2  # sidecar + sandbox
    sidecar = client.runs[0]
    assert sidecar.started is True
    assert net.connected and net.connected[0][0] is sidecar
    # (c) The sandbox is on the internal net, NOT bridge, NOT network_mode="none".
    sb_kw = client.last.run_kwargs
    assert "network_mode" not in sb_kw
    assert sb_kw.get("network") == net.name
    # (d) Proxy env is wired (defense in depth atop the no-route net).
    assert sb_kw["environment"]["HTTPS_PROXY"].startswith("http://")
    assert ":8888" in sb_kw["environment"]["HTTPS_PROXY"]
    # (e) The aux refs are attached on the instance (destroy tears them down).
    assert inst._egress_network is net
    assert inst._egress_sidecar is sidecar


async def test_gvisor_sealed_and_open_paths_unchanged(tmp_path):
    """E8 + BP-G10 together must keep the non-filtered paths stable. Default
    spec → sealed (network_mode="none"); explicit NETWORK capability → open
    (network_mode="bridge"). Neither path may have been touched."""
    svc, client = _gvisor_svc(tmp_path)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["network_mode"] == "none"
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})),
        owner_id="o",
        conversation_id="c",
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"


# ---------------------------------------------------------------------------
# 5. _compose_build_loop — the production path: default → filtered spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_build_loop_default_produces_filtered_spec():
    """End-to-end through the production entry point: the loop a build
    conversation actually gets. With no env var set, the spec the sandbox
    receives is filtered (registry allowlist, no NETWORK). This is the
    BEHAVIORAL acceptance — the default flip is real, not just a spec
    helper internal change."""
    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SqliteEventStore
    from disco.core.llm import DefaultLLMRouter
    from disco.core.loop import RouterAgent

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)

    env = {k: v for k, v in os.environ.items() if k != "PMX_BUILD_EGRESS"}
    with mock.patch.dict(os.environ, env, clear=True):
        with mock.patch.object(rt, "_sandbox_service_now"):
            loop = rt._compose_build_loop("c_default", router, agent)
            spec = loop.executor._sandbox.spec
    assert spec.egress_allow == REGISTRY_EGRESS_ALLOW
    assert Capability.NETWORK not in spec.permitted
