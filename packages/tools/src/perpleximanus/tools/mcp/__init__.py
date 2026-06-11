"""MCP client — Model Context Protocol v1 (RP-05 rung A).

Public surface: McpPool, McpServerConfig, McpServerSpec, ApprovalRecord,
McpToolDescriptor, and the qualified-name helpers. The agent-server composes
these; the UI surface wires them in rung B.
"""

from __future__ import annotations

from .approval import ApprovalRecord, ApprovalRequired
from .config import McpServerConfig, McpSettings, SecretRef
from .naming import qualified_name, split_qualified_name
from .pool import McpPool
from .stdio import McpStdioClient
from .tool_search import meta_tool_search

__all__ = [
    "ApprovalRecord",
    "ApprovalRequired",
    "McpPool",
    "McpServerConfig",
    "McpSettings",
    "McpStdioClient",
    "SecretRef",
    "meta_tool_search",
    "qualified_name",
    "split_qualified_name",
]
