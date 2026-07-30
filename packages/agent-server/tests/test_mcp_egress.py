"""MCP egress routing tests — RP-05 rung B.

Anti-gaming bar (from master brief):
- sandbox egress_allow == existing_registry_egress ∪ mcp_hosts
- Assert BOTH the pre-existing hosts AND the MCP hosts survive
- A test that would still pass if you REPLACED the set is a reject
"""

from __future__ import annotations

from disco.tools.mcp.http_egress import build_egress_union, url_host
from disco.tools.sandbox.base import REGISTRY_EGRESS_ALLOW


def test_url_host_extracts_netloc():
    """url_host extracts host[:port] from a URL."""
    assert url_host("https://api.example.com/mcp") == "api.example.com"
    assert url_host("http://localhost:8080/path") == "localhost:8080"
    assert url_host("https://sub.domain.com:443/test") == "sub.domain.com:443"
    assert url_host("http://192.168.1.100:9999") == "192.168.1.100:9999"
    assert url_host("") == ""


def test_build_egress_union_is_superset_not_replacement():
    """ANTI-GAMING: the result is a SUPERSET — pre-existing hosts AND MCP
    hosts BOTH survive. A test that would still pass if you REPLACED the
    set is a reject. This test explicitly asserts BOTH sets survive."""
    existing = frozenset(
        {
            "registry.npmjs.org",
            "pypi.org",
            "github.com",
        }
    )

    mcp_urls = ["https://api.example.com/mcp", "https://mcp2.org:9090/stream"]
    mcp_allowed = ["cdn.example.com", "*.mcp.org"]

    result = build_egress_union(
        existing,
        http_server_urls=mcp_urls,
        http_server_allowed_hosts=mcp_allowed,
    )

    # PRE-EXISTING hosts survive (REPLACEMENT would drop these)
    assert "registry.npmjs.org" in result
    assert "pypi.org" in result
    assert "github.com" in result

    # MCP URL hosts survive
    assert "api.example.com" in result
    assert "mcp2.org:9090" in result

    # MCP allowed hosts survive
    assert "cdn.example.com" in result
    assert "*.mcp.org" in result

    # The result has the union size (pre-existing + MCP-specific)
    assert len(result) >= 7  # 3 existing + 2 URLs + 2 allowed = 7


def test_build_egress_union_with_real_registry():
    """Drive the REAL REGISTRY_EGRESS_ALLOW as the base — production path.

    The registry hosts are well-known and must survive the union.
    """
    mcp_urls = ["https://my-mcp-server.internal:7777/mcp"]
    mcp_allowed = ["api.myservice.com"]

    result = build_egress_union(
        REGISTRY_EGRESS_ALLOW,
        http_server_urls=mcp_urls,
        http_server_allowed_hosts=mcp_allowed,
    )

    # Registry hosts survive
    assert "registry.npmjs.org" in result
    assert "pypi.org" in result
    assert "github.com" in result

    # MCP hosts survive
    assert "my-mcp-server.internal:7777" in result
    assert "api.myservice.com" in result

    # The result is a STRICT superset of the registry
    assert result.issuperset(REGISTRY_EGRESS_ALLOW)
    # And it's larger (has the MCP additions)
    assert len(result) > len(REGISTRY_EGRESS_ALLOW)


def test_build_egress_union_with_empty_existing():
    """Even with an empty base, the union correctly includes MCP hosts."""
    result = build_egress_union(
        frozenset(),
        http_server_urls=["https://only.example.com/mcp"],
        http_server_allowed_hosts=["extra.example.com"],
    )

    assert "only.example.com" in result
    assert "extra.example.com" in result
    assert len(result) == 2


def test_build_egress_union_with_no_mcp_servers():
    """When no MCP hosts are provided, the existing set is returned unchanged."""
    existing = frozenset(["pypi.org", "github.com"])
    result = build_egress_union(
        existing,
        http_server_urls=[],
        http_server_allowed_hosts=[],
    )

    assert result == existing


def test_build_egress_union_deduplicates():
    """If a host appears in both existing and MCP lists, it's deduplicated."""
    existing = frozenset(["pypi.org", "github.com"])
    result = build_egress_union(
        existing,
        http_server_urls=["https://github.com/mcp"],
        http_server_allowed_hosts=["pypi.org"],
    )

    assert len(result) == 2  # deduplicated
    assert "pypi.org" in result
    assert "github.com" in result


def test_runtime_mcp_egress_hosts_method():
    """The McpManager._mcp_egress_hosts() method computes the correct
    union from active HTTP clients."""
    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SecurityRisk, SqliteEventStore
    from disco.tools.mcp.config import McpServerConfig
    from disco.tools.mcp.http import McpHttpClient

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)

    # Add a fake HTTP client with a known URL and allowed_hosts
    config = McpServerConfig(
        name="test_srv",
        transport="streamable_http",
        url="https://test-mcp.example.com:8443/mcp",
        allowed_hosts=["cdn.test.example.com"],
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    client = McpHttpClient(server=config, call_timeout_s=5.0)
    runtime._mcp._http_clients["test_srv"] = client

    hosts = runtime._mcp._mcp_egress_hosts()

    assert "test-mcp.example.com:8443" in hosts
    assert "cdn.test.example.com" in hosts
    store.close()


def test_build_sandbox_spec_unions_mcp_hosts_into_egress_allow(monkeypatch):
    """ANTI-GAMING (spec level, not just the host-set helper): under
    PMX_BUILD_EGRESS=filtered, the ACTUAL SandboxSpec the sandbox receives carries
    egress_allow = REGISTRY_EGRESS_ALLOW ∪ {mcp hosts}. Both survive — a REPLACEMENT
    (drop the registry, keep only MCP) would fail the registry assertions; an
    omission (ignore MCP) would fail the MCP-host assertion. The earlier tests stop
    at build_egress_union / _mcp_egress_hosts; this one drives _build_sandbox_spec,
    the real production fold-in point."""
    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SecurityRisk, SqliteEventStore
    from disco.tools.mcp.config import McpServerConfig
    from disco.tools.mcp.http import McpHttpClient

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    config = McpServerConfig(
        name="egress_srv",
        transport="streamable_http",
        url="https://mcp-egress.example:7443/mcp",
        allowed_hosts=["cdn.mcp-egress.example"],
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    runtime._mcp._http_clients["egress_srv"] = McpHttpClient(server=config, call_timeout_s=5.0)

    monkeypatch.setenv("PMX_BUILD_EGRESS", "filtered")
    spec = runtime._sandbox._build_sandbox_spec(mcp_egress_hosts=runtime._mcp._mcp_egress_hosts())

    allow = set(spec.egress_allow)
    # Registry hosts survive (a REPLACEMENT would have dropped these).
    assert REGISTRY_EGRESS_ALLOW.issubset(allow)
    # MCP URL host + the server's allowed_hosts are unioned in.
    assert "mcp-egress.example:7443" in allow
    assert "cdn.mcp-egress.example" in allow
    # Strictly larger than the registry alone — the MCP additions are real.
    assert len(allow) > len(REGISTRY_EGRESS_ALLOW)

    # CONTROL: with no MCP hosts, the spec is exactly the registry base — proves
    # the additions above came from the MCP union, not from the spec by default.
    spec_bare = runtime._sandbox._build_sandbox_spec(mcp_egress_hosts=frozenset())
    assert set(spec_bare.egress_allow) == set(REGISTRY_EGRESS_ALLOW)
    store.close()
