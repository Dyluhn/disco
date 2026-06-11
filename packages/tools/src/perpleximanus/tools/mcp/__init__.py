"""MCP client — Model Context Protocol v1 (RP-05 rung A).

Public surface: McpPool, McpServerConfig, McpServerSpec, ApprovalRecord,
McpToolDescriptor, and the qualified-name helpers. The agent-server composes
these; the UI surface wires them in rung B.
"""

from __future__ import annotations

from .approval import ApprovalRecord, ApprovalRequired
from .config import McpServerConfig, McpSettings, SecretRef
from .fence import fence_mcp_result
from .http import McpHttpClient
from .http_egress import build_egress_union, url_host
from .naming import qualified_name, split_qualified_name
from .pool import McpPool
from .retrieval_tier import build_retrieval_providers
from .stdio import McpStdioClient
from .tool_search import meta_tool_search

__all__ = [
    "ApprovalRecord",
    "ApprovalRequired",
    "McpHttpClient",
    "McpPool",
    "McpServerConfig",
    "McpSettings",
    "McpStdioClient",
    "SecretRef",
    "build_egress_union",
    "build_retrieval_providers",
    "fence_mcp_result",
    "meta_tool_search",
    "qualified_name",
    "split_qualified_name",
    "url_host",
]
