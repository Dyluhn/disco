"""MCP egress-proxy routing — RP-05 rung B.

For each HTTP server in the pool, builds the union of the server's allowed_hosts
and the URL's host into the sandbox's egress allowlist. The allowlist is folded
into the sandbox spec at conversation start (UNION, not replacement).

Uses `format_allow` and `proxy_env` from `_container.py` — never edits them.
"""

from __future__ import annotations

from urllib.parse import urlparse


def url_host(url: str) -> str:
    """Extract the host[:port] from a URL for egress allowlisting.

    Returns the netloc (host + port if non-default). The sidecar's allowlist
    format supports `host:port` entries, so we keep the port when present.
    """
    parsed = urlparse(url)
    return parsed.netloc or parsed.hostname or ""


def build_egress_union(
    existing_egress: frozenset[str],
    *,
    http_server_urls: list[str],
    http_server_allowed_hosts: list[str],
) -> frozenset[str]:
    """Build the UNION of the existing egress allowlist and MCP server hosts.

    The result is a SUPERSET — the pre-existing registry hosts AND the MCP
    hosts both survive. This is the function the runtime calls to extend
    `_build_sandbox_spec`'s egress_allow.

    Args:
        existing_egress: The existing egress_allow set (e.g., REGISTRY_EGRESS_ALLOW).
        http_server_urls: The URLs of enabled HTTP MCP servers.
        http_server_allowed_hosts: The allowed_hosts from each server config.
    """
    mcp_hosts: set[str] = set()

    # Each server's URL host
    for url in http_server_urls:
        host = url_host(url)
        if host:
            mcp_hosts.add(host)

    # Each server's explicitly allowed hosts
    for host in http_server_allowed_hosts:
        host = host.strip()
        if host:
            mcp_hosts.add(host)

    # UNION — superset, not replacement
    return frozenset(existing_egress | mcp_hosts)
